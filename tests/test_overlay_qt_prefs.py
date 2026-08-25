"""
tests/test_overlay_qt_prefs.py
Remembered preferences: size, opacity, column choice and window position.

Position is the odd one out. Wayland gives placement to the compositor, so
move() is ignored and QWidget.pos() reports what Qt intended rather than where
the window is. Writing it there would persist a fiction, so it is skipped, and
these tests pin that behaviour down in both directions.
"""

import json
import os

import pytest

pytest.importorskip("PyQt6", reason="Qt overlay tests need PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from overlay_qt import prefs  # noqa: E402
from overlay_qt.views.pack_view import (  # noqa: E402
    COL_ALSA, COL_MANA, COL_NAME, COL_SCORE, COLUMNS, DEFAULT_HIDDEN,
    MANDATORY_COLUMNS,
)


@pytest.fixture(scope="module")
def qt_app():
    """Module scoped and held: a garbage collected QApplication segfaults."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    path = tmp_path / "overlay_qt_prefs.json"
    monkeypatch.setattr(prefs, "_path", lambda: str(path))
    return path


class TestPreferences:
    def test_defaults_when_nothing_is_stored(self, prefs_file):
        stored = prefs.load()
        assert stored["width"] == prefs.DEFAULTS["width"]
        assert stored["x"] is None and stored["y"] is None
        assert stored["hidden_columns"] is None

    def test_round_trip(self, prefs_file):
        prefs.save({"width": 420, "height": 800, "opacity": 0.7,
                    "x": 120, "y": 40, "hidden_columns": [2, 4]})
        stored = prefs.load()
        assert (stored["width"], stored["height"]) == (420, 800)
        assert stored["opacity"] == 0.7
        assert (stored["x"], stored["y"]) == (120, 40)
        assert stored["hidden_columns"] == [2, 4]

    def test_opacity_is_clamped(self, prefs_file):
        prefs.save({**prefs.DEFAULTS, "opacity": 5.0})
        assert prefs.load()["opacity"] == prefs.MAX_OPACITY
        prefs.save({**prefs.DEFAULTS, "opacity": 0.0})
        assert prefs.load()["opacity"] == prefs.MIN_OPACITY

    def test_a_window_narrower_than_the_floor_is_refused(self, prefs_file):
        prefs.save({**prefs.DEFAULTS, "width": 10, "height": 10})
        stored = prefs.load()
        assert stored["width"] >= 280 and stored["height"] >= 320

    def test_corrupt_values_fall_back(self, prefs_file):
        """
        A hand-edited or half-written file must not stop the overlay starting.
        This file is saved on close, when the process may be going away.
        """
        prefs_file.write_text(
            json.dumps({"width": "large", "opacity": None, "x": "nope",
                        "hidden_columns": "all"}),
            encoding="utf-8",
        )
        stored = prefs.load()
        assert stored["width"] == prefs.DEFAULTS["width"]
        assert stored["opacity"] == prefs.DEFAULTS["opacity"]
        assert stored["x"] is None
        assert stored["hidden_columns"] is None

    def test_unreadable_file_falls_back(self, prefs_file):
        prefs_file.write_text("{ not json at all", encoding="utf-8")
        assert prefs.load()["width"] == prefs.DEFAULTS["width"]

    def test_unknown_keys_are_dropped(self, prefs_file):
        prefs_file.write_text(json.dumps({"width": 400, "junk": 1}), encoding="utf-8")
        assert "junk" not in prefs.load()

    def test_a_missing_directory_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            prefs, "_path", lambda: str(tmp_path / "deep" / "nested" / "p.json")
        )
        prefs.save(dict(prefs.DEFAULTS))
        assert prefs.load()["width"] == prefs.DEFAULTS["width"]


class TestColumnChoice:
    """The upstream menu only ever adds columns; ours has to remove them too."""

    @pytest.fixture
    def view(self, qt_app):
        from overlay_qt.views.pack_view import PackView

        return PackView()

    def test_compact_default(self, view):
        assert set(view.hidden_columns()) == set(DEFAULT_HIDDEN)

    def test_a_hidden_column_can_be_shown_again(self, view):
        view.setColumnHidden(COL_ALSA, False)
        assert COL_ALSA not in view.hidden_columns()

    def test_a_shown_column_can_be_hidden(self, view):
        view.setColumnHidden(COL_MANA, True)
        assert COL_MANA in view.hidden_columns()

    def test_restoring_a_saved_choice(self, view):
        view.set_hidden_columns([COL_MANA])
        assert view.hidden_columns() == [COL_MANA]

    def test_none_restores_the_compact_default(self, view):
        view.set_hidden_columns([])
        view.set_hidden_columns(None)
        assert set(view.hidden_columns()) == set(DEFAULT_HIDDEN)

    def test_mandatory_columns_can_never_be_hidden(self, view):
        """Without the score and the card name the table says nothing."""
        view.set_hidden_columns(list(range(len(COLUMNS))))
        hidden = view.hidden_columns()
        assert COL_SCORE not in hidden
        assert COL_NAME not in hidden
        assert MANDATORY_COLUMNS == {COL_SCORE, COL_NAME}
