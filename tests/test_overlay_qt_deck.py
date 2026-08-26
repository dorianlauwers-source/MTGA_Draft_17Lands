"""
tests/test_overlay_qt_deck.py
The deck tab: build checklist, comparison against a submitted deck, and Sealed.

These were the two views validated only by hand. Everything here drives the
real widgets through their real signals, off screen.
"""

import os
import tempfile

import pytest

pytest.importorskip("PyQt6", reason="Qt overlay tests need PyQt6")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import src.card_logic  # noqa: F401,E402  (import order: card_logic <-> deck_builder)
import src.utils as utils  # noqa: E402
from src import constants  # noqa: E402
from src.log_scanner import ArenaScanner  # noqa: E402

from overlay_qt.state import DraftSnapshot, read_submitted_deck  # noqa: E402
from overlay_qt.views.deck_view import DeckView  # noqa: E402

from tests.test_log_scanner_data import (  # noqa: E402
    DSK_SEALED_ENTRIES_2024_9_24,
    TEST_SETS,
)


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def _card(name, cmc=2, count=1, types=None, gihwr=55.0, rarity="common"):
    return {
        constants.DATA_FIELD_NAME: name,
        constants.DATA_FIELD_CMC: cmc,
        constants.DATA_FIELD_COUNT: count,
        constants.DATA_FIELD_TYPES: types or ["Creature"],
        constants.DATA_FIELD_RARITY: rarity,
        constants.DATA_FIELD_MANA_COST: "{%d}" % cmc,
        constants.DATA_FIELD_DECK_COLORS: {
            constants.FILTER_OPTION_ALL_DECKS: {constants.DATA_FIELD_GIHWR: gihwr}
        },
    }


def _land(name="Plains", count=8):
    return _card(name, cmc=0, count=count, types=["Basic", "Land"], gihwr=0.0)


DECK = {
    "WU Test [Est: 7-x] (Power: 91)": {
        "rating": 91.0,
        "record": "7-x",
        "breakdown": "Excellent Aggro Curve (+5.0)",
        "deck_cards": [
            _card("Alpha", cmc=1, count=2),
            _card("Bravo", cmc=1),
            _card("Charlie", cmc=2, count=3),
            _card("Delta", cmc=3),
            _land("Plains", 8),
            _land("Island", 9),
        ],
    }
}


@pytest.fixture
def deck_view(qt_app):
    view = DeckView()
    view.set_decks(DECK, constants.FILTER_OPTION_ALL_DECKS)
    return view


class TestChecklist:
    def test_cards_are_grouped_by_mana_value(self, deck_view):
        model = deck_view.model
        groups = [model.item(row).text() for row in range(model.rowCount())]
        assert groups[0].startswith("1 mana")
        assert any(g.startswith("2 mana") for g in groups)
        assert groups[-1].startswith("Terrains"), "lands sort last"

    def test_group_counter_counts_copies_not_entries(self, deck_view):
        """Alpha is a playset of two; the group must read 3, not 2."""
        header = deck_view.model.item(0).text()
        assert header.endswith("0/3"), header

    def test_ticking_a_card_updates_both_counters(self, deck_view, qt_app):
        model = deck_view.model
        group = model.item(0)
        group.child(0, 0).setCheckState(Qt.CheckState.Checked)
        qt_app.processEvents()          # the redraw is deferred on purpose
        deck_view._render()

        assert deck_view._checked, "the tick was not recorded"
        # 7 spells and 17 lands, and Alpha is a playset of two.
        assert model.horizontalHeaderItem(0).text() == "Cible 2/24"
        assert model.item(0).text().endswith("2/3"), "the group counter follows too"
        assert "Reste 22" in deck_view.status.text()

    def test_reset_clears_every_tick(self, deck_view, qt_app):
        group = deck_view.model.item(0)
        group.child(0, 0).setCheckState(Qt.CheckState.Checked)
        qt_app.processEvents()
        deck_view._reset_checks()
        assert deck_view._checked == set()

    def test_ticking_does_not_destroy_the_live_item(self, deck_view, qt_app):
        """
        Rebuilding the model inside itemChanged frees the item mid-signal and
        segfaults; the redraw has to be deferred. Ticking several boxes in a
        row is the shape that used to crash.
        """
        for _ in range(3):
            group = deck_view.model.item(0)
            for row in range(group.rowCount()):
                group.child(row, 0).setCheckState(Qt.CheckState.Checked)
            qt_app.processEvents()
        assert deck_view.model.rowCount() > 0


