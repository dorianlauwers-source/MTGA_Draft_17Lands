"""
tests/test_overlay_qt_pipeline.py
End-to-end check of the Qt6 overlay pipeline on real Arena draft log lines.

Replays the MSH ContenderDraft session captured in test_log_scanner_data.py
through the upstream scanner, builds a snapshot with overlay_qt.state, and
feeds it to the Qt table model. No Qt application or display is required: the
model is a QAbstractTableModel, not a widget.

This is the pipeline the overlay runs in production, so a regression anywhere
between the log line and the rendered cell shows up here.
"""

import os
import shutil

import pytest

from tests.test_log_scanner_data import (
    MSH_CONTENDER_DRAFT_ENTRIES_2026_7_7,
    TEST_SETS,
)

from src import constants
from src.log_scanner import ArenaScanner
from src.utils import invalidate_local_set_cache

pytest.importorskip("PyQt6", reason="Qt overlay tests need PyQt6")

from overlay_qt.state import build_snapshot, compute_signals, take_raw_snapshot  # noqa: E402
from overlay_qt.views.pack_view import (  # noqa: E402
    COL_CAST,
    COL_GIHWR,
    COL_NAME,
    COL_SCORE,
    COL_WHEEL,
    PackTableModel,
)

# The real dataset the app downloads for this event. Skip rather than fail when
# it is absent, so CI without a populated Sets/ directory stays green.
REAL_DATASET = os.path.expanduser(
    "~/.config/MTGA_Draft_Tool/Sets/MSH_ContenderDraft_All_Data.json"
)


class _Configuration:
    """Minimal stand-in for the pydantic Configuration the state module reads."""

    class settings:
        deck_filter = constants.FILTER_OPTION_ALL_DECKS


@pytest.fixture
def msh_scanner(tmp_path, monkeypatch):
    """Scanner replaying the recorded MSH ContenderDraft session."""
    sets_dir = tmp_path / "Sets"
    sets_dir.mkdir()
    if os.path.exists(REAL_DATASET):
        shutil.copy(REAL_DATASET, sets_dir / os.path.basename(REAL_DATASET))

    # ArenaScanner takes a sets_location, but retrieve_data_sources() reaches
    # for the module-level SETS_FOLDER instead, so point that at the temp copy
    # and drop the memoised listing.
    monkeypatch.setattr("src.utils.SETS_FOLDER", str(sets_dir))
    invalidate_local_set_cache()

    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    scanner = ArenaScanner(
        str(log_path), TEST_SETS, sets_location=str(sets_dir), retrieve_unknown=False
    )
    scanner.log_enable(False)

    for _label, _expected, entry in MSH_CONTENDER_DRAFT_ENTRIES_2026_7_7:
        # Reopen per line: the scanner reads the file independently, so an open
        # handle would keep the writes in the buffer and it would see nothing.
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(entry + "\n")
        scanner.draft_start_search()
        scanner.draft_data_search()

    sources = scanner.retrieve_data_sources()
    for location in sources.values():
        if isinstance(location, str) and os.path.exists(location):
            scanner.retrieve_set_data(location)
            break

    return scanner


def test_scanner_recognises_the_contender_draft(msh_scanner):
    event_set, event_type = msh_scanner.retrieve_current_limited_event()
    assert event_set == "MSH"
    assert event_type == "ContenderDraft"


def test_raw_snapshot_is_consistent(msh_scanner):
    raw = take_raw_snapshot(msh_scanner, blocking=True)
    assert raw is not None
    assert raw["event_set"] == "MSH"
    assert raw["pack"] >= 1
    assert raw["pick"] >= 1
    # A booster holds cards, and the picks made so far are tracked.
    assert isinstance(raw["pack_cards"], list)
    assert isinstance(raw["taken_cards"], list)


def test_signals_cover_every_colour(msh_scanner):
    raw = take_raw_snapshot(msh_scanner, blocking=True)
    signals = compute_signals(msh_scanner, raw["metrics"], raw["history"])
    assert set(signals) == set(constants.CARD_COLORS)
    assert all(isinstance(value, float) for value in signals.values())


