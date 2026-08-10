"""
overlay_qt/views/deck_view.py
The 40-card decks the engine builds from the drafted pool.

Deliberately a reading view, not an editor. Upstream's own deck screen is 1587
lines of drag and drop; during a draft what is actually needed is "which
archetype is strongest and what is in it". The full editor stays available in
the upstream window for anyone who wants to shuffle cards by hand.
"""

import re

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QListWidget,
                             QListWidgetItem, QTreeView, QVBoxLayout, QWidget)

from src import constants
from src.card_logic import get_deck_metrics, get_functional_cmc

from overlay_qt.views.pack_view import RARITY_COLORS, ROW_TINTS, _stats

COLUMNS = [("Nb", 32), ("Carte", 190), ("Mana", 74), ("GIH WR", 66)]
COL_COUNT, COL_NAME, COL_MANA, COL_GIHWR = range(4)


def _is_land(card):
    return constants.CARD_TYPE_LAND in (card.get(constants.DATA_FIELD_TYPES) or [])


class DeckCardsModel(QAbstractTableModel):
    """Spells first, ordered by mana value, then the lands."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []
        self._deck_filter = constants.FILTER_OPTION_ALL_DECKS

    def set_cards(self, cards, deck_filter):
        self.beginResetModel()
        self._cards = sorted(
            list(cards or []),
            key=lambda card: (_is_land(card), get_functional_cmc(card),
                              card.get(constants.DATA_FIELD_NAME, "")),
        )
        self._deck_filter = deck_filter or constants.FILTER_OPTION_ALL_DECKS
        self.endResetModel()

    def card_at(self, row):
        return self._cards[row] if 0 <= row < len(self._cards) else None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._cards)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][0]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        card = self._cards[index.row()]
        column = index.column()
        stats = _stats(card, self._deck_filter)

        if role == Qt.ItemDataRole.DisplayRole:
            if column == COL_COUNT:
                return f"{card.get(constants.DATA_FIELD_COUNT, 1)}x"
            if column == COL_NAME:
                return card.get(constants.DATA_FIELD_NAME, "?")
            if column == COL_MANA:
                return card.get(constants.DATA_FIELD_MANA_COST, "")
            if column == COL_GIHWR:
                value = stats.get(constants.DATA_FIELD_GIHWR) or 0.0
                return f"{value:.1f}" if value else "-"

        if role == Qt.ItemDataRole.ForegroundRole and column == COL_NAME:
            rarity = str(card.get(constants.DATA_FIELD_RARITY, "")).lower()
            return RARITY_COLORS.get(rarity, RARITY_COLORS["common"])

        if role == Qt.ItemDataRole.BackgroundRole:
            from src.card_logic import row_color_tag

            return ROW_TINTS.get(
                row_color_tag(card.get(constants.DATA_FIELD_MANA_COST, ""))
            )

        if role == Qt.ItemDataRole.TextAlignmentRole and column != COL_NAME:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        return None


class DeckView(QWidget):
    card_hovered = pyqtSignal(object)
    card_left = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._decks = {}
        self._deck_filter = constants.FILTER_OPTION_ALL_DECKS

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self.status = QLabel("Aucune proposition pour l'instant.")
        self.status.setStyleSheet("color: #95a5a6; padding: 6px;")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        # The archetype list is the point of the screen, so it leads.
        self.archetypes = QListWidget()
        self.archetypes.setMaximumHeight(96)
        self.archetypes.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { padding: 3px 6px; }"
            "QListWidget::item:selected { background: #34495e; }"
        )
        self.archetypes.currentRowChanged.connect(self._on_archetype_selected)
        layout.addWidget(self.archetypes)

        summary = QWidget()
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(8, 0, 8, 0)
        self.shape_label = QLabel("")
        self.shape_label.setStyleSheet("color: #bdc3c7; font-size: 11px;")
        summary_layout.addWidget(self.shape_label)
        summary_layout.addStretch(1)
        layout.addWidget(summary)

        self.model = DeckCardsModel(self)
        self.table = QTreeView()
        self.table.setModel(self.model)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        for index, (_, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(index, width)
        self.table.header().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.setMouseTracking(True)
        self.table.viewport().setMouseTracking(True)
        self.table.entered.connect(self._on_entered)
        self.table.leaveEvent = self._on_leave
        layout.addWidget(self.table, 1)

    # --- data -----------------------------------------------------------

    def set_pending(self):
        self.status.setText("Construction des decks en cours...")

    def set_decks(self, decks, deck_filter):
        self._decks = decks or {}
        self._deck_filter = deck_filter

        self.archetypes.clear()
        if not self._decks:
            self.status.setText(
                "Pas encore de proposition. Il faut au moins 15 cartes jouables "
                "dans la pool."
            )
            self.model.set_cards([], deck_filter)
            self.shape_label.setText("")
            return

        self.status.setText(f"{len(self._decks)} archétype(s) proposé(s)")
        for name, deck in self._decks.items():
            # The name already carries the power score, so drop the duplicate.
            label = re.sub(r"\s*\(Power: \d+\)", "", name)
            item = QListWidgetItem(f"{deck.get('rating') or 0:>3.0f}   {label}")
            item.setFont(QFont("Sans Serif", 10))
            self.archetypes.addItem(item)
        self.archetypes.setCurrentRow(0)

    # --- selection ------------------------------------------------------

    def _on_archetype_selected(self, row):
        if row < 0 or row >= len(self._decks):
            return
        deck = list(self._decks.values())[row]
        cards = deck.get("deck_cards", [])
        self.model.set_cards(cards, self._deck_filter)

        total = sum(card.get(constants.DATA_FIELD_COUNT, 1) for card in cards)
        lands = sum(card.get(constants.DATA_FIELD_COUNT, 1)
                    for card in cards if _is_land(card))

        # get_deck_metrics counts one entry per card, so a playset of four
        # would weigh the same as a single copy. Expand first.
        expanded = []
        for card in cards:
            if _is_land(card):
                continue
            expanded.extend([card] * max(1, card.get(constants.DATA_FIELD_COUNT, 1)))
        metrics = get_deck_metrics(expanded)
        curve = "-".join(str(metrics.distribution_all[i]) for i in range(1, 7))
        self.shape_label.setText(
            f"{total} cartes · {lands} terrains · {metrics.creature_count} créatures"
            f" · courbe {curve}"
        )

        breakdown = deck.get("breakdown") or ""
        record = deck.get("record") or ""
        details = "  ·  ".join(x for x in (f"Est. {record}" if record else "", breakdown) if x)
        self.status.setText(details or f"{len(self._decks)} archétype(s) proposé(s)")

    # --- hover ----------------------------------------------------------

    def _on_entered(self, index):
        card = self.model.card_at(index.row())
        if card is not None:
            self.card_hovered.emit(card)

    def _on_leave(self, event):
        self.card_left.emit()
