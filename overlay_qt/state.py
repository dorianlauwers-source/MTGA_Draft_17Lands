"""
overlay_qt/state.py
Draft state extraction, deliberately free of any GUI framework.

This is the logic half of src/ui/app_controller.py:refresh_ui_data(): take a
consistent snapshot of the scanner under its lock, then run the signal and
advisor engines on it. Keeping it framework-free means it can be unit tested
without a display, reused by a headless mode, and shared between the Tk and Qt
front ends.

Nothing here mutates upstream state, so upstream can keep evolving underneath.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src import constants
from src.advisor.engine import DraftAdvisor
from src.card_logic import filter_options, get_deck_metrics
from src.signals import SignalCalculator


@dataclass
class DraftSnapshot:
    """Everything a view needs for one refresh, already computed."""

    event_set: str = ""
    event_type: str = ""
    event_string: str = ""
    draft_id: str = ""
    start_time: Any = None

    pack: int = 0
    pick: int = 0

    pack_cards: List[dict] = field(default_factory=list)
    missing_cards: List[dict] = field(default_factory=list)
    taken_cards: List[dict] = field(default_factory=list)
    picked_cards: List[dict] = field(default_factory=list)

    recommendations: List[Any] = field(default_factory=list)
    signals: Dict[str, float] = field(default_factory=dict)
    colors: str = ""

    metrics: Any = None
    tier_data: Any = None
    deck_metrics: Any = None

    @property
    def is_drafting(self) -> bool:
        return self.pack > 0

    @property
    def active_filter(self) -> str:
        """
        The archetype key used to index card["deck_colors"].

        filter_options() returns a single-element list (["All Decks"] or
        ["UB"]), so unwrap it once here rather than in every view.
        """
        if isinstance(self.colors, (list, tuple)):
            return self.colors[0] if self.colors else constants.FILTER_OPTION_ALL_DECKS
        return self.colors or constants.FILTER_OPTION_ALL_DECKS

    @property
    def status_text(self) -> str:
        if self.is_drafting:
            return f"Pack {self.pack} Pick {self.pick}"
        return "Waiting for draft..."


def take_raw_snapshot(scanner, blocking: bool = False) -> Optional[dict]:
    """
    Grab every field we need in one critical section.

    Returns None when the scanner lock is busy, so the caller can retry rather
    than block; this mirrors what AppController does with a non-blocking
    acquire followed by a delayed retry.
    """
    if not scanner.lock.acquire(blocking=blocking):
        return None
    try:
        event_set, event_type = scanner.retrieve_current_limited_event()
        pack, pick = scanner.retrieve_current_pack_and_pick()
        return {
            "event_set": event_set,
            "event_type": event_type,
            "pack": pack,
            "pick": pick,
            "metrics": scanner.retrieve_set_metrics(),
            "tier_data": scanner.retrieve_tier_data(),
            "taken_cards": scanner.retrieve_taken_cards(),
            "pack_cards": scanner.retrieve_current_pack_cards(),
            "missing_cards": scanner.retrieve_current_missing_cards(),
            "picked_cards": scanner.retrieve_current_picked_cards(),
            "history": scanner.retrieve_draft_history(),
            "draft_id": scanner.current_draft_id,
            "start_time": scanner.draft_start_time,
            "event_string": scanner.event_string,
        }
    finally:
        scanner.lock.release()


def compute_signals(scanner, metrics, history) -> Dict[str, float]:
    """
    Cumulative colour openness across the packs seen so far.

    Pack 2 is skipped because it passes the other way round, so its contents
    say nothing about what the neighbours on your left are taking.
    """
    calculator = SignalCalculator(metrics)
    scores = {colour: 0.0 for colour in constants.CARD_COLORS}
    for entry in history or []:
        if entry.get("Pack") == 2:
            continue
        pack = scanner.set_data.get_data_by_id(entry["Cards"])
        for colour, value in calculator.calculate_pack_signals(
            pack, entry["Pick"]
        ).items():
            scores[colour] += value
    return scores


def build_snapshot(scanner, configuration, raw: Optional[dict] = None,
                   with_recommendations: bool = True) -> Optional[DraftSnapshot]:
    """
    Full refresh: snapshot, signals, advisor, colour filter, deck metrics.

    This is the expensive call. DraftAdvisor.evaluate_pack rebuilds candidate
    decks per card from pack 3 onwards, so it must never run on the GUI thread.
    """
    if raw is None:
        raw = take_raw_snapshot(scanner)
    if raw is None:
        return None

    metrics = raw["metrics"]
    signals = compute_signals(scanner, metrics, raw["history"])

    recommendations = []
    if with_recommendations and raw["pack_cards"]:
        advisor = DraftAdvisor(metrics, raw["taken_cards"], signals=signals)
        recommendations = advisor.evaluate_pack(
            raw["pack_cards"], raw["pick"], current_pack=raw["pack"]
        )

    colors = filter_options(
        raw["taken_cards"], configuration.settings.deck_filter, metrics, configuration
    )

    return DraftSnapshot(
        event_set=raw["event_set"],
        event_type=raw["event_type"],
        event_string=raw["event_string"],
        draft_id=raw["draft_id"],
        start_time=raw["start_time"],
        pack=raw["pack"],
        pick=raw["pick"],
        pack_cards=raw["pack_cards"],
        missing_cards=raw["missing_cards"],
        taken_cards=raw["taken_cards"],
        picked_cards=raw["picked_cards"],
        recommendations=recommendations,
        signals=signals,
        colors=colors,
        metrics=metrics,
        tier_data=raw["tier_data"],
        deck_metrics=get_deck_metrics(raw["taken_cards"]),
    )
