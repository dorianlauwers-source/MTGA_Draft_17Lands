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

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src import constants
from src.advisor.engine import DraftAdvisor
from src.card_logic import filter_options, get_deck_metrics
from src.signals import SignalCalculator

logger = logging.getLogger(__name__)

# A dataset with fewer rated cards than this is not worth showing: the table
# fills with zeros. 17lands publishes a file per event type, and the ones for
# formats nobody has played yet contain every card with no statistics at all.
MINIMUM_RATED_CARDS = 50
MAX_DATASETS_TRIED = 4


def _rated_card_count(scanner) -> int:
    """
    How many cards in the loaded dataset actually carry a win rate.

    Counting the entries of _dataset is meaningless: it holds three sections
    (meta, color_ratings, card_ratings), so any size check against it is
    always true and silently discards a perfectly good dataset.
    """
    dataset = getattr(scanner.set_data, "_dataset", None) or {}
    ratings = dataset.get("card_ratings") or {}
    count = 0
    for card in ratings.values():
        stats = (card.get(constants.DATA_FIELD_DECK_COLORS) or {}).get(
            constants.FILTER_OPTION_ALL_DECKS
        ) or {}
        if (stats.get(constants.DATA_FIELD_GIHWR) or 0) > 0:
            count += 1
    return count


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


def bind_dataset(scanner, configuration=None) -> bool:
    """
    Attach the ratings dataset matching the event currently being drafted.

    Without this the scanner still resolves names, types and mana costs from
    the local card database, so the table and the mana curve look right while
    every win rate, advisor score and wheel chance sits at zero. Mirrors what
    main.load_data does after its deep scan.
    """
    event_set, event_type = scanner.retrieve_current_limited_event()
    if not event_set:
        return False

    marker = f"[{event_set.upper()}]"
    candidates = [
        (label, path)
        for label, path in (scanner.retrieve_data_sources() or {}).items()
        if marker in label.upper() and isinstance(path, str) and os.path.exists(path)
    ]
    if not candidates:
        logger.warning("No local dataset found for %s", event_set)
        return False

    def rank(item):
        """
        Matching only on the set code is not enough. On MSH it picked
        ContenderDraft (Top), a file holding three cards, and every win rate
        came out at zero. Prefer the event actually being played, and the
        all-players sample over the top-players one, which is far smaller.
        """
        label = item[0].upper()
        exact_event = event_type and event_type.upper() in label
        all_players = "(ALL)" in label
        return (not exact_event, not all_players, label)

    candidates.sort(key=rank)

    best = None
    for label, path in candidates[:MAX_DATASETS_TRIED]:
        scanner.retrieve_set_data(path)
        usable = _rated_card_count(scanner)
        if best is None or usable > best[0]:
            best = (usable, label, path)
        if usable >= MINIMUM_RATED_CARDS:
            break

    usable, label, path = best
    # retrieve_set_data replaces whatever was loaded, so if the last file tried
    # was not the best one the scanner is currently holding the wrong dataset.
    if path != candidates[0][1] or usable != _rated_card_count(scanner):
        scanner.retrieve_set_data(path)

    if configuration is not None:
        configuration.card_data.latest_dataset = os.path.basename(path)
    logger.info("Dataset bound to %s %s: %s (%d rated cards)",
                event_set, event_type, label, usable)
    if not usable:
        logger.warning("No win rate data in any %s dataset yet", event_set)
    return usable > 0


def rebuild_draft(scanner, configuration=None) -> bool:
    """
    Re-read the log from byte zero and restore a draft already in progress.

    Order matters: the event has to be identified before its dataset can be
    picked, and the dataset has to be in place before the picks are replayed,
    otherwise the cards come back without statistics.
    """
    scanner.clear_draft(True)
    scanner.draft_start_search()
    bound = bind_dataset(scanner, configuration)
    scanner.draft_data_search()
    return bound


MINIMUM_POOL_FOR_DECK = 15


def suggest_decks(snapshot, configuration) -> Dict[str, Any]:
    """
    Ask the upstream builder for complete 40-card decks from the drafted pool.

    It runs four strategies in parallel (strict castability, capped splash,
    curve-weighted and colour-blind best cards), computes a mana base from the
    coloured pip counts, then scores each candidate with a 10,000 game Monte
    Carlo simulation that penalises colour screw and flood. No language model
    is involved: the result is reproducible.

    src.card_logic has to be imported first. It and deck_builder import each
    other, and reaching deck_builder first raises ImportError on
    GLOBAL_DECK_CACHE. Upstream only escapes this through the order its own
    UI happens to import things.
    """
    if snapshot is None or len(snapshot.taken_cards or []) < MINIMUM_POOL_FOR_DECK:
        return {}

    import src.card_logic  # noqa: F401  (import order matters, see above)
    from src.advisor.deck_builder import suggest_deck

    return suggest_deck(
        snapshot.taken_cards,
        snapshot.metrics,
        configuration,
        event_type=snapshot.event_type or "PremierDraft",
        progress_callback=lambda *args, **kwargs: None,
        dataset_name=getattr(configuration.card_data, "latest_dataset", None),
    )


SUBMITTED_DECK_TAIL_BYTES = 8 * 1024 * 1024


def read_submitted_deck(log_path: str, event_name: str) -> List[tuple]:
    """
    The deck Arena has on record for this event, as [(card_id, quantity)].

    Arena only publishes a limited deck once it has been submitted: while you
    are still building, the Courses payload reports CurrentModule DeckSelect
    and an empty MainDeck. So this can confirm what you ended up playing, but
    it can never follow the build card by card.
    """
    if not log_path or not event_name or not os.path.exists(log_path):
        return []

    try:
        size = os.path.getsize(log_path)
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(max(0, size - SUBMITTED_DECK_TAIL_BYTES))
            raw = handle.read()
    except OSError:
        logger.debug("Could not read %s", log_path, exc_info=True)
        return []

    decoder = json.JSONDecoder()
    best: List[tuple] = []
    for match in re.finditer(r'\{"Courses":\[', raw):
        try:
            payload, _ = decoder.raw_decode(raw[match.start():])
        except ValueError:
            continue
        for course in payload.get("Courses") or []:
            if course.get("InternalEventName") != event_name:
                continue
            entries = (course.get("CourseDeck") or {}).get("MainDeck") or []
            deck = [
                (str(item["cardId"]), int(item.get("quantity", 1)))
                for item in entries
                if isinstance(item, dict) and "cardId" in item
            ]
            if deck:
                best = deck          # later payloads win
    return best


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
