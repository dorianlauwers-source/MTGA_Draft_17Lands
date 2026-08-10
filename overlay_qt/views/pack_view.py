"""
overlay_qt/views/pack_view.py
The current booster, ranked.

Columns mirror the upstream dashboard table (GIH WR / ALSA / IWD) and add the
advisor's own numbers, which upstream only exposes in a separate panel: the
0-100 contextual score, wheel probability and castability. Sorting, column
resizing and alternating rows come from Qt's model/view rather than the ~630
lines of ttk.Treeview subclassing upstream needs for the same result.
"""

from PyQt6.QtCore import (QAbstractTableModel, QModelIndex, Qt,
                          QSortFilterProxyModel, pyqtSignal)
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QHeaderView, QMenu, QTreeView

from src import constants

# Rarity colours, matched to the palette we already used in the prototype.
RARITY_COLORS = {
    "mythic": QColor("#ff9f43"),
    "rare": QColor("#f1c40f"),
    "uncommon": QColor("#bdc3c7"),
    "common": QColor("#ffffff"),
}

# Row tint by colour identity, keyed on the tags upstream's row_color_tag emits.
ROW_TINTS = {
    constants.CARD_ROW_COLOR_WHITE_TAG: QColor(249, 235, 206, 26),
    constants.CARD_ROW_COLOR_BLUE_TAG: QColor(163, 203, 232, 30),
    constants.CARD_ROW_COLOR_BLACK_TAG: QColor(203, 194, 191, 26),
    constants.CARD_ROW_COLOR_RED_TAG: QColor(243, 158, 136, 30),
    constants.CARD_ROW_COLOR_GREEN_TAG: QColor(196, 213, 198, 30),
    constants.CARD_ROW_COLOR_GOLD_TAG: QColor(234, 202, 120, 34),
    constants.CARD_ROW_COLOR_COLORLESS_TAG: QColor(0, 0, 0, 0),
}

GOOD_WR = 56.0
OK_WR = 52.0

COLUMNS = [
    ("Score", 58),
    ("Carte", 210),
    ("Mana", 78),
    ("GIH WR", 70),
    ("ALSA", 56),
    ("IWD", 56),
    ("Wheel", 60),
    ("Jouable", 66),
]

COL_SCORE, COL_NAME, COL_MANA, COL_GIHWR, COL_ALSA, COL_IWD, COL_WHEEL, COL_CAST = range(8)


def _stats(card, deck_filter):
    """
    Per-archetype ratings, with the all-decks bucket filling the gaps.

    Once the pool is large enough the filter switches from "All Decks" to a
    colour pair, and archetype samples are far sparser: the key exists but
    holds 0.0 for most cards. Falling back only when the key is missing left
    two thirds of a real MSH booster showing a win rate of zero, for cards
    that do have a perfectly good overall number.
    """
    colors = card.get(constants.DATA_FIELD_DECK_COLORS, {}) or {}
    overall = colors.get(constants.FILTER_OPTION_ALL_DECKS) or {}
    specific = colors.get(deck_filter) or {}
    if not specific or specific is overall:
        return overall
    merged = dict(overall)
    merged.update({key: value for key, value in specific.items() if value})
    return merged


class PackTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []
        self._by_name = {}
        self._deck_filter = constants.FILTER_OPTION_ALL_DECKS

    # --- data feed ------------------------------------------------------

    def set_pack(self, cards, recommendations, deck_filter):
        self.beginResetModel()
        self._cards = list(cards or [])
        self._deck_filter = deck_filter or constants.FILTER_OPTION_ALL_DECKS
        self._by_name = {
            rec.card_name: rec for rec in (recommendations or [])
        }
        self.endResetModel()

    def card_at(self, row):
        if 0 <= row < len(self._cards):
            return self._cards[row]
        return None

    def recommendation_at(self, row):
        card = self.card_at(row)
        if not card:
            return None
        return self._by_name.get(card.get(constants.DATA_FIELD_NAME))

    # --- Qt model interface ---------------------------------------------

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
        rec = self._by_name.get(card.get(constants.DATA_FIELD_NAME))

        if role == Qt.ItemDataRole.UserRole:
            # Numeric value used for sorting, so "-" never sorts as text.
            return self._sort_value(card, stats, rec, column)

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(card, stats, rec, column)

        if role == Qt.ItemDataRole.ForegroundRole:
            if column == COL_NAME:
                rarity = str(card.get(constants.DATA_FIELD_RARITY, "")).lower()
                return RARITY_COLORS.get(rarity, RARITY_COLORS["common"])
            if column == COL_GIHWR:
                value = stats.get(constants.DATA_FIELD_GIHWR) or 0.0
                if not value:
                    return QColor("#7f8c8d")
                if value >= GOOD_WR:
                    return QColor("#2ecc71")
                if value >= OK_WR:
                    return QColor("#ecf0f1")
                return QColor("#e74c3c")
            if column == COL_SCORE and rec is not None:
                return QColor("#00d2d3") if rec.is_elite else QColor("#ecf0f1")
            return None

        if role == Qt.ItemDataRole.BackgroundRole:
            from src.card_logic import row_color_tag

            tint = ROW_TINTS.get(row_color_tag(card.get(constants.DATA_FIELD_MANA_COST, "")))
            return tint

        if role == Qt.ItemDataRole.FontRole and column in (COL_SCORE, COL_GIHWR):
            font = QFont()
            font.setBold(True)
            return font

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(card, stats, rec)

        if role == Qt.ItemDataRole.TextAlignmentRole and column != COL_NAME:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        return None

    # --- rendering helpers ----------------------------------------------

    def _display(self, card, stats, rec, column):
        if column == COL_NAME:
            return card.get(constants.DATA_FIELD_NAME, "?")
        if column == COL_MANA:
            return card.get(constants.DATA_FIELD_MANA_COST, "")
        if column == COL_SCORE:
            return f"{rec.contextual_score:.0f}" if rec else "-"
        if column == COL_GIHWR:
            value = stats.get(constants.DATA_FIELD_GIHWR) or 0.0
            return f"{value:.1f}" if value else "-"
        if column == COL_ALSA:
            value = stats.get(constants.DATA_FIELD_ALSA) or 0.0
            return f"{value:.1f}" if value else "-"
        if column == COL_IWD:
            value = stats.get(constants.DATA_FIELD_IWD) or 0.0
            return f"{value:+.1f}" if value else "-"
        if column == COL_WHEEL:
            return f"{rec.wheel_chance:.0f}%" if rec else "-"
        if column == COL_CAST:
            return f"{rec.cast_probability * 100:.0f}%" if rec else "-"
        return ""

    def _sort_value(self, card, stats, rec, column):
        if column == COL_NAME:
            return card.get(constants.DATA_FIELD_NAME, "")
        if column == COL_MANA:
            return card.get(constants.DATA_FIELD_CMC, 0) or 0
        if column == COL_SCORE:
            return rec.contextual_score if rec else -1.0
        if column == COL_GIHWR:
            return stats.get(constants.DATA_FIELD_GIHWR) or -1.0
        if column == COL_ALSA:
            # Lower ALSA is better, so invert to keep "best first" consistent.
            value = stats.get(constants.DATA_FIELD_ALSA) or 0.0
            return -value if value else -99.0
        if column == COL_IWD:
            return stats.get(constants.DATA_FIELD_IWD) or -99.0
        if column == COL_WHEEL:
            return rec.wheel_chance if rec else -1.0
        if column == COL_CAST:
            return rec.cast_probability if rec else -1.0
        return 0

    def _tooltip(self, card, stats, rec):
        lines = [f"<b>{card.get(constants.DATA_FIELD_NAME, '?')}</b>"]
        types = card.get(constants.DATA_FIELD_TYPES) or []
        if types:
            lines.append(" ".join(types) if isinstance(types, list) else str(types))
        if rec is not None:
            if rec.archetype_fit and rec.archetype_fit != "Neutral":
                lines.append(f"Archétype : {rec.archetype_fit}")
            for reason in rec.reasoning[:6]:
                lines.append(f"• {reason}")
            if rec.tags:
                lines.append("<i>" + ", ".join(rec.tags[:6]) + "</i>")
        return "<br>".join(lines)


# Columns you cannot hide: without them the table says nothing.
MANDATORY_COLUMNS = {COL_SCORE, COL_NAME}

# Hidden by default in the narrow overlay. The point of a pick view is the
# score, the card and whether it comes back; everything else is on demand.
DEFAULT_HIDDEN = {COL_MANA, COL_ALSA, COL_IWD, COL_CAST}


class PackView(QTreeView):
    """Sorted booster table with hover preview and toggleable columns."""

    card_hovered = pyqtSignal(object)
    card_left = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source_model = PackTableModel(self)

        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(self.source_model)
        proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.setModel(proxy)

        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(False)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.sortByColumn(COL_SCORE, Qt.SortOrder.DescendingOrder)
        self.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)

        header = self.header()
        header.setStretchLastSection(False)
        for index, (_, width) in enumerate(COLUMNS):
            self.setColumnWidth(index, width)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)

        # Columns are added AND removed from the same checkable menu, so a
        # column you turned on can always be turned back off.
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_column_menu)
        for column in DEFAULT_HIDDEN:
            self.setColumnHidden(column, True)

        # Hover preview rather than click-to-freeze.
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.entered.connect(self._on_entered)

    # --- columns --------------------------------------------------------

    def _show_column_menu(self, position):
        menu = QMenu(self)
        menu.addAction("Colonnes affichées").setEnabled(False)
        menu.addSeparator()
        for index, (title, _) in enumerate(COLUMNS):
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(not self.isColumnHidden(index))
            if index in MANDATORY_COLUMNS:
                action.setEnabled(False)
            else:
                action.toggled.connect(
                    lambda visible, col=index: self.setColumnHidden(col, not visible)
                )
        menu.addSeparator()
        menu.addAction("Tout afficher").triggered.connect(self._show_all_columns)
        menu.addAction("Vue compacte").triggered.connect(self._compact_columns)
        menu.exec(self.header().mapToGlobal(position))

    def _show_all_columns(self):
        for index in range(len(COLUMNS)):
            self.setColumnHidden(index, False)

    def _compact_columns(self):
        for index in range(len(COLUMNS)):
            self.setColumnHidden(index, index in DEFAULT_HIDDEN)

    # --- hover ----------------------------------------------------------

    def _on_entered(self, index):
        source = self.model().mapToSource(index)
        card = self.source_model.card_at(source.row())
        if card is not None:
            self.card_hovered.emit(card)

    def leaveEvent(self, event):
        self.card_left.emit()
        super().leaveEvent(event)

    def update_pack(self, snapshot):
        self.source_model.set_pack(
            snapshot.pack_cards, snapshot.recommendations, snapshot.active_filter
        )
