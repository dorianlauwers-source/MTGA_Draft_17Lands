"""
overlay_qt/views/advisor_view.py
The advisor's top picks, with the reasoning that produced them.

Mirrors src/ui/advisor_view.py: a score badge, an accent bar, the card name and
the engine's own explanation lines, with elite picks highlighted. Upstream
rebuilds the whole widget tree on every refresh (destroy + recreate); here the
rows are created once and only their text changes, which avoids the flicker and
the churn during a fast draft.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

ELITE_COLOR = "#10b981"
NORMAL_COLOR = "#3b82f6"
MUTED_COLOR = "#95a5a6"

DEFAULT_LIMIT = 3


class _RecommendationRow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(8)

        self.accent = QFrame()
        self.accent.setFixedWidth(4)
        self.accent.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(self.accent)

        self.score = QLabel("-")
        self.score.setFont(QFont("Sans Serif", 15, QFont.Weight.Bold))
        self.score.setFixedWidth(46)
        self.score.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.score)

        text_column = QVBoxLayout()
        text_column.setSpacing(0)
        self.name = QLabel("")
        self.name.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.reason = QLabel("")
        self.reason.setFont(QFont("Sans Serif", 9))
        self.reason.setStyleSheet(f"color: {MUTED_COLOR};")
        self.reason.setWordWrap(True)
        text_column.addWidget(self.name)
        text_column.addWidget(self.reason)
        layout.addLayout(text_column, 1)

    def set_recommendation(self, rec):
        if rec is None:
            self.hide()
            return
        colour = ELITE_COLOR if rec.is_elite else NORMAL_COLOR
        self.accent.setStyleSheet(f"background: {colour};")
        self.score.setText(f"{rec.contextual_score:.0f}")
        self.score.setStyleSheet(f"color: {colour};")
        star = "★ " if rec.is_elite else ""
        self.name.setText(f"{star}{rec.card_name}")

        parts = list(rec.reasoning[:3])
        if rec.archetype_fit and rec.archetype_fit != "Neutral":
            parts.insert(0, rec.archetype_fit)
        self.reason.setText("  •  ".join(parts) if parts else "")
        self.setToolTip("\n".join(rec.reasoning) if rec.reasoning else "")
        self.show()


class AdvisorPanel(QWidget):
    """Compact ranking of the best picks in the current booster."""

    def __init__(self, limit=DEFAULT_LIMIT, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.placeholder = QLabel("En attente d'un booster...")
        self.placeholder.setStyleSheet(f"color: {MUTED_COLOR}; padding: 10px;")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.placeholder)

        self.rows = [_RecommendationRow() for _ in range(limit)]
        for row in self.rows:
            row.hide()
            layout.addWidget(row)

    def update_recommendations(self, recommendations):
        recommendations = list(recommendations or [])
        # evaluate_pack returns them in engine order; the panel is a ranking.
        recommendations.sort(key=lambda rec: rec.contextual_score, reverse=True)

        self.placeholder.setVisible(not recommendations)
        for index, row in enumerate(self.rows):
            row.set_recommendation(
                recommendations[index] if index < len(recommendations) else None
            )
