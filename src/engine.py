# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""The Daily Mix selection engine.

Design notes
------------
* Cards are chosen first, then materialised through Anki's *official* filtered
  deck API (``col.sched.add_or_update_filtered_deck``) with ``reschedule=True``.
  We never touch ``ivl``, ``factor`` or FSRS memory state.
* The filtered deck is rebuilt from scratch every time a session starts, which
  automatically returns stale cards to their home decks.
* Positional ordering inside the filtered deck is stored in ``cards.due`` (the
  same field Anki's own filtered-deck builder uses for positions).  With
  rescheduling enabled, the scheduler reads ``original_due`` instead, so
  reordering is safe and does not affect scheduling.
* Every search is built from numeric deck/card ids, never from user-entered
  deck names, so quoting and wildcard characters cannot break a query.
"""

from __future__ import annotations

import heapq
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from . import constants
from .config import normalize_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DailyMixError(Exception):
    """Base class for user-presentable Daily Mix errors."""


class NoDecksConfigured(DailyMixError):
    pass


class NoCardsAvailable(DailyMixError):
    pass


class QuotaReached(DailyMixError):
    pass


class MixDeckError(DailyMixError):
    pass


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DeckSource:
    """One selectable source of cards (a deck or a top-level deck group)."""

    name: str
    deck_ids: list[int]
    weight: float = 1.0
    review_weight: float = 0.0
    new_weight: float = 0.0
    available_review: int = 0
    available_new: int = 0
    planned_review: int = 0
    planned_new: int = 0
    picked_review: list[int] = field(default_factory=list, repr=False)
    picked_new: list[int] = field(default_factory=list, repr=False)
    review_pool: list[int] = field(default_factory=list, repr=False)
    new_pool: list[int] = field(default_factory=list, repr=False)

    @property
    def planned_total(self) -> int:
        return self.planned_review + self.planned_new


@dataclass
class MixPlan:
    target: int
    done: int
    remaining: int
    review_target: int
    new_target: int
    sources: list[DeckSource]
    uses_all_decks: bool
    order_strategy: str
    review_percent: int
    review_cids: list[int] = field(default_factory=list)
    new_cids: list[int] = field(default_factory=list)

    @property
    def planned_review(self) -> int:
        return sum(source.planned_review for source in self.sources)

    @property
    def planned_new(self) -> int:
        return sum(source.planned_new for source in self.sources)

    @property
    def planned_total(self) -> int:
        return self.planned_review + self.planned_new

    @property
    def available_review(self) -> int:
        return sum(source.available_review for source in self.sources)

    @property
    def available_new(self) -> int:
        return sum(source.available_new for source in self.sources)

    def sequential_cids(self) -> list[int]:
        """Cards in deck order, reviews before new cards inside each deck."""
        result: list[int] = []
        for source in self.sources:
            result.extend(source.picked_review)
            result.extend(source.picked_new)
        return result


@dataclass
class MixDeckStatus:
    id: int | None
    name: str
    exists: bool
    card_count: int


@dataclass
class SessionResult:
    deck_id: int
    deck_name: str
    card_count: int
    plan: MixPlan


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def allocate(
    total: int,
    weights: Sequence[float],
    capacities: Sequence[int],
) -> list[int]:
    """Allocate *total* units proportionally to weights, capped by capacities.

    Uses a largest-average (Jefferson / D'Hondt) style priority queue.  The
    method naturally redistributes units when a deck hits its availability cap,
    and the integer result always sums to ``min(total, sum(capacities))``.

    Example: with two equal decks and a total of 5, the result is [3, 2].
    """
    if total <= 0:
        return [0] * len(weights)

    allocation = [0] * len(weights)
    heap: list[tuple[float, int]] = []

    for index, (weight, capacity) in enumerate(zip(weights, capacities)):
        if capacity > 0 and weight > 0:
            heapq.heappush(heap, (-float(weight), index))

    for _ in range(min(total, sum(max(0, cap) for cap in capacities))):
        if not heap:
            break
        _, index = heapq.heappop(heap)
        allocation[index] += 1
        if allocation[index] < capacities[index]:
            next_score = float(weights[index]) / (allocation[index] + 1)
            heapq.heappush(heap, (-next_score, index))

    return allocation


def deck_depth(name: str) -> int:
    """Number of ``::`` separators in a deck name plus one.

    Used when claiming deck ids for configured sources.  Deeper decks must be
    claimed first so that a selected subdeck keeps its own weight instead of
    being swallowed by a selected parent.
    """
    return name.count("::") + 1


def _round_half_up(value: float) -> int:
    return int(value + 0.5)


def _query_for_decks(deck_ids: Sequence[int], state: str) -> str:
    valid_dids = sorted(set(int(did) for did in deck_ids))
    if not valid_dids:
        return "did:0"
    if len(valid_dids) == 1:
        deck_clause = f"did:{valid_dids[0]}"
    else:
        deck_clause = "(" + " or ".join(f"did:{did}" for did in valid_dids) + ")"
    return f"{deck_clause} {state} {constants.SEARCH_EXCLUDES}".strip()


def _cid_query(card_ids: Sequence[int]) -> str:
    return "cid:" + ",".join(str(int(cid)) for cid in card_ids)


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def _answered_today_query() -> str:
    """SQL counting distinct cards answered today."""
    types = ",".join(str(int(value)) for value in constants.REVLOG_ANSWER_TYPES)
    return (
        "select count(distinct cid) from revlog "
        "where id > ? and type in (%s)" % types
    )


def get_daily_progress(col: Any) -> int:
    """Number of distinct cards answered today (Anki day cutoff aware)."""
    query = _answered_today_query()
    try:
        cutoff = int(col.sched.day_cutoff)
        start_ms = (cutoff - constants.SECONDS_PER_DAY) * 1000
        count = col.db.scalar(query, start_ms)
        return int(count or 0)
    except Exception:
        # Extremely old schedulers do not expose day_cutoff.  Fall back to
        # local midnight rather than breaking the dashboard.
        logger.debug("day_cutoff unavailable; falling back to local midnight")
        try:
            now = datetime.now()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ms = int(start.timestamp() * 1000)
            count = col.db.scalar(query, start_ms)
            return int(count or 0)
        except Exception:
            logger.exception("Could not read today's review count")
            return 0


def get_seconds_until_reset(col: Any) -> int:
    """Seconds until Anki's next day cutoff, for the all-done message."""
    try:
        cutoff = int(col.sched.day_cutoff)
        return max(0, cutoff - int(time.time()))
    except Exception:
        tomorrow = datetime.now() + timedelta(days=1)
        midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        return max(0, int(midnight.timestamp() - time.time()))


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


def resolve_sources(col: Any, config: dict) -> tuple[list[DeckSource], bool]:
    """Resolve configured decks into disjoint groups of deck ids.

    Returns ``(sources, uses_all_decks)``.  With an empty ``decks`` list all
    normal decks are grouped by their top-level deck, and each group's weight
    is based on its available pool (so "All decks" behaves proportionally).
    """
    all_decks = list(col.decks.all_names_and_ids(include_filtered=False))
    if not all_decks:
        raise NoDecksConfigured(
            "There are no decks in this collection. Create a deck first."
        )

    configured = config.get("decks") or []
    if not configured:
        groups: dict[str, list[int]] = {}
        for entry in all_decks:
            root = entry.name.split("::", 1)[0]
            groups.setdefault(root, []).append(int(entry.id))

        sources = [
            DeckSource(
                name=root,
                deck_ids=sorted(dids),
                weight=1.0,
                review_weight=1.0,
                new_weight=1.0,
            )
            for root, dids in sorted(groups.items())
        ]
        return sources, True

    by_name = {entry.name: int(entry.id) for entry in all_decks}
    valid_ids = {int(entry.id) for entry in all_decks}
    claimed: set[int] = set()
    sources_by_position: list[tuple[int, DeckSource]] = []

    # Claim deeper decks first.  This lets a user give a subdeck its own weight
    # while the parent still covers the rest of its subtree.  Sources are put
    # back into the configured order afterwards so that sequential study and
    # the dashboard follow the user's deck order.
    ordered_entries = sorted(
        enumerate(configured),
        key=lambda pair: (-deck_depth(str(pair[1].get("name", ""))), pair[0]),
    )

    for position, entry in ordered_entries:
        name = str(entry.get("name", ""))
        did = by_name.get(name)
        if did is None:
            continue

        if config.get("include_subdecks", True):
            try:
                deck_ids = [int(x) for x in col.decks.deck_and_child_ids(did)]
            except Exception:
                deck_ids = [did]
        else:
            deck_ids = [did]

        # `deck_and_child_ids()` can include filtered children; those are not
        # sources and must never be emptied or searched through.
        deck_ids = [x for x in deck_ids if x in valid_ids and x not in claimed]
        if not deck_ids:
            continue
        claimed.update(deck_ids)

        weight = float(entry.get("weight", 1.0))
        sources_by_position.append(
            (
                position,
                DeckSource(
                    name=name,
                    deck_ids=deck_ids,
                    weight=weight,
                    review_weight=weight,
                    new_weight=weight,
                ),
            )
        )

    sources = [source for _, source in sorted(sources_by_position, key=lambda p: p[0])]
    if not sources:
        raise NoDecksConfigured(
            "None of the decks selected in Settings exist anymore. "
            "Open Settings and choose your decks again."
        )
    return sources, False


def _load_pools(col: Any, source: DeckSource, include_cards: bool) -> None:
    review_query = _query_for_decks(source.deck_ids, constants.SEARCH_DUE)
    new_query = _query_for_decks(source.deck_ids, constants.SEARCH_NEW)

    review_ids = list(col.find_cards(review_query, order=constants.POOL_ORDER))
    new_ids = list(col.find_cards(new_query, order=constants.POOL_ORDER))

    source.available_review = len(review_ids)
    source.available_new = len(new_ids)
    if include_cards:
        source.review_pool = [int(cid) for cid in review_ids]
        source.new_pool = [int(cid) for cid in new_ids]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_mix(col: Any, config: dict, *, include_cards: bool = False) -> MixPlan:
    """Build a selection plan without touching the collection's decks."""
    cfg = normalize_config(config)
    target = int(cfg["daily_target"])
    done = get_daily_progress(col)
    remaining = max(0, target - done)

    sources, uses_all = resolve_sources(col, cfg)

    if remaining == 0:
        # Still load the pools so the dashboard can show what is waiting and a
        # meaningful per-deck summary even after the quota has been reached.
        for source in sources:
            _load_pools(col, source, include_cards=False)
        if uses_all:
            for source in sources:
                source.review_weight = float(max(source.available_review, 0))
                source.new_weight = float(max(source.available_new, 0))
        return MixPlan(
            target=target,
            done=done,
            remaining=0,
            review_target=0,
            new_target=0,
            sources=sources,
            uses_all_decks=uses_all,
            order_strategy=cfg["order_strategy"],
            review_percent=int(cfg["review_percent"]),
        )

    for source in sources:
        _load_pools(col, source, include_cards)

    # In all-decks mode there are no user weights.  Weight each deck group by
    # the size of its available pool so the mix reflects the actual workload.
    if uses_all:
        for source in sources:
            source.review_weight = float(max(source.available_review, 0))
            source.new_weight = float(max(source.available_new, 0))

    review_target = _round_half_up(remaining * int(cfg["review_percent"]) / 100)
    review_target = min(remaining, max(0, review_target))
    new_target = remaining - review_target

    review_alloc = allocate(
        review_target,
        [source.review_weight for source in sources],
        [source.available_review for source in sources],
    )
    new_alloc = allocate(
        new_target,
        [source.new_weight for source in sources],
        [source.available_new for source in sources],
    )

    for source, review_count, new_count in zip(sources, review_alloc, new_alloc):
        source.planned_review = review_count
        source.planned_new = new_count

    # Optional backfill: when one side runs out, top the session up from the
    # other side without exceeding the daily quota.
    if cfg["backfill"]:
        _backfill_pair(sources, review_target, new_target, remaining)

    review_cids: list[int] = []
    new_cids: list[int] = []
    if include_cards:
        for source in sources:
            source.picked_review = source.review_pool[: source.planned_review]
            source.picked_new = source.new_pool[: source.planned_new]
            review_cids.extend(source.picked_review)
            new_cids.extend(source.picked_new)

    return MixPlan(
        target=target,
        done=done,
        remaining=remaining,
        review_target=review_target,
        new_target=new_target,
        sources=sources,
        uses_all_decks=uses_all,
        order_strategy=cfg["order_strategy"],
        review_percent=int(cfg["review_percent"]),
        review_cids=review_cids,
        new_cids=new_cids,
    )


def _backfill_pair(
    sources: list[DeckSource],
    review_target: int,
    new_target: int,
    quota_remaining: int,
) -> None:
    """Replace an unavailable category with cards from the other category."""
    total = sum(source.planned_total for source in sources)

    # Reviews were scarce: use spare new cards.
    short_review = max(0, review_target - sum(s.planned_review for s in sources))
    if short_review and total < quota_remaining:
        extra_total = min(short_review, quota_remaining - total)
        residual = [max(0, s.available_new - s.planned_new) for s in sources]
        extra = allocate(
            extra_total,
            [s.new_weight for s in sources],
            residual,
        )
        for source, amount in zip(sources, extra):
            source.planned_new += amount
        total += sum(extra)

    # New cards were scarce: use spare reviews.
    short_new = max(0, new_target - sum(s.planned_new for s in sources))
    if short_new and total < quota_remaining:
        extra_total = min(short_new, quota_remaining - total)
        residual = [max(0, s.available_review - s.planned_review) for s in sources]
        extra = allocate(
            extra_total,
            [s.review_weight for s in sources],
            residual,
        )
        for source, amount in zip(sources, extra):
            source.planned_review += amount


# ---------------------------------------------------------------------------
# Filtered deck management
# ---------------------------------------------------------------------------


def _stored_mix_id(col: Any) -> int | None:
    try:
        value = col.get_config(constants.KEY_MIX_DECK_ID)
    except Exception:
        return None
    try:
        did = int(value)
    except (TypeError, ValueError):
        return None
    return did if did > 0 else None


def get_mix_deck_status(col: Any, config: dict | None = None) -> MixDeckStatus:
    did = _stored_mix_id(col)
    if did is None:
        return MixDeckStatus(id=None, name=constants.MIX_DECK_NAME, exists=False, card_count=0)

    deck = col.decks.get(did, default=False)
    if not deck or not deck.get("dyn"):
        return MixDeckStatus(id=None, name=constants.MIX_DECK_NAME, exists=False, card_count=0)

    try:
        count = int(col.db.scalar("select count() from cards where did=?", did) or 0)
    except Exception:
        count = 0
    return MixDeckStatus(id=did, name=deck.get("name") or constants.MIX_DECK_NAME, exists=True, card_count=count)


def resolve_mix_deck(col: Any, config: dict) -> tuple[int, str]:
    """Return ``(deck_id_or_zero, deck_name)`` for the session deck.

    An existing deck is reused so its id stays stable across sessions.  If a
    normal deck occupies the preferred name, friendly fallbacks are tried.
    """
    stored = _stored_mix_id(col)
    if stored is not None:
        deck = col.decks.get(stored, default=False)
        if deck and deck.get("dyn"):
            return stored, deck.get("name") or constants.MIX_DECK_NAME
        try:
            col.remove_config(constants.KEY_MIX_DECK_ID)
        except Exception:
            logger.debug("Could not clear stale mix deck id")

    configured = str(config.get("mix_deck_name") or constants.MIX_DECK_NAME).strip()
    candidates = [configured]
    for fallback in constants.MIX_DECK_FALLBACKS:
        if fallback not in candidates:
            candidates.append(fallback)

    for name in candidates:
        # Only a deck we remembered (above) is safe to reuse.  A pre-existing
        # deck with the same name may belong to the user, and overwriting it
        # would silently destroy their search terms.
        existing = col.decks.id_for_name(name)
        if existing is None:
            return 0, name

    raise MixDeckError(
        "Every candidate name for the daily filtered deck is already in use. "
        "Rename the conflicting deck or change 'mix_deck_name' in the add-on "
        "config."
    )


def _write_filtered_deck(
    col: Any,
    deck_id: int,
    name: str,
    card_ids: Sequence[int],
) -> int:
    """Create or update the filtered deck and return its id."""
    if not card_ids:
        raise NoCardsAvailable("No cards were selected for today's mix.")

    update = col.sched.get_or_create_filtered_deck(deck_id)
    update.name = name

    filtered = update.config
    # The single most important flag: answered cards are rescheduled in their
    # home deck, and FSRS/SM-2 state is updated by Anki's normal scheduler.
    filtered.reschedule = True
    del filtered.search_terms[:]

    term = filtered.search_terms.add()
    term.search = _cid_query(card_ids)
    term.limit = len(card_ids)
    # 1 == RANDOM in Anki's filtered-deck order enum.  We reorder explicitly
    # afterwards for both strategies, but this keeps the fallback pleasant.
    term.order = 1

    changes = col.sched.add_or_update_filtered_deck(update)
    new_id = int(changes.id)
    try:
        col.set_config(constants.KEY_MIX_DECK_ID, new_id)
    except Exception:
        logger.debug("Could not remember the mix deck id")
    return new_id


def _reorder_filtered_cards(col: Any, deck_id: int, plan: MixPlan) -> None:
    """Assign positions inside the temporary deck.

    Anki stores filtered-deck positions in ``cards.due`` and reads
    ``original_due`` for scheduling, so changing ``due`` here does not alter a
    card's interval, ease factor, or FSRS memory state.  Cards with an
    ``original_due`` of zero are skipped: Anki treats zero as "unset" and will
    not restore their due value when the filtered deck is emptied.
    """
    rows = list(col.db.all("select id, odid, odue from cards where did=?", deck_id))
    if len(rows) < 2:
        return

    actual: list[int] = []
    home_deck: dict[int, int] = {}
    restorable: dict[int, bool] = {}
    for cid, odid, odue in rows:
        cid_int = int(cid)
        actual.append(cid_int)
        home_deck[cid_int] = int(odid or 0)
        # Anki uses 0 as "no original due date".  Such cards (the first new
        # card, and any day-0 learning/review card) are deliberately left
        # alone when a filtered deck is emptied, so a synthetic position
        # written here would leak into the home deck.
        restorable[cid_int] = bool(int(odue or 0))

    if plan.order_strategy == constants.ORDER_SEQUENTIAL:
        wanted = [cid for cid in plan.sequential_cids() if cid in home_deck]
        wanted_set = set(wanted)
        ordered = wanted + [cid for cid in actual if cid not in wanted_set]
    else:
        groups: dict[int, list[int]] = {}
        for cid in actual:
            groups.setdefault(home_deck[cid], []).append(cid)

        keys = list(groups)
        random.shuffle(keys)
        for key in keys:
            random.shuffle(groups[key])

        ordered = []
        while True:
            progressed = False
            for key in keys:
                bucket = groups[key]
                if bucket:
                    ordered.append(bucket.pop(0))
                    progressed = True
            if not progressed:
                break

    cards = []
    for index, cid in enumerate(ordered):
        if not restorable.get(cid, False):
            continue
        card = col.get_card(cid)
        card.due = constants.POSITION_START + index
        cards.append(card)

    if not cards:
        return
    try:
        col.update_cards(cards, skip_undo_entry=True)
    except TypeError:
        # Older API without the keyword.
        col.update_cards(cards)


def cleanup_mix_deck(col: Any, config: dict | None = None) -> int:
    """Return every unanswered card in the mix deck to its home deck.

    Returns the number of cards that were returned.  Safe to call repeatedly
    and safe to call when the deck no longer exists.
    """
    did = _stored_mix_id(col)
    if did is None:
        return 0

    deck = col.decks.get(did, default=False)
    if not deck:
        try:
            col.remove_config(constants.KEY_MIX_DECK_ID)
        except Exception:
            pass
        return 0
    if not deck.get("dyn"):
        try:
            col.remove_config(constants.KEY_MIX_DECK_ID)
        except Exception:
            pass
        return 0

    try:
        before = int(col.db.scalar("select count() from cards where did=?", did) or 0)
        col.sched.empty_filtered_deck(did)
        logger.info("Returned %d card(s) from the Daily Mix deck", before)
        return before
    except Exception:
        logger.exception("Could not empty the Daily Mix filtered deck")
        return 0


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


def start_session(col: Any, config: dict) -> SessionResult:
    """Create/rebuild the filtered deck and select it.

    The caller is responsible for moving Anki to the review screen; keeping Qt
    out of the engine makes the selection logic testable.
    """
    cfg = normalize_config(config)
    done = get_daily_progress(col)
    if done >= int(cfg["daily_target"]):
        raise QuotaReached("You have already reached today's Daily Mix quota.")

    # Return cards from a previous build before planning, otherwise those cards
    # would be hidden from the `did:` searches by the filtered deck.
    try:
        cleanup_mix_deck(col, cfg)
    except Exception:
        logger.exception("Pre-session cleanup failed; continuing")

    plan = plan_mix(col, cfg, include_cards=True)
    if plan.remaining <= 0:
        raise QuotaReached("You have already reached today's Daily Mix quota.")
    if not plan.review_cids and not plan.new_cids:
        raise NoCardsAvailable(
            "There are no due or new cards in your selected decks right now."
        )

    deck_id, deck_name = resolve_mix_deck(col, cfg)
    card_ids = plan.review_cids + plan.new_cids
    # Safety net: never add a card twice if a future deck selection model
    # produces overlapping sources.
    card_ids = list(dict.fromkeys(card_ids))

    try:
        new_id = _write_filtered_deck(col, deck_id, deck_name, card_ids)
    except NoCardsAvailable:
        raise
    except Exception as exc:
        logger.exception("Anki refused to build the filtered deck")
        raise MixDeckError(
            "Anki could not build the Daily Mix filtered deck. "
            "Try running Check Database, then start the mix again."
        ) from exc

    try:
        _reorder_filtered_cards(col, new_id, plan)
    except Exception:
        # The deck is still perfectly usable with Anki's random order.
        logger.exception("Could not apply the custom card order")

    try:
        actual = int(col.db.scalar("select count() from cards where did=?", new_id) or 0)
    except Exception:
        actual = len(card_ids)

    return SessionResult(
        deck_id=new_id,
        deck_name=deck_name,
        card_count=actual,
        plan=plan,
    )


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------


def get_dashboard_data(col: Any, config: dict) -> dict:
    """Assemble the JSON payload consumed by web/script.js."""
    cfg = normalize_config(config)
    target = int(cfg["daily_target"])
    done = get_daily_progress(col)
    remaining = max(0, target - done)
    reset_seconds = get_seconds_until_reset(col)

    plan: MixPlan | None = None
    error: str | None = None
    try:
        plan = plan_mix(col, cfg)
    except DailyMixError as exc:
        error = str(exc)
    except Exception:
        logger.exception("Failed to build the dashboard plan")
        error = (
            "Daily Mix could not read your collection. "
            "Try closing this window and opening it again."
        )

    sources = plan.sources if plan else []
    deck_rows = [
        {
            "name": source.name,
            "weight": round(source.weight, 2),
            "planned": source.planned_total,
            "planned_review": source.planned_review,
            "planned_new": source.planned_new,
            "available_review": source.available_review,
            "available_new": source.available_new,
            "available_total": source.available_review + source.available_new,
        }
        for source in sources
        if source.available_review
        or source.available_new
        or source.planned_total
    ]

    planned_total = plan.planned_total if plan else 0
    planned_review = plan.planned_review if plan else 0
    planned_new = plan.planned_new if plan else 0
    available_review = plan.available_review if plan else 0
    available_new = plan.available_new if plan else 0
    quota_reached = done >= target
    can_start = bool(
        plan
        and not error
        and not quota_reached
        and remaining > 0
        and planned_total > 0
    )

    if quota_reached:
        status = "done"
    elif error:
        status = "error"
    elif not can_start:
        status = "empty"
    elif planned_total < remaining:
        status = "partial"
    else:
        status = "ready"

    mix = get_mix_deck_status(col, cfg)
    date_label = datetime.now().strftime("%A, %B %d")
    order_label = constants.ORDER_LABELS.get(
        cfg["order_strategy"], constants.ORDER_LABELS[constants.ORDER_INTERLEAVED]
    )

    return {
        "date_label": date_label,
        "target": target,
        "done": done,
        "remaining": remaining,
        "percent": min(100.0, (done / target * 100.0) if target else 0.0),
        "quota_reached": quota_reached,
        "review_percent": int(cfg["review_percent"]),
        "review_target": plan.review_target if plan else 0,
        "new_target": plan.new_target if plan else 0,
        "planned_review": planned_review,
        "planned_new": planned_new,
        "planned_total": planned_total,
        "available_review": available_review,
        "available_new": available_new,
        "total_available": available_review + available_new,
        "can_start": can_start,
        "status": status,
        "error": error,
        "order_strategy": cfg["order_strategy"],
        "order_label": order_label,
        "uses_all_decks": bool(plan.uses_all_decks) if plan else not cfg["decks"],
        "deck_count": len(deck_rows),
        "decks": deck_rows,
        "mix_deck": {
            "name": mix.name,
            "exists": mix.exists,
            "card_count": mix.card_count,
        },
        "reset_seconds": reset_seconds,
        "review_percent_label": int(cfg["review_percent"]),
        "cleanup_on_close": bool(cfg["cleanup_on_close"]),
        "shortcut": cfg["shortcut"],
        "version": constants.ADDON_VERSION,
    }
