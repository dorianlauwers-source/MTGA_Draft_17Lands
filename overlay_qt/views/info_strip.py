"""
overlay_qt/views/info_strip.py
One permanently visible line: lane, curve, pool, wheel.

Upstream puts colour signals, the mana curve and the pool balance in separate
dashboard widgets, which means scrolling or going full screen to read them
during a draft. They are important but they are glance information, so they
belong on a single line that never moves and never needs scrolling.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

# A card that wheels comes back after the other seven players have picked.
WHEEL_THRESHOLD = 50.0

LANE_COLORS = {
    "W": "#f9ebce",
    "U": "#a3cbe8",
    "B": "#b0a6a2",
    "R": "#f39e88",
    "G": "#c4d5c6",
}


class InfoStrip(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(10)

        font = QFont("Sans Serif", 9)
        self.lane = QLabel("")
        self.curve = QLabel("")
        self.pool = QLabel("")
        self.wheel = QLabel("")
        for label in (self.lane, self.curve, self.pool, self.wheel):
            label.setFont(font)
            label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.lane)
        layout.addStretch(1)
        layout.addWidget(self.curve)
        layout.addStretch(1)
        layout.addWidget(self.pool)
        layout.addStretch(1)
        layout.addWidget(self.wheel)

        self.setStyleSheet("background: rgba(255,255,255,0.05);")
        self.setFixedHeight(22)

    def update_from(self, snapshot):
        self.lane.setText(self._lane_text(snapshot.signals))
        self.curve.setText(self._curve_text(snapshot.deck_metrics))
        self.pool.setText(self._pool_text(snapshot.deck_metrics))
        self.wheel.setText(self._wheel_text(snapshot.recommendations))

        self.lane.setToolTip("Couleurs les plus ouvertes d'après les boosters vus")
        self.curve.setToolTip("Répartition des coûts de mana, de 1 à 6 et plus")
        self.pool.setToolTip("Cartes prises, créatures, coût moyen")
        self.wheel.setToolTip(
            f"Cartes de ce booster ayant plus de {WHEEL_THRESHOLD:.0f}% "
            "de chances de revenir"
        )

    # --- sections -------------------------------------------------------

    @staticmethod
    def _lane_text(signals):
        if not signals:
            return "<span style='color:#7f8c8d;'>lane —</span>"
        ranked = sorted(signals.items(), key=lambda item: item[1], reverse=True)[:2]
        parts = []
        for colour, value in ranked:
            tint = LANE_COLORS.get(colour, "#ecf0f1")
            parts.append(f"<b style='color:{tint};'>{colour}</b> {value:+.1f}")
        return " ".join(parts)

    @staticmethod
    def _curve_text(metrics):
        if metrics is None or not any(metrics.distribution_all):
            return "<span style='color:#7f8c8d;'>courbe —</span>"
        # Slots 1..6+, slot 0 holds the zero-cost cards and is rarely useful.
        counts = metrics.distribution_all
        cells = [str(counts[i]) for i in range(1, 7)]
        return "<span style='color:#95a5a6;'>courbe</span> " + "-".join(cells)

    @staticmethod
    def _pool_text(metrics):
        if metrics is None:
            return ""
        return (
            f"<span style='color:#95a5a6;'>pool</span> {metrics.total_cards}"
            f" · {metrics.creature_count}cr"
            f" · {metrics.cmc_average:.1f}"
        )

    @staticmethod
    def _wheel_text(recommendations):
        wheeling = [
            rec for rec in (recommendations or [])
            if getattr(rec, "wheel_chance", 0.0) >= WHEEL_THRESHOLD
        ]
        if not wheeling:
            return "<span style='color:#7f8c8d;'>wheel 0</span>"
        return f"<span style='color:#f1c40f;'>wheel {len(wheeling)}</span>"
