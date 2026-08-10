"""
overlay_qt/views/pool_view.py
The cards taken so far, plus the shape of the deck they form.

Combines what upstream splits between taken_cards.py (the table) and the
dashboard's mana curve / colour widgets, because during a draft those questions
are asked together: what do I have, and what is it missing.
"""

from PyQt6.QtCore import (QAbstractTableModel, QModelIndex, Qt,
                          QSortFilterProxyModel, pyqtSignal)
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QTreeView,
                             QVBoxLayout, QWidget)

from src import constants
from src.card_logic import get_functional_cmc

from overlay_qt.views.pack_view import RARITY_COLORS, ROW_TINTS, _stats

def _s(count):
    """French plural marker."""
    return "s" if count > 1 else ""


COLUMNS = [("Carte", 200), ("Mana", 78), ("CMC", 46), ("GIH WR", 70), ("Type", 130)]
COL_NAME, COL_MANA, COL_CMC, COL_GIHWR, COL_TYPE = range(5)

COLOR_SWATCHES = {
    "W": QColor("#f9ebce"),
    "U": QColor("#a3cbe8"),
    "B": QColor("#9e9490"),
    "R": QColor("#f39e88"),
    "G": QColor("#c4d5c6"),
}


class PoolTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = []
        self._deck_filter = constants.FILTER_OPTION_ALL_DECKS

    def set_cards(self, cards, deck_filter):
        self.beginResetModel()
        self._cards = list(cards or [])
        self._deck_filter = deck_filter or constants.FILTER_OPTION_ALL_DECKS
        self.endResetModel()

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

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole):
            sorting = role == Qt.ItemDataRole.UserRole
            if column == COL_NAME:
                return card.get(constants.DATA_FIELD_NAME, "?")
            if column == COL_MANA:
                return card.get(constants.DATA_FIELD_MANA_COST, "")
            if column == COL_CMC:
                value = get_functional_cmc(card)
                return value if sorting else str(value)
            if column == COL_GIHWR:
                value = stats.get(constants.DATA_FIELD_GIHWR) or 0.0
                if sorting:
                    return value or -1.0
                return f"{value:.1f}" if value else "-"
            if column == COL_TYPE:
                types = card.get(constants.DATA_FIELD_TYPES) or []
                return " ".join(types) if isinstance(types, list) else str(types)

        if role == Qt.ItemDataRole.ForegroundRole and column == COL_NAME:
            rarity = str(card.get(constants.DATA_FIELD_RARITY, "")).lower()
            return RARITY_COLORS.get(rarity, RARITY_COLORS["common"])

        if role == Qt.ItemDataRole.BackgroundRole:
            from src.card_logic import row_color_tag

            return ROW_TINTS.get(
                row_color_tag(card.get(constants.DATA_FIELD_MANA_COST, ""))
            )

        return None


class ManaCurveWidget(QWidget):
    """Mana curve, creatures stacked under the total, drawn with QPainter."""

    BAR_COLOR = QColor("#3498db")
    CREATURE_COLOR = QColor("#2ecc71")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(76)
        self._all = [0] * 8
        self._creatures = [0] * 8

    def set_distribution(self, distribution_all, distribution_creatures):
        self._all = list(distribution_all or [0] * 8)
        self._creatures = list(distribution_creatures or [0] * 8)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        peak = max(self._all) if any(self._all) else 1
        slots = len(self._all)
        width = max(1, self.width() // slots)

        # Reserve a band under the bars for the CMC labels and one above for
        # the counts, otherwise both get clipped by the widget edges.
        label_band = 14
        count_band = 13
        floor = self.height() - label_band
        usable = max(1, floor - count_band)

        for index in range(slots):
            left = index * width + 2
            bar_width = width - 4

            total_height = int((self._all[index] / peak) * usable)
            painter.fillRect(left, floor - total_height, bar_width, total_height,
                             self.BAR_COLOR)

            creature_height = int((self._creatures[index] / peak) * usable)
            painter.fillRect(left, floor - creature_height, bar_width,
                             creature_height, self.CREATURE_COLOR)

            painter.setPen(QColor("#ecf0f1"))
            label = f"{index}" if index < slots - 1 else f"{index}+"
            painter.drawText(left, floor, bar_width, label_band,
                             int(Qt.AlignmentFlag.AlignCenter), label)
            if self._all[index]:
                painter.drawText(left, floor - total_height - count_band, bar_width,
                                 count_band, int(Qt.AlignmentFlag.AlignCenter),
                                 str(self._all[index]))
        painter.end()


class PoolView(QWidget):
    card_hovered = pyqtSignal(object)
    card_left = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        summary = QWidget()
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(8, 2, 8, 2)
        self.count_label = QLabel("0 carte")
        self.creature_label = QLabel("")
        self.cmc_label = QLabel("")
        for label in (self.count_label, self.creature_label, self.cmc_label):
            summary_layout.addWidget(label)
        summary_layout.addStretch(1)
        self.colors_label = QLabel("")
        summary_layout.addWidget(self.colors_label)
        layout.addWidget(summary)

        self.curve = ManaCurveWidget()
        layout.addWidget(self.curve)

        self.source_model = PoolTableModel(self)
        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(self.source_model)
        proxy.setSortRole(Qt.ItemDataRole.UserRole)

        self.table = QTreeView()
        self.table.setModel(proxy)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_GIHWR, Qt.SortOrder.DescendingOrder)
        self.table.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        for index, (_, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(index, width)
        self.table.header().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.setMouseTracking(True)
        self.table.viewport().setMouseTracking(True)
        self.table.entered.connect(self._on_entered)
        self.table.leaveEvent = self._on_leave
        layout.addWidget(self.table, 1)

    def _on_entered(self, index):
        source = self.table.model().mapToSource(index)
        card = self.source_model._cards[source.row()] if 0 <= source.row() < len(
            self.source_model._cards) else None
        if card is not None:
            self.card_hovered.emit(card)

    def _on_leave(self, event):
        self.card_left.emit()

    def update_pool(self, snapshot):
        cards = snapshot.taken_cards or []
        self.source_model.set_cards(cards, snapshot.active_filter)

        metrics = snapshot.deck_metrics
        if metrics is not None:
            self.count_label.setText(
                f"{metrics.total_cards} carte{_s(metrics.total_cards)}"
            )
            self.creature_label.setText(
                f"|  {metrics.creature_count} créature{_s(metrics.creature_count)}"
                f" / {metrics.noncreature_count} sort{_s(metrics.noncreature_count)}"
            )
            self.cmc_label.setText(f"|  CMC moyen {metrics.cmc_average:.2f}")
            self.curve.set_distribution(
                metrics.distribution_all, metrics.distribution_creatures
            )

        self.colors_label.setText(self._format_signals(snapshot.signals))

    @staticmethod
    def _format_signals(signals):
        """Colour openness, strongest first, as the advisor sees it."""
        if not signals:
            return ""
        ordered = sorted(signals.items(), key=lambda item: item[1], reverse=True)
        return "  ".join(f"{colour} {value:+.1f}" for colour, value in ordered[:3])
