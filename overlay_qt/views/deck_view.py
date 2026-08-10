"""
overlay_qt/views/deck_view.py
The 40-card decks the engine builds, and help getting them into Arena.

Arena offers no way to import a deck during a limited event, and it only
publishes the deck once it has been submitted: while you are building, the log
reports an empty MainDeck. So the overlay cannot follow the build card by card,
and pretending otherwise would be worse than useless.

What it can do is two honest things:
  * a checklist, grouped the way Arena groups your pool, that you tick off as
    you click cards in, with a counter of what is left
  * once the deck is submitted, a comparison against what the engine would
    have played
"""

import re

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QTreeView,
                             QVBoxLayout, QWidget)

from src import constants
from src.card_logic import get_deck_metrics, get_functional_cmc

from overlay_qt.views.pack_view import RARITY_COLORS, _stats

CARD_ROLE = Qt.ItemDataRole.UserRole + 1

ADD_COLOR = QColor("#2ecc71")
REMOVE_COLOR = QColor("#e74c3c")
DONE_COLOR = QColor("#5d6d7e")


def _is_land(card):
    return constants.CARD_TYPE_LAND in (card.get(constants.DATA_FIELD_TYPES) or [])


def _bucket_label(card):
    if _is_land(card):
        return "Terrains"
    cmc = int(get_functional_cmc(card) or 0)
    return f"{min(cmc, 6)}+ mana" if cmc >= 6 else f"{cmc} mana"


def _copies(card):
    return max(1, int(card.get(constants.DATA_FIELD_COUNT, 1) or 1))