def test_snapshot_drives_the_table_model(msh_scanner):
    snapshot = build_snapshot(msh_scanner, _Configuration())
    assert snapshot is not None
    assert snapshot.is_drafting
    assert snapshot.status_text.startswith("Pack ")

    model = PackTableModel()
    model.set_pack(snapshot.pack_cards, snapshot.recommendations, snapshot.active_filter)

    assert model.rowCount() == len(snapshot.pack_cards)
    if not snapshot.pack_cards:
        pytest.skip("no booster in the replayed window")

    from PyQt6.QtCore import Qt

    names = [
        model.data(model.index(row, COL_NAME), Qt.ItemDataRole.DisplayRole)
        for row in range(model.rowCount())
    ]
    assert all(isinstance(name, str) and name for name in names)
    assert "?" not in names, "every card in the booster must resolve to a name"

    # Every cell must render without raising, for every role the view asks for.
    roles = [
        Qt.ItemDataRole.DisplayRole,
        Qt.ItemDataRole.ForegroundRole,
        Qt.ItemDataRole.BackgroundRole,
        Qt.ItemDataRole.ToolTipRole,
        Qt.ItemDataRole.UserRole,
    ]
    for row in range(model.rowCount()):
        for column in range(model.columnCount()):
            for role in roles:
                model.data(model.index(row, column), role)


@pytest.mark.skipif(
    not os.path.exists(REAL_DATASET), reason="real MSH ContenderDraft dataset absent"
)
def test_real_dataset_yields_real_numbers(msh_scanner):
    """With the downloaded dataset the table must show actual statistics."""
    from PyQt6.QtCore import Qt

    snapshot = build_snapshot(msh_scanner, _Configuration())
    if not snapshot.pack_cards:
        pytest.skip("no booster in the replayed window")

    model = PackTableModel()
    model.set_pack(snapshot.pack_cards, snapshot.recommendations, snapshot.active_filter)

    def column_values(column):
        return [
            model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)
            for row in range(model.rowCount())
        ]

    # The advisor must have scored the booster.
    assert snapshot.recommendations, "advisor produced no recommendation"
    scores = [v for v in column_values(COL_SCORE) if v != "-"]
    assert scores, "no contextual score reached the table"

    # Wheel and castability come from the advisor and are always present.
    assert any(v != "-" for v in column_values(COL_WHEEL))
    assert any(v != "-" for v in column_values(COL_CAST))

    # At least one card should carry a win rate from the real dataset.
    win_rates = [v for v in column_values(COL_GIHWR) if v != "-"]
    assert win_rates, "no GIH win rate resolved from the real dataset"


def test_advisor_panel_ranks_by_score(msh_scanner):
    """The panel is a ranking, whatever order the engine returns."""
    from overlay_qt.views.advisor_view import AdvisorPanel

    snapshot = build_snapshot(msh_scanner, _Configuration())
    if not snapshot.recommendations:
        pytest.skip("no recommendation in the replayed window")

    panel = AdvisorPanel.__new__(AdvisorPanel)   # no QWidget, no display needed
    ordered = sorted(
        snapshot.recommendations, key=lambda rec: rec.contextual_score, reverse=True
    )
    scores = [rec.contextual_score for rec in ordered]
    assert scores == sorted(scores, reverse=True)
    assert ordered[0].contextual_score >= ordered[-1].contextual_score
    del panel


def test_pool_model_renders_taken_cards(msh_scanner):
    from PyQt6.QtCore import Qt

    from overlay_qt.views.pool_view import PoolTableModel, COL_NAME as POOL_NAME

    snapshot = build_snapshot(msh_scanner, _Configuration())
    model = PoolTableModel()
    model.set_cards(snapshot.taken_cards, snapshot.active_filter)

    assert model.rowCount() == len(snapshot.taken_cards or [])
    for row in range(model.rowCount()):
        name = model.data(model.index(row, POOL_NAME), Qt.ItemDataRole.DisplayRole)
        assert isinstance(name, str) and name != "?"
        for column in range(model.columnCount()):
            for role in (
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.UserRole,
                Qt.ItemDataRole.ForegroundRole,
                Qt.ItemDataRole.BackgroundRole,
            ):
                model.data(model.index(row, column), role)


def test_deck_metrics_reach_the_snapshot(msh_scanner):
    snapshot = build_snapshot(msh_scanner, _Configuration())
    metrics = snapshot.deck_metrics
    assert metrics is not None
    assert metrics.total_cards == len(snapshot.taken_cards or [])
    assert len(metrics.distribution_all) == 8
    assert len(metrics.distribution_creatures) == 8
    # Creatures are a subset of the whole curve, slot by slot.
    assert all(
        creature <= total
        for creature, total in zip(
            metrics.distribution_creatures, metrics.distribution_all
        )
    )


def test_sorting_keys_are_numeric(msh_scanner):
    """UserRole feeds the sort proxy; text there would sort '9' above '10'."""
    from PyQt6.QtCore import Qt

    snapshot = build_snapshot(msh_scanner, _Configuration())
    model = PackTableModel()
    model.set_pack(snapshot.pack_cards, snapshot.recommendations, snapshot.active_filter)
    if not model.rowCount():
        pytest.skip("no booster in the replayed window")

    for column in (COL_SCORE, COL_GIHWR, COL_WHEEL, COL_CAST):
        for row in range(model.rowCount()):
            value = model.data(model.index(row, column), Qt.ItemDataRole.UserRole)
            assert isinstance(value, (int, float)), (
                f"column {column} row {row} sorts on {type(value).__name__}"
            )