class TestComparison:
    def _submitted(self, view, cards):
        by_id = {str(i): card for i, card in enumerate(cards)}
        entries = [(i, card[constants.DATA_FIELD_COUNT]) for i, card in by_id.items()]
        view.set_submitted_deck(entries, lambda cid: by_id.get(str(cid)))

    def test_identical_decks_show_no_difference(self, deck_view):
        self._submitted(deck_view, DECK["WU Test [Est: 7-x] (Power: 91)"]["deck_cards"])
        deck_view._toggle_mode()
        groups = [deck_view.model.item(r).text() for r in range(deck_view.model.rowCount())]
        assert "À RETIRER (0)" in groups
        assert "À AJOUTER (0)" in groups

    def test_missing_and_extra_cards_are_reported(self, deck_view):
        played = [
            _card("Alpha", cmc=1, count=2),
            _card("Bravo", cmc=1),
            _card("Intruder", cmc=4),      # not in the recommendation
            _land("Plains", 8),
        ]
        self._submitted(deck_view, played)
        deck_view._toggle_mode()

        model = deck_view.model
        groups = {model.item(r).text(): model.item(r) for r in range(model.rowCount())}
        remove = next(k for k in groups if k.startswith("À RETIRER"))
        add = next(k for k in groups if k.startswith("À AJOUTER"))

        assert "(1)" in remove, "Intruder should be flagged for removal"
        # Charlie x3 and Delta are missing from what was played.
        assert "(4)" in add, add
        names = [groups[add].child(r, 0).text() for r in range(groups[add].rowCount())]
        assert any("Charlie" in n for n in names)

    def test_basic_lands_are_excluded_from_the_comparison(self, deck_view):
        """
        Arena supplies basics from an unlimited pool and 17lands has no entry
        for them, so they never resolve and would make a 40-card deck read as
        16. They must not appear as a difference.
        """
        played = list(DECK["WU Test [Est: 7-x] (Power: 91)"]["deck_cards"])
        self._submitted(deck_view, played)
        deck_view._toggle_mode()
        model = deck_view.model
        for row in range(model.rowCount()):
            group = model.item(row)
            for child in range(group.rowCount()):
                assert "Plains" not in group.child(child, 0).text()
                assert "Island" not in group.child(child, 0).text()

    def test_button_is_disabled_without_a_submitted_deck(self, deck_view):
        deck_view.set_submitted_deck([], lambda cid: None)
        assert not deck_view.btn_mode.isEnabled()
        assert "soumis" in deck_view.btn_mode.toolTip()


class TestSealed:
    """Sealed hands over the whole pool at once, with no booster ever."""

    @pytest.fixture
    def sealed_scanner(self, tmp_path, monkeypatch):
        sets_dir = tmp_path / "Sets"
        sets_dir.mkdir()
        monkeypatch.setattr(utils, "SETS_FOLDER", str(sets_dir))
        utils.invalidate_local_set_cache()

        log_path = tmp_path / "Player.log"
        log_path.write_text("", encoding="utf-8")
        scanner = ArenaScanner(
            str(log_path), TEST_SETS, sets_location=str(sets_dir),
            retrieve_unknown=False,
        )
        scanner.log_enable(False)
        for _label, _expected, entry in DSK_SEALED_ENTRIES_2024_9_24:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(entry + "\n")
            scanner.draft_start_search()
            scanner.draft_data_search()
        return scanner

    def test_the_whole_pool_is_captured(self, sealed_scanner):
        assert sealed_scanner.retrieve_current_limited_event() == ("DSK", "Sealed")
        # Six boosters, so far more than a draft's 45 picks.
        assert len(sealed_scanner.taken_cards) == 84

    def test_no_booster_is_ever_reported(self, sealed_scanner):
        assert sealed_scanner.retrieve_current_pack_cards() == []
        assert sealed_scanner.retrieve_current_pack_and_pick() == (0, 0)

    def test_snapshot_flags_sealed_and_says_so(self):
        snapshot = DraftSnapshot(
            event_type="Sealed", taken_cards=[_card("Alpha")] * 84
        )
        assert snapshot.is_sealed
        assert not snapshot.is_drafting
        assert "Sealed" in snapshot.status_text
        assert "84" in snapshot.status_text

    def test_a_draft_is_not_mistaken_for_sealed(self):
        snapshot = DraftSnapshot(event_type="QuickDraft", pack=1, pick=3)
        assert not snapshot.is_sealed
        assert snapshot.status_text == "Pack 1 Pick 3"


