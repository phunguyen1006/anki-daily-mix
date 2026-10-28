# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Unit tests for the pure-Python side of Daily Mix.

The tests use a small in-memory fake of the parts of ``Collection`` the engine
touches.  This keeps the suite fast and runnable with a plain Python
interpreter, without Anki, Qt, or a real collection file.
"""

from __future__ import annotations

import random
import unittest
from types import SimpleNamespace

from src import constants
from src.config import normalize_config
from src.engine import (
    MixDeckError,
    NoCardsAvailable,
    NoDecksConfigured,
    QuotaReached,
    allocate,
    cleanup_mix_deck,
    get_dashboard_data,
    get_daily_progress,
    get_mix_deck_status,
    plan_mix,
    resolve_mix_deck,
    resolve_sources,
    start_session,
)


# ---------------------------------------------------------------------------
# Fake collection
# ---------------------------------------------------------------------------


class FakeCard:
    def __init__(self, cid: int, did: int, kind: str, due: int) -> None:
        self.id = cid
        self.did = did
        self.odid = 0
        self.kind = kind
        self.due = due
        self.original_due = due
        self.suspended = False
        self.buried = False


class FakeTerms:
    """Minimal stand-in for a protobuf repeated field."""

    def __init__(self) -> None:
        self.items: list[SimpleNamespace] = []

    def add(self) -> SimpleNamespace:
        term = SimpleNamespace(search="", limit=0, order=0)
        self.items.append(term)
        return term

    def __delitem__(self, index: slice) -> None:
        del self.items[index]

    def __len__(self) -> int:
        return len(self.items)


class FakeFilteredConfig:
    def __init__(self) -> None:
        self.reschedule = False
        self.search_terms = FakeTerms()


class FakeFilteredUpdate:
    def __init__(self, deck_id: int, name: str = "") -> None:
        self.id = deck_id
        self.name = name
        self.config = FakeFilteredConfig()


class FakeDeckManager:
    def __init__(self) -> None:
        self._next_id = 1
        self._decks: list[dict] = []

    def add(self, name: str, dyn: bool = False) -> int:
        did = self._next_id
        self._next_id += 1
        self._decks.append({"id": did, "name": name, "dyn": dyn})
        return did

    def all_names_and_ids(self, include_filtered: bool = False):
        entries = [
            deck
            for deck in self._decks
            if include_filtered or not deck["dyn"]
        ]
        return [
            SimpleNamespace(id=deck["id"], name=deck["name"])
            for deck in sorted(entries, key=lambda item: item["name"])
        ]

    def deck_and_child_ids(self, deck_id: int) -> list[int]:
        deck = self.get(deck_id, default=False)
        if not deck:
            return []
        base = deck["name"]
        return [
            entry["id"]
            for entry in self._decks
            if not entry["dyn"]
            and (
                entry["name"] == base
                or entry["name"].startswith(base + "::")
            )
        ]

    def get(self, deck_id: int, default=None):
        for deck in self._decks:
            if deck["id"] == deck_id:
                return dict(deck)
        return default

    def id_for_name(self, name: str):
        for deck in self._decks:
            if deck["name"] == name:
                return deck["id"]
        return None

    def by_name(self, name: str):
        did = self.id_for_name(name)
        return self.get(did, default=False) if did is not None else False


class FakeDB:
    def __init__(self, col: "FakeCollection") -> None:
        self.col = col

    def scalar(self, sql: str, *args):
        query = " ".join(sql.split())
        if "count(distinct cid) from revlog" in query:
            start = int(args[0])
            answered = {
                entry["cid"]
                for entry in self.col.revlog
                if entry["ts"] > start and entry["type"] in constants.REVLOG_ANSWER_TYPES
            }
            return len(answered)
        if query.startswith("select count() from cards where did=?"):
            return sum(1 for card in self.col.cards.values() if card.did == int(args[0]))
        raise AssertionError("Unexpected scalar query: %s" % query)

    def all(self, sql: str, *args):
        query = " ".join(sql.split())
        if query == "select id, odid, odue from cards where did=?":
            did = int(args[0])
            cards = [card for card in self.col.cards.values() if card.did == did]
            cards.sort(key=lambda card: (card.due, card.id))
            return [(card.id, card.odid, card.original_due) for card in cards]
        raise AssertionError("Unexpected all query: %s" % query)


class FakeScheduler:
    def __init__(self, col: "FakeCollection") -> None:
        self.col = col
        self.day_cutoff = 1_700_000_000
        self.last_update: FakeFilteredUpdate | None = None

    def get_or_create_filtered_deck(self, deck_id: int) -> FakeFilteredUpdate:
        deck = self.col.decks.get(deck_id, default=False)
        if deck and deck.get("dyn"):
            return FakeFilteredUpdate(deck_id, deck["name"])
        return FakeFilteredUpdate(0)

    def add_or_update_filtered_deck(self, update: FakeFilteredUpdate):
        if update.id:
            deck = self.col.decks.get(update.id, default=False)
            if not deck or not deck.get("dyn"):
                raise AssertionError("Tried to update a non-filtered deck")
            deck_id = update.id
        else:
            deck_id = self.col.decks.add(update.name or constants.MIX_DECK_NAME, dyn=True)

        # Anki rebuilds the deck from scratch on every update.
        self.empty_filtered_deck(deck_id)

        moved: list[int] = []
        for term in update.config.search_terms.items:
            search = str(term.search or "")
            if not search.startswith("cid:"):
                raise AssertionError("Only cid: terms are expected in tests")
            cids = [int(part) for part in search[4:].split(",") if part]
            for cid in cids[: int(term.limit or len(cids))]:
                card = self.col.cards.get(cid)
                if card is None or card.did == deck_id or cid in moved:
                    continue
                card.odid = card.did
                card.original_due = card.due
                card.did = deck_id
                # Anki only assigns a filtered position when due is positive;
                # a new card at position 0 keeps its real due of 0.
                if card.due > 0:
                    card.due = constants.POSITION_START + len(moved)
                moved.append(cid)

        self.last_update = update
        return SimpleNamespace(id=deck_id)

    def empty_filtered_deck(self, deck_id: int) -> int:
        returned = 0
        for card in self.col.cards.values():
            if card.did != deck_id:
                continue
            card.did = card.odid or 1
            # Anki restores due only when an original due was recorded.
            if card.original_due:
                card.due = card.original_due
            card.odid = 0
            card.original_due = 0
            returned += 1
        return returned


class FakeCollection:
    def __init__(self, *, default_deck: bool = True) -> None:
        self.decks = FakeDeckManager()
        if default_deck:
            self.decks.add("Default")
        self.cards: dict[int, FakeCard] = {}
        self.revlog: list[dict] = []
        self._config: dict = {}
        self._next_cid = 100
        self.db = FakeDB(self)
        self.sched = FakeScheduler(self)

    def add_deck(self, name: str, dyn: bool = False) -> int:
        return self.decks.add(name, dyn)

    def add_card(
        self,
        deck_id: int,
        kind: str = "review",
        due: int | None = None,
    ) -> int:
        cid = self._next_cid
        self._next_cid += 1
        if due is None:
            due = cid
        card = FakeCard(cid, deck_id, kind, due)
        self.cards[cid] = card
        return cid

    def answer(self, count: int = 1) -> None:
        """Record distinct cards answered today."""
        timestamp = (self.sched.day_cutoff - 60) * 1000
        for index in range(count):
            self.revlog.append(
                {"cid": 900_000 + index, "ts": timestamp, "type": 1}
            )

    def find_cards(self, query: str, order: str = "") -> list[int]:
        tokens = query.split()
        cids: list[int] | None = None
        dids: list[int] = []
        state = ""
        for token in tokens:
            if token.startswith("cid:"):
                cids = [int(part) for part in token[4:].split(",") if part]
            elif token.startswith("did:"):
                dids = [int(part) for part in token[4:].split(",") if part]
            elif token == "is:due":
                state = "review"
            elif token == "is:new":
                state = "new"

        cards = list(self.cards.values())
        if cids is not None:
            wanted = set(cids)
            cards = [card for card in cards if card.id in wanted]
        if dids:
            allowed = set(dids)
            cards = [card for card in cards if card.did in allowed]
        if state == "new":
            cards = [card for card in cards if card.kind == "new"]
        elif state == "review":
            cards = [card for card in cards if card.kind != "new"]
        cards = [card for card in cards if not card.suspended and not card.buried]
        cards.sort(key=lambda card: (card.due, card.id))
        return [card.id for card in cards]

    def get_card(self, cid: int) -> FakeCard:
        return self.cards[cid]

    def update_cards(self, cards, skip_undo_entry: bool = False) -> None:
        for card in cards:
            self.cards[card.id] = card

    def get_config(self, key: str):
        return self._config.get(key)

    def set_config(self, key: str, value) -> None:
        self._config[key] = value

    def remove_config(self, key: str) -> None:
        self._config.pop(key, None)


# ---------------------------------------------------------------------------
# Pure helpers / config
# ---------------------------------------------------------------------------


class AllocateTests(unittest.TestCase):
    def test_equal_weights_split_evenly(self) -> None:
        self.assertEqual(allocate(5, [1.0, 1.0], [10, 10]), [3, 2])

    def test_capacity_is_redistributed(self) -> None:
        self.assertEqual(allocate(10, [1.0, 1.0], [2, 10]), [2, 8])

    def test_respects_weights(self) -> None:
        result = allocate(10, [3.0, 1.0], [100, 100])
        self.assertEqual(sum(result), 10)
        self.assertGreater(result[0], result[1])

    def test_zero_weight_gets_nothing(self) -> None:
        self.assertEqual(allocate(5, [0.0, 1.0], [10, 10]), [0, 5])

    def test_zero_total(self) -> None:
        self.assertEqual(allocate(0, [1.0, 1.0], [10, 10]), [0, 0])

    def test_no_capacity(self) -> None:
        self.assertEqual(allocate(5, [1.0, 1.0], [0, 0]), [0, 0])


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = normalize_config(None)
        self.assertEqual(cfg["daily_target"], constants.DEFAULT_DAILY_TARGET)
        self.assertEqual(cfg["review_percent"], constants.DEFAULT_REVIEW_PERCENT)
        self.assertEqual(cfg["order_strategy"], constants.ORDER_INTERLEAVED)
        self.assertEqual(cfg["decks"], [])
        self.assertEqual(cfg["shortcut"], constants.DEFAULT_SHORTCUT)

    def test_clamps_numbers_and_preserves_empty_shortcut(self) -> None:
        cfg = normalize_config(
            {
                "daily_target": 99_999,
                "review_percent": -4,
                "shortcut": "   ",
            }
        )
        self.assertEqual(cfg["daily_target"], constants.MAX_DAILY_TARGET)
        self.assertEqual(cfg["review_percent"], 0)
        self.assertEqual(cfg["shortcut"], "")

    def test_aliases(self) -> None:
        cfg = normalize_config(
            {
                "quota": 40,
                "review_ratio": 0.75,
                "order": "by_deck",
            }
        )
        self.assertEqual(cfg["daily_target"], 40)
        self.assertEqual(cfg["review_percent"], 75)
        self.assertEqual(cfg["order_strategy"], constants.ORDER_SEQUENTIAL)

    def test_unknown_order_falls_back(self) -> None:
        self.assertEqual(
            normalize_config({"order_strategy": "sideways"})["order_strategy"],
            constants.DEFAULT_ORDER_STRATEGY,
        )

    def test_deck_names_are_normalised(self) -> None:
        cfg = normalize_config(
            {
                "decks": [
                    "  Spanish  ",
                    {"name": "Spanish", "weight": 5},
                    {"name": "Japanese", "weight": 999},
                    {"name": "", "weight": 1},
                    "Japanese",
                ]
            }
        )
        # The first occurrence wins; later duplicates are dropped entirely.
        self.assertEqual(
            cfg["decks"],
            [
                {"name": "Spanish", "weight": 1.0},
                {"name": "Japanese", "weight": constants.MAX_DECK_WEIGHT},
            ],
        )

    def test_boolean_strings(self) -> None:
        cfg = normalize_config({"include_subdecks": "no", "backfill": "yes"})
        self.assertFalse(cfg["include_subdecks"])
        self.assertTrue(cfg["backfill"])


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------


class SourceTests(unittest.TestCase):
    def test_all_decks_groups_by_top_level_deck(self) -> None:
        col = FakeCollection()
        parent = col.add_deck("Languages")
        child = col.add_deck("Languages::Spanish")
        other = col.add_deck("Science")

        sources, uses_all = resolve_sources(col, normalize_config(None))

        self.assertTrue(uses_all)
        by_name = {source.name: source for source in sources}
        self.assertEqual(set(by_name), {"Default", "Languages", "Science"})
        self.assertEqual(by_name["Languages"].deck_ids, [parent, child])
        self.assertEqual(by_name["Science"].deck_ids, [other])

    def test_deeper_selected_deck_is_claimed_first(self) -> None:
        col = FakeCollection()
        parent = col.add_deck("Languages")
        child = col.add_deck("Languages::Spanish")

        cfg = normalize_config(
            {
                "decks": [
                    {"name": "Languages", "weight": 1},
                    {"name": "Languages::Spanish", "weight": 3},
                ]
            }
        )
        sources, uses_all = resolve_sources(col, cfg)

        self.assertFalse(uses_all)
        self.assertEqual([source.name for source in sources], ["Languages", "Languages::Spanish"])
        self.assertEqual(sources[0].deck_ids, [parent])
        self.assertEqual(sources[1].deck_ids, [child])
        self.assertEqual(sources[1].weight, 3.0)

    def test_subdecks_can_be_excluded(self) -> None:
        col = FakeCollection()
        parent = col.add_deck("Languages")
        col.add_deck("Languages::Spanish")

        cfg = normalize_config(
            {
                "decks": [{"name": "Languages", "weight": 1}],
                "include_subdecks": False,
            }
        )
        sources, _ = resolve_sources(col, cfg)
        self.assertEqual(sources[0].deck_ids, [parent])

    def test_missing_decks_are_ignored(self) -> None:
        col = FakeCollection()
        real = col.add_deck("Real")
        cfg = normalize_config(
            {
                "decks": [
                    {"name": "Deleted", "weight": 1},
                    {"name": "Real", "weight": 2},
                ]
            }
        )
        sources, _ = resolve_sources(col, cfg)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].deck_ids, [real])

    def test_no_matching_decks_raises(self) -> None:
        col = FakeCollection()
        cfg = normalize_config({"decks": [{"name": "Deleted", "weight": 1}]})
        with self.assertRaises(NoDecksConfigured):
            resolve_sources(col, cfg)

    def test_empty_collection_raises(self) -> None:
        col = FakeCollection(default_deck=False)
        with self.assertRaises(NoDecksConfigured):
            resolve_sources(col, normalize_config(None))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class PlanningTests(unittest.TestCase):
    def _deck_with_cards(
        self,
        col: FakeCollection,
        name: str = "Deck",
        reviews: int = 0,
        new: int = 0,
    ) -> int:
        did = col.add_deck(name)
        for index in range(reviews):
            col.add_card(did, kind="review", due=index)
        for index in range(new):
            col.add_card(did, kind="new", due=index)
        return did

    def _selected(self, name: str, weight: float = 1.0) -> dict:
        return normalize_config(
            {
                "daily_target": 10,
                "review_percent": 80,
                "backfill": False,
                "include_subdecks": False,
                "decks": [{"name": name, "weight": weight}],
            }
        )

    def test_ratio_selects_reviews_and_new_cards(self) -> None:
        col = FakeCollection()
        did = self._deck_with_cards(col, "Deck", reviews=20, new=10)
        plan = plan_mix(col, self._selected("Deck"), include_cards=True)

        self.assertEqual(plan.remaining, 10)
        self.assertEqual((plan.review_target, plan.new_target), (8, 2))
        self.assertEqual((plan.planned_review, plan.planned_new), (8, 2))
        self.assertEqual(len(plan.review_cids), 8)
        self.assertEqual(len(plan.new_cids), 2)
        self.assertTrue(all(col.cards[cid].did == did for cid in plan.review_cids))

    def test_backfill_uses_new_cards_when_reviews_are_missing(self) -> None:
        col = FakeCollection()
        self._deck_with_cards(col, "Deck", reviews=0, new=30)
        cfg = self._selected("Deck")
        cfg["backfill"] = True

        plan = plan_mix(col, cfg, include_cards=True)
        self.assertEqual((plan.planned_review, plan.planned_new), (0, 10))
        self.assertEqual(plan.planned_total, 10)

    def test_without_backfill_a_short_side_stays_short(self) -> None:
        col = FakeCollection()
        self._deck_with_cards(col, "Deck", reviews=0, new=30)
        plan = plan_mix(col, self._selected("Deck"), include_cards=True)
        self.assertEqual((plan.planned_review, plan.planned_new), (0, 2))
        self.assertEqual(plan.planned_total, 2)

    def test_weight_is_relative(self) -> None:
        col = FakeCollection()
        self._deck_with_cards(col, "Big", reviews=100)
        self._deck_with_cards(col, "Small", reviews=100)
        cfg = normalize_config(
            {
                "daily_target": 10,
                "review_percent": 100,
                "backfill": False,
                "include_subdecks": False,
                "decks": [
                    {"name": "Big", "weight": 3},
                    {"name": "Small", "weight": 1},
                ],
            }
        )
        plan = plan_mix(col, cfg)
        by_name = {source.name: source for source in plan.sources}
        self.assertEqual(plan.planned_total, 10)
        self.assertGreater(by_name["Big"].planned_review, by_name["Small"].planned_review)

    def test_all_decks_mode_weights_by_available_work(self) -> None:
        col = FakeCollection(default_deck=False)
        self._deck_with_cards(col, "Lots", reviews=9)
        self._deck_with_cards(col, "Few", reviews=1)
        cfg = normalize_config(
            {
                "daily_target": 10,
                "review_percent": 100,
                "backfill": False,
                "decks": [],
            }
        )
        plan = plan_mix(col, cfg)
        by_name = {source.name: source for source in plan.sources}
        self.assertTrue(plan.uses_all_decks)
        self.assertEqual(by_name["Lots"].planned_review, 9)
        self.assertEqual(by_name["Few"].planned_review, 1)

    def test_quota_already_met_still_loads_availability(self) -> None:
        col = FakeCollection()
        self._deck_with_cards(col, "Deck", reviews=7, new=3)
        col.answer(10)
        cfg = normalize_config(
            {"daily_target": 10, "decks": [{"name": "Deck", "weight": 1}]}
        )

        plan = plan_mix(col, cfg)
        self.assertEqual(plan.done, 10)
        self.assertEqual(plan.remaining, 0)
        self.assertEqual(plan.planned_total, 0)
        self.assertEqual(plan.available_review, 7)
        self.assertEqual(plan.available_new, 3)

    def test_fewer_cards_than_quota(self) -> None:
        col = FakeCollection()
        self._deck_with_cards(col, "Deck", reviews=3, new=1)
        plan = plan_mix(col, self._selected("Deck"), include_cards=True)
        self.assertEqual(plan.planned_total, 4)
        self.assertEqual(plan.remaining, 10)


# ---------------------------------------------------------------------------
# Filtered deck lifecycle
# ---------------------------------------------------------------------------


class SessionTests(unittest.TestCase):
    def _collection(self) -> FakeCollection:
        return FakeCollection()

    def _selected(
        self,
        names: list[str],
        *,
        target: int = 6,
        order: str = constants.ORDER_SEQUENTIAL,
        review_percent: int = 100,
    ) -> dict:
        return normalize_config(
            {
                "daily_target": target,
                "review_percent": review_percent,
                "backfill": False,
                "include_subdecks": False,
                "order_strategy": order,
                "decks": [{"name": name, "weight": 1} for name in names],
            }
        )

    def test_start_builds_a_rescheduling_filtered_deck(self) -> None:
        col = self._collection()
        deck = col.add_deck("Deck")
        for index in range(6):
            col.add_card(deck, kind="review", due=index)

        result = start_session(col, self._selected(["Deck"]))
        deck_data = col.decks.get(result.deck_id)

        self.assertTrue(deck_data["dyn"])
        self.assertEqual(result.deck_name, constants.MIX_DECK_NAME)
        self.assertEqual(result.card_count, 6)
        self.assertEqual(result.plan.planned_total, 6)
        self.assertIsNotNone(col.sched.last_update)
        update = col.sched.last_update
        assert update is not None
        self.assertTrue(update.config.reschedule)
        self.assertEqual(len(update.config.search_terms.items), 1)
        term = update.config.search_terms.items[0]
        self.assertTrue(term.search.startswith("cid:"))
        self.assertEqual(term.limit, 6)
        self.assertEqual(term.order, 1)
        self.assertEqual(col.get_config(constants.KEY_MIX_DECK_ID), result.deck_id)

    def test_sequential_order_follows_configured_deck_order(self) -> None:
        col = self._collection()
        first = col.add_deck("First")
        second = col.add_deck("Second")
        first_cards = [
            col.add_card(first, kind="review", due=index + 1) for index in range(3)
        ]
        second_cards = [
            col.add_card(second, kind="review", due=index + 1) for index in range(3)
        ]

        result = start_session(
            col,
            self._selected(["Second", "First"], order=constants.ORDER_SEQUENTIAL),
        )
        ordered = [
            cid
            for cid, _ in sorted(
                [(card.id, card.due) for card in col.cards.values() if card.did == result.deck_id],
                key=lambda row: row[1],
            )
        ]
        self.assertEqual(ordered, second_cards + first_cards)

    def test_zero_original_due_is_never_overwritten(self) -> None:
        col = self._collection()
        deck = col.add_deck("Deck")
        first = col.add_card(deck, kind="new", due=0)
        second = col.add_card(deck, kind="new", due=1)

        result = start_session(
            col,
            self._selected(
                ["Deck"],
                target=2,
                order=constants.ORDER_SEQUENTIAL,
                review_percent=0,
            ),
        )
        self.assertEqual(result.card_count, 2)
        # Anki uses 0 as "no original due", so the first new card must keep its
        # real position instead of receiving a synthetic filtered-deck one.
        self.assertEqual(col.cards[first].due, 0)
        self.assertLess(col.cards[second].due, 0)

        cleanup_mix_deck(col)
        self.assertEqual(col.cards[first].due, 0)
        self.assertEqual(col.cards[second].due, 1)

    def test_interleaved_order_alternates_between_decks(self) -> None:
        col = self._collection()
        first = col.add_deck("First")
        second = col.add_deck("Second")
        for index in range(4):
            col.add_card(first, kind="review", due=index)
        for index in range(4):
            col.add_card(second, kind="review", due=index)

        random.seed(1234)
        result = start_session(
            col,
            self._selected(["First", "Second"], order=constants.ORDER_INTERLEAVED, target=8),
        )
        ordered = [
            card.odid
            for card in sorted(
                (card for card in col.cards.values() if card.did == result.deck_id),
                key=lambda card: card.due,
            )
        ]
        self.assertEqual(len(ordered), 8)
        self.assertEqual(set(ordered), {first, second})
        for index in range(0, len(ordered) - 1, 2):
            self.assertNotEqual(ordered[index], ordered[index + 1])
        self.assertEqual(ordered[0], ordered[2])
        self.assertEqual(ordered[1], ordered[3])

    def test_quota_reached_is_reported(self) -> None:
        col = self._collection()
        deck = col.add_deck("Deck")
        col.add_card(deck, kind="review")
        col.answer(1)
        with self.assertRaises(QuotaReached):
            start_session(col, self._selected(["Deck"], target=1))

    def test_no_cards_raises(self) -> None:
        col = self._collection()
        col.add_deck("Deck")
        with self.assertRaises(NoCardsAvailable):
            start_session(col, self._selected(["Deck"]))

    def test_stale_deck_is_cleaned_before_replanning(self) -> None:
        col = self._collection()
        deck = col.add_deck("Deck")
        for index in range(6):
            col.add_card(deck, kind="review", due=index)

        first = start_session(col, self._selected(["Deck"]))
        # Simulate an abrupt close: cards stay in the filtered deck.
        self.assertEqual(
            sum(1 for card in col.cards.values() if card.did == first.deck_id), 6
        )

        second = start_session(col, self._selected(["Deck"]))
        self.assertEqual(second.deck_id, first.deck_id)
        self.assertEqual(
            sum(1 for card in col.cards.values() if card.did == second.deck_id), 6
        )

    def test_cleanup_returns_cards_and_restores_due_dates(self) -> None:
        col = self._collection()
        deck = col.add_deck("Deck")
        original = [
            (cid, col.cards[cid].due)
            for cid in (col.add_card(deck, kind="review", due=index) for index in range(4))
        ]

        result = start_session(col, self._selected(["Deck"], target=4))
        returned = cleanup_mix_deck(col)
        self.assertEqual(returned, 4)
        status = get_mix_deck_status(col)
        self.assertTrue(status.exists)
        self.assertEqual(status.card_count, 0)

        for cid, due in original:
            card = col.cards[cid]
            self.assertEqual(card.did, deck)
            self.assertEqual(card.due, due)
            self.assertEqual(card.odid, 0)

        # Running cleanup twice is harmless.
        self.assertEqual(cleanup_mix_deck(col), 0)
        self.assertIsNotNone(result.deck_id)

    def test_cleanup_ignores_a_normal_deck(self) -> None:
        col = self._collection()
        normal = col.add_deck("Not filtered")
        col.set_config(constants.KEY_MIX_DECK_ID, normal)
        self.assertEqual(cleanup_mix_deck(col), 0)
        self.assertIsNone(col.get_config(constants.KEY_MIX_DECK_ID))

    def test_mix_deck_name_conflicts_use_fallback(self) -> None:
        col = self._collection()
        col.add_deck(constants.MIX_DECK_NAME)
        deck_id, name = resolve_mix_deck(
            col, normalize_config({"mix_deck_name": constants.MIX_DECK_NAME})
        )
        self.assertEqual(deck_id, 0)
        self.assertEqual(name, constants.MIX_DECK_FALLBACKS[0])

    def test_unowned_filtered_deck_is_not_hijacked(self) -> None:
        col = self._collection()
        col.add_deck(constants.MIX_DECK_NAME, dyn=True)
        deck_id, name = resolve_mix_deck(
            col, normalize_config({"mix_deck_name": constants.MIX_DECK_NAME})
        )
        self.assertEqual(deck_id, 0)
        self.assertEqual(name, constants.MIX_DECK_FALLBACKS[0])

    def test_all_name_conflicts_raise(self) -> None:
        col = self._collection()
        for name in (constants.MIX_DECK_NAME, *constants.MIX_DECK_FALLBACKS):
            col.add_deck(name)
        with self.assertRaises(MixDeckError):
            resolve_mix_deck(col, normalize_config(None))

    def test_existing_filtered_deck_is_reused(self) -> None:
        col = self._collection()
        existing = col.add_deck("My Mix", dyn=True)
        col.set_config(constants.KEY_MIX_DECK_ID, existing)
        deck_id, name = resolve_mix_deck(col, normalize_config(None))
        self.assertEqual((deck_id, name), (existing, "My Mix"))


# ---------------------------------------------------------------------------
# Dashboard payload
# ---------------------------------------------------------------------------


class DashboardTests(unittest.TestCase):
    def test_progress_uses_day_cutoff(self) -> None:
        col = FakeCollection()
        col.answer(3)
        self.assertEqual(get_daily_progress(col), 3)

    def test_ready_payload(self) -> None:
        col = FakeCollection()
        deck = col.add_deck("Deck")
        for index in range(20):
            col.add_card(deck, kind="review", due=index)

        data = get_dashboard_data(
            col,
            normalize_config(
                {
                    "daily_target": 10,
                    "decks": [{"name": "Deck", "weight": 1}],
                }
            ),
        )
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["can_start"])
        self.assertEqual(data["planned_total"], 10)
        # Backfill is on by default, so the missing new cards are replaced by
        # two spare reviews.
        self.assertEqual(data["planned_review"], 10)
        self.assertEqual(data["planned_new"], 0)
        self.assertEqual(data["deck_count"], 1)
        self.assertEqual(data["percent"], 0.0)
        self.assertIn("version", data)

    def test_partial_payload(self) -> None:
        col = FakeCollection()
        deck = col.add_deck("Deck")
        for index in range(4):
            col.add_card(deck, kind="review", due=index)

        data = get_dashboard_data(
            col,
            normalize_config(
                {"daily_target": 10, "decks": [{"name": "Deck", "weight": 1}]}
            ),
        )
        self.assertEqual(data["status"], "partial")
        self.assertTrue(data["can_start"])
        self.assertEqual(data["planned_total"], 4)

    def test_empty_payload(self) -> None:
        col = FakeCollection()
        col.add_deck("Deck")
        data = get_dashboard_data(
            col,
            normalize_config({"decks": [{"name": "Deck", "weight": 1}]}),
        )
        self.assertEqual(data["status"], "empty")
        self.assertFalse(data["can_start"])
        self.assertEqual(data["planned_total"], 0)

    def test_done_payload_still_reports_availability(self) -> None:
        col = FakeCollection()
        deck = col.add_deck("Deck")
        for index in range(7):
            col.add_card(deck, kind="review", due=index)
        col.answer(5)

        data = get_dashboard_data(
            col,
            normalize_config(
                {"daily_target": 5, "decks": [{"name": "Deck", "weight": 1}]}
            ),
        )
        self.assertEqual(data["status"], "done")
        self.assertTrue(data["quota_reached"])
        self.assertFalse(data["can_start"])
        self.assertEqual(data["done"], 5)
        self.assertEqual(data["remaining"], 0)
        self.assertEqual(data["available_review"], 7)
        self.assertEqual(data["percent"], 100.0)

    def test_error_payload_for_missing_decks(self) -> None:
        col = FakeCollection()
        data = get_dashboard_data(
            col,
            normalize_config({"decks": [{"name": "Deleted", "weight": 1}]}),
        )
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["can_start"])
        self.assertIsInstance(data["error"], str)
        self.assertTrue(data["error"])


if __name__ == "__main__":
    unittest.main()