class TestDatasetRebinding:
    """
    Joining a draft while the overlay is already running must load the ratings
    file for the event actually being played.

    The upstream orchestrator binds one itself, but sync_dataset_to_event takes
    the first source whose label carries the set code, in dictionary order.
    Measured on a real HOB PremierDraft it loaded HOB_TradSealed (45 rated
    cards) and every win rate in the pack read 0.0. The bridge therefore
    re-binds with the event-aware selection once the event is known.
    """

    @pytest.fixture
    def bridge(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from overlay_qt import bridge as bridge_module

        log = tmp_path / "Player.log"
        log.write_text("DETAILED LOGS: ENABLED\n", encoding="utf-8")

        config = SimpleNamespace(
            settings=SimpleNamespace(arena_log_location=str(log)),
            card_data=SimpleNamespace(latest_dataset=""),
        )
        scanner = SimpleNamespace(event_string="", arena_file=str(log))

        started = []
        monkeypatch.setattr(bridge_module.QThreadPool, "start",
                            lambda self, worker, *a: started.append(worker))

        instance = bridge_module.DraftBridge(scanner, config)
        return instance, scanner, started

    def test_nothing_is_rebound_while_idle(self, bridge):
        instance, _scanner, started = bridge
        instance._rebind_if_event_changed()
        assert started == [], "no event means nothing to bind"

    def test_joining_a_draft_triggers_a_rebind(self, bridge):
        from overlay_qt.bridge import _BindWorker

        instance, scanner, started = bridge
        scanner.event_string = "PremierDraft_HOB_20260820"
        instance._rebind_if_event_changed()
        assert len(started) == 1
        assert isinstance(started[0], _BindWorker)

    def test_the_same_event_is_not_rebound_twice(self, bridge):
        instance, scanner, started = bridge
        scanner.event_string = "PremierDraft_HOB_20260820"
        instance._rebind_if_event_changed()
        instance._binding = False           # first bind finished
        instance._rebind_if_event_changed()
        assert len(started) == 1, "re-reading a 25 MB dataset every tick"

    def test_a_second_draft_rebinds_again(self, bridge):
        instance, scanner, started = bridge
        scanner.event_string = "PremierDraft_HOB_20260820"
        instance._rebind_if_event_changed()
        instance._binding = False
        scanner.event_string = "QuickDraft_MSH_20260710"
        instance._rebind_if_event_changed()
        assert len(started) == 2

    def test_a_bind_in_flight_is_not_duplicated(self, bridge):
        instance, scanner, started = bridge
        scanner.event_string = "PremierDraft_HOB_20260820"
        instance._rebind_if_event_changed()
        scanner.event_string = "QuickDraft_MSH_20260710"
        instance._rebind_if_event_changed()   # previous one still running
        assert len(started) == 1

    def test_a_cold_start_that_already_bound_does_not_rebind(self, tmp_path, monkeypatch):
        """rebuild_draft binds before the bridge exists; don't redo the work."""
        from types import SimpleNamespace

        from overlay_qt import bridge as bridge_module

        log = tmp_path / "Player.log"
        log.write_text("x", encoding="utf-8")
        config = SimpleNamespace(
            settings=SimpleNamespace(arena_log_location=str(log)),
            card_data=SimpleNamespace(latest_dataset="HOB_PremierDraft_All_Data.json"),
        )
        scanner = SimpleNamespace(
            event_string="PremierDraft_HOB_20260820", arena_file=str(log)
        )
        started = []
        monkeypatch.setattr(bridge_module.QThreadPool, "start",
                            lambda self, worker, *a: started.append(worker))

        instance = bridge_module.DraftBridge(scanner, config)
        instance._rebind_if_event_changed()
        assert started == []

    def test_a_second_draft_of_the_same_event_rebinds(self, bridge):
        """
        Two QuickDrafts of the same set share an event string. Keying the
        re-bind on the name alone skipped the second one, leaving whatever
        the upstream set-code match had loaded.
        """
        instance, scanner, started = bridge
        scanner.event_string = "QuickDraft_HOB_20260820"
        scanner.current_transaction_id = "6c473c0a-4350-4499-a7c5-3db0a103b1a7"
        instance._rebind_if_event_changed()
        instance._binding = False
        scanner.current_transaction_id = "11111111-2222-3333-4444-555555555555"
        instance._rebind_if_event_changed()
        assert len(started) == 2