def test_submitted_deck_reader_survives_a_missing_log():
    assert read_submitted_deck("", "Sealed_DSK") == []
    assert read_submitted_deck(tempfile.gettempdir() + "/nope.log", "x") == []


class TestDetailedLogsDetection:
    """
    Arena writes no draft data at all when plugin-support logging is off, and
    the setting does not follow the account between installs: enabling it on a
    Flatpak Steam install leaves a native Steam install disabled. Detecting it
    is the difference between an explanation and an overlay that looks broken.
    """

    def _log(self, tmp_path, marker):
        path = tmp_path / "Player.log"
        path.write_text(
            "Unity Player log\n" + marker + "\n[UnityCrossThreadLogger]Client.SceneChange {}\n",
            encoding="utf-8",
        )
        return str(path)

    def test_enabled_is_detected(self, tmp_path):
        from overlay_qt.state import detailed_logs_enabled

        assert detailed_logs_enabled(self._log(tmp_path, "DETAILED LOGS: ENABLED")) is True

    def test_disabled_is_detected(self, tmp_path):
        from overlay_qt.state import detailed_logs_enabled

        assert detailed_logs_enabled(self._log(tmp_path, "DETAILED LOGS: DISABLED")) is False

    def test_unknown_when_the_marker_is_absent(self, tmp_path):
        from overlay_qt.state import detailed_logs_enabled

        assert detailed_logs_enabled(self._log(tmp_path, "nothing here")) is None

    def test_unknown_when_the_log_is_missing(self, tmp_path):
        from overlay_qt.state import detailed_logs_enabled

        assert detailed_logs_enabled(str(tmp_path / "absent.log")) is None
        assert detailed_logs_enabled("") is None

    def test_the_last_marker_wins(self, tmp_path):
        """Arena stamps one per session; a relaunch appends a fresh line."""
        from overlay_qt.state import detailed_logs_enabled

        path = tmp_path / "Player.log"
        path.write_text(
            "DETAILED LOGS: DISABLED\nsession one\nDETAILED LOGS: ENABLED\nsession two\n",
            encoding="utf-8",
        )
        assert detailed_logs_enabled(str(path)) is True


class TestDraftPersistence:
    """
    ArenaScanner persists the whole draft, history included, and reloads it in
    its constructor. That file is the only thing that survives a log rotation:
    once Arena restarts, the per-pick events are gone from Player.log and only
    the final CardPool remains, so a pool rebuilt from the log alone carries no
    history and therefore no colour signals.

    Rebuilding unconditionally at startup wiped that state and saved the empty
    result over it, losing the history for good.
    """

    class _Scanner:
        def __init__(self, event=("HOB", "QuickDraft"), taken=None, history=None):
            self._event = event
            self.taken_cards = list(taken or [])
            self.draft_history = list(history or [])
            self.event_string = "QuickDraft_HOB_20260820"
            self.cleared = False
            self.scanned = 0

        def retrieve_current_limited_event(self):
            return self._event

        def clear_draft(self, full):
            self.cleared = True
            self.taken_cards = []
            self.draft_history = []

        def draft_start_search(self):
            self.scanned += 1
            return False

        def draft_data_search(self):
            self.scanned += 1
            return False

        def retrieve_data_sources(self):
            return {}

    def test_a_restored_draft_is_recognised(self):
        from overlay_qt.state import has_restored_draft

        assert has_restored_draft(
            self._Scanner(taken=["1", "2"], history=[{"Pack": 1, "Pick": 1}])
        )

    def test_an_empty_scanner_is_not(self):
        from overlay_qt.state import has_restored_draft

        assert not has_restored_draft(self._Scanner(event=("", ""), taken=[]))
        assert not has_restored_draft(self._Scanner(taken=[], history=[]))

    def test_restored_state_is_never_cleared(self):
        from overlay_qt.state import ensure_draft

        scanner = self._Scanner(
            taken=["1", "2", "3"],
            history=[{"Pack": 1, "Pick": p} for p in range(1, 13)],
        )
        ensure_draft(scanner)

        assert not scanner.cleared, "the persisted draft was thrown away"
        assert len(scanner.taken_cards) == 3
        assert len(scanner.draft_history) == 12
        assert scanner.scanned >= 2, "it should still catch up on new picks"

    def test_a_cold_start_does_rebuild(self):
        """With nothing restored there is no history to protect."""
        from overlay_qt.state import ensure_draft

        scanner = self._Scanner(event=("", ""), taken=[])
        ensure_draft(scanner)
        assert scanner.cleared