class DeckView(QWidget):
    card_hovered = pyqtSignal(object)
    card_left = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._decks = {}
        self._deck_filter = constants.FILTER_OPTION_ALL_DECKS
        self._checked = set()
        self._current_cards = []
        self._submitted = []
        self._submitted_lands = 0
        self._mode = "target"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self.status = QLabel("Aucune proposition pour l'instant.")
        self.status.setStyleSheet("color: #95a5a6; padding: 4px 6px; font-size: 11px;")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.archetypes = QListWidget()
        self.archetypes.setMaximumHeight(74)
        self.archetypes.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { padding: 2px 6px; }"
            "QListWidget::item:selected { background: #34495e; }"
        )
        self.archetypes.currentRowChanged.connect(self._on_archetype_selected)
        layout.addWidget(self.archetypes)

        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(6, 0, 6, 0)
        toolbar_layout.setSpacing(4)
        self.shape_label = QLabel("")
        self.shape_label.setStyleSheet("color: #bdc3c7; font-size: 11px;")
        toolbar_layout.addWidget(self.shape_label, 1)
        self.btn_mode = self._small_button("Écart", self._toggle_mode)
        self.btn_reset = self._small_button("Réinit.", self._reset_checks)
        toolbar_layout.addWidget(self.btn_mode)
        toolbar_layout.addWidget(self.btn_reset)
        layout.addWidget(toolbar)

        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(["Carte", "WR"])
        self.model.itemChanged.connect(self._on_item_changed)

        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        # The name needs every pixel at 340px wide; pin the rating narrow so a
        # checkbox and an indent do not leave "3x Aerial Do...".
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 44)
        self.tree.setIndentation(12)
        self.tree.setMouseTracking(True)
        self.tree.viewport().setMouseTracking(True)
        self.tree.entered.connect(self._on_entered)
        self.tree.leaveEvent = self._on_leave
        layout.addWidget(self.tree, 1)

    @staticmethod
    def _small_button(text, slot):
        button = QPushButton(text)
        button.setFixedHeight(20)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.10); color: #ecf0f1;"
            " border: none; border-radius: 3px; padding: 1px 8px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.20); }"
        )
        button.clicked.connect(slot)
        return button

    # --- data -----------------------------------------------------------

    def set_pending(self):
        self.status.setText("Construction des decks en cours...")

    def set_submitted_deck(self, entries, card_lookup):
        """
        entries is [(card_id, quantity)] as recorded by Arena.

        Basic lands never resolve: Arena supplies them from an unlimited pool
        and 17lands has no entry for them, so a raw count here reads 16 cards
        instead of 40. They are counted separately and the comparison is made
        on spells, which is the part a build decision turns on anyway.
        """
        cards = []
        unresolved = 0
        for card_id, quantity in entries or []:
            card = card_lookup(card_id)
            if card:
                cards.append(dict(card, **{constants.DATA_FIELD_COUNT: quantity}))
            else:
                unresolved += quantity
        self._submitted = cards
        self._submitted_lands = unresolved
        self.btn_mode.setEnabled(bool(cards))
        self.btn_mode.setToolTip(
            "Comparer votre deck soumis à la recommandation" if cards else
            "Disponible une fois le deck soumis dans Arena :\n"
            "Arena ne publie le deck qu'à ce moment-là."
        )

    def set_decks(self, decks, deck_filter):
        self._decks = decks or {}
        self._deck_filter = deck_filter
        self.archetypes.clear()

        if not self._decks:
            self.status.setText(
                "Pas encore de proposition. Il faut au moins 15 cartes jouables."
            )
            self.model.removeRows(0, self.model.rowCount())
            self.shape_label.setText("")
            return

        for name, deck in self._decks.items():
            label = re.sub(r"\s*\(Power: \d+\)", "", name)
            item = QListWidgetItem(f"{deck.get('rating') or 0:>3.0f}   {label}")
            item.setFont(QFont("Sans Serif", 10))
            self.archetypes.addItem(item)
        self.archetypes.setCurrentRow(0)

    # --- modes ----------------------------------------------------------

    def _toggle_mode(self):
        self._mode = "diff" if self._mode == "target" else "target"
        self.btn_mode.setText("Cible" if self._mode == "diff" else "Écart")
        self._render()

    def _reset_checks(self):
        self._checked.clear()
        self._render()

    def _on_archetype_selected(self, row):
        if row < 0 or row >= len(self._decks):
            return
        deck = list(self._decks.values())[row]
        self._current_cards = deck.get("deck_cards", [])
        self._checked.clear()

        cards = self._current_cards
        total = sum(_copies(c) for c in cards)
        lands = sum(_copies(c) for c in cards if _is_land(c))
        expanded = [c for c in cards if not _is_land(c) for _ in range(_copies(c))]
        metrics = get_deck_metrics(expanded)
        curve = "-".join(str(metrics.distribution_all[i]) for i in range(1, 7))
        self.shape_label.setText(
            f"{total} · {lands} terrains · {metrics.creature_count} cr · {curve}"
        )
        record = deck.get("record") or ""
        self.status.setText(
            "  ·  ".join(x for x in (f"Est. {record}" if record else "",
                                     deck.get("breakdown") or "") if x)
        )
        self._render()

    # --- rendering ------------------------------------------------------

    def _render(self):
        self.model.blockSignals(True)
        self.model.removeRows(0, self.model.rowCount())
        if self._mode == "diff":
            self._render_diff()
        else:
            self._render_target()
        self.model.blockSignals(False)
        self.tree.expandAll()

    def _card_row(self, card, prefix="", colour=None, checkable=True):
        name = card.get(constants.DATA_FIELD_NAME, "?")
        label = QStandardItem(f"{prefix}{_copies(card)}x {name}")
        label.setData(card, CARD_ROLE)
        label.setEditable(False)
        # Long names are cut at 340px wide, so keep the full one within reach.
        types = card.get(constants.DATA_FIELD_TYPES) or []
        label.setToolTip(
            f"{name}\n{' '.join(types) if isinstance(types, list) else types}"
        )
        if checkable:
            label.setCheckable(True)
            done = name in self._checked
            label.setCheckState(
                Qt.CheckState.Checked if done else Qt.CheckState.Unchecked
            )
            if done:
                font = label.font()
                font.setStrikeOut(True)
                label.setFont(font)
                label.setForeground(DONE_COLOR)
            else:
                rarity = str(card.get(constants.DATA_FIELD_RARITY, "")).lower()
                label.setForeground(RARITY_COLORS.get(rarity, RARITY_COLORS["common"]))
        elif colour is not None:
            label.setForeground(colour)

        value = _stats(card, self._deck_filter).get(constants.DATA_FIELD_GIHWR) or 0.0
        rating = QStandardItem(f"{value:.1f}" if value else "-")
        rating.setEditable(False)
        rating.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return [label, rating]

    def _render_target(self):
        groups = {}
        for card in self._current_cards:
            groups.setdefault(_bucket_label(card), []).append(card)

        def order(key):
            return (key == "Terrains", key)

        root = self.model.invisibleRootItem()
        for bucket in sorted(groups, key=order):
            cards = sorted(groups[bucket],
                           key=lambda c: c.get(constants.DATA_FIELD_NAME, ""))
            done = sum(_copies(c) for c in cards
                       if c.get(constants.DATA_FIELD_NAME) in self._checked)
            total = sum(_copies(c) for c in cards)
            header = QStandardItem(f"{bucket}   {done}/{total}")
            header.setEditable(False)
            header.setForeground(QColor("#95a5a6"))
            root.appendRow([header, QStandardItem("")])
            for card in cards:
                header.appendRow(self._card_row(card))

        placed = sum(_copies(c) for c in self._current_cards
                     if c.get(constants.DATA_FIELD_NAME) in self._checked)
        total = sum(_copies(c) for c in self._current_cards)
        remaining = total - placed
        self.tree.setHeaderHidden(False)
        self.model.setHorizontalHeaderLabels(
            [f"Cible {placed}/{total}" if placed else "Deck cible", "WR"]
        )
        if remaining and placed:
            self.status.setText(f"Reste {remaining} carte(s) à ajouter dans Arena.")
        elif placed and not remaining:
            self.status.setText("Deck complet. Vérifiez avec « Écart » après soumission.")

    def _render_diff(self):
        # Spells only: land counts follow from the spells, and basic lands do
        # not resolve on the Arena side at all.
        target = {
            card.get(constants.DATA_FIELD_NAME): card
            for card in self._current_cards if not _is_land(card)
        }
        submitted = {
            card.get(constants.DATA_FIELD_NAME): card
            for card in self._submitted if not _is_land(card)
        }

        def counts(source):
            return {name: _copies(card) for name, card in source.items()}

        want, have = counts(target), counts(submitted)
        to_remove, to_add = [], []
        for name in set(want) | set(have):
            delta = have.get(name, 0) - want.get(name, 0)
            if delta > 0:
                card = dict(submitted[name], **{constants.DATA_FIELD_COUNT: delta})
                to_remove.append(card)
            elif delta < 0:
                card = dict(target[name], **{constants.DATA_FIELD_COUNT: -delta})
                to_add.append(card)

        root = self.model.invisibleRootItem()
        for title, cards, colour, prefix in (
            (f"À RETIRER ({sum(_copies(c) for c in to_remove)})", to_remove,
             REMOVE_COLOR, "− "),
            (f"À AJOUTER ({sum(_copies(c) for c in to_add)})", to_add,
             ADD_COLOR, "+ "),
        ):
            header = QStandardItem(title)
            header.setEditable(False)
            header.setForeground(colour)
            root.appendRow([header, QStandardItem("")])
            for card in sorted(cards, key=lambda c: c.get(constants.DATA_FIELD_NAME, "")):
                header.appendRow(self._card_row(card, prefix, colour, checkable=False))

        shared = sum(min(want.get(n, 0), have.get(n, 0)) for n in set(want) | set(have))
        total = sum(want.values())
        self.model.setHorizontalHeaderLabels(["Votre deck vs moteur", "WR"])
        if not self._submitted:
            self.status.setText(
                "Aucun deck soumis trouvé dans le log. Arena ne le publie "
                "qu'une fois le deck validé."
            )
        else:
            lands = getattr(self, "_submitted_lands", 0)
            note = f"  ·  {lands} terrains non comparés" if lands else ""
            self.status.setText(
                f"{shared}/{total} sorts en commun avec la recommandation{note}."
            )

    # --- interaction ----------------------------------------------------

    def _on_item_changed(self, item):
        card = item.data(CARD_ROLE)
        if card is None or self._mode != "target":
            return
        name = card.get(constants.DATA_FIELD_NAME)
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(name)
        else:
            self._checked.discard(name)
        # Deferred: rebuilding the model here would destroy the very item whose
        # signal we are inside, which segfaults.
        QTimer.singleShot(0, self._render)

    def _on_entered(self, index):
        item = self.model.itemFromIndex(index.siblingAtColumn(0))
        card = item.data(CARD_ROLE) if item else None
        if card is not None:
            self.card_hovered.emit(card)

    def _on_leave(self, event):
        self.card_left.emit()
