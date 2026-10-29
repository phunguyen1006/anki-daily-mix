# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Constants shared by the Daily Mix add-on.

Keep this module free of Anki imports so it can be imported by tests and by the
config loader before a collection is open.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

ADDON_NAME = "Anki Daily Mix"
ADDON_VERSION = "1.0.0"

#: Default name for the temporary filtered deck. Anki shows filtered decks in
#: the deck list, so keep the name friendly and predictable.
MIX_DECK_NAME = "Daily Mix"

#: Used when a *normal* deck already occupies the preferred name.
MIX_DECK_FALLBACKS = (
    "Daily Mix (Filtered)",
    "Daily Mix (Auto)",
    "Daily Mix (Session)",
)

#: Collection-config key storing the filtered deck id.  The id is profile
#: specific, which is why it lives in the collection config rather than in the
#: (shared between profiles) add-on config.
KEY_MIX_DECK_ID = "dailyMixDeckId"

# ---------------------------------------------------------------------------
# Defaults (mirrored in config.json; constants are the fallback if the file is
# missing or unreadable).
# ---------------------------------------------------------------------------

DEFAULT_DAILY_TARGET = 100
DEFAULT_REVIEW_PERCENT = 80
DEFAULT_ORDER_STRATEGY = "interleaved"
DEFAULT_SHORTCUT = "Ctrl+Shift+D"

FALLBACK_CONFIG: dict = {
    "daily_target": DEFAULT_DAILY_TARGET,
    "review_percent": DEFAULT_REVIEW_PERCENT,
    "order_strategy": DEFAULT_ORDER_STRATEGY,
    "decks": [],
    "include_subdecks": True,
    "backfill": True,
    "cleanup_on_start": True,
    "cleanup_on_close": True,
    "mix_deck_name": MIX_DECK_NAME,
    "show_toolbar_button": True,
    "shortcut": DEFAULT_SHORTCUT,
}

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MIN_DAILY_TARGET = 1
MAX_DAILY_TARGET = 5000
MIN_DECK_WEIGHT = 0.1
MAX_DECK_WEIGHT = 10.0

# ---------------------------------------------------------------------------
# Search / scheduler
# ---------------------------------------------------------------------------

ORDER_INTERLEAVED = "interleaved"
ORDER_SEQUENTIAL = "sequential"
ORDER_STRATEGIES = (ORDER_INTERLEAVED, ORDER_SEQUENTIAL)
ORDER_LABELS = {
    ORDER_INTERLEAVED: "Interleaved",
    ORDER_SEQUENTIAL: "Sequential",
}

SEARCH_DUE = "(-is:new is:due)"
SEARCH_NEW = "is:new"
SEARCH_EXCLUDES = "-is:suspended -is:buried"

#: Sort pools by due date / new-card position.  Both columns are valid in the
#: cards table and keep the selection deterministic.
POOL_ORDER = "c.due asc, c.ord asc"

#: Filtered-deck card positions are stored in `cards.due`.  Anki's own filtered
#: deck builder starts at -100000; using the same range keeps our synthetic
#: positions consistent with what the scheduler expects.
POSITION_START = -100000

#: Anki revlog `type` values that represent actual study answers.  Type 3 is a
#: filtered-deck (cram) answer, 4 is a manual reschedule and 5 is a reschedule.
#: Counting 0-3 measures cards actually studied today.
REVLOG_ANSWER_TYPES = (0, 1, 2, 3)

SECONDS_PER_DAY = 86400

# ---------------------------------------------------------------------------
# Web bridge commands
# ---------------------------------------------------------------------------

CMD_START = "dailyMix:start"
CMD_SETTINGS = "dailyMix:settings"
CMD_REFRESH = "dailyMix:refresh"
CMD_CLEANUP = "dailyMix:cleanup"
CMD_CLOSE = "dailyMix:close"

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

#: Curated palette used by the dashboard.  Kept in Python only for documentation;
#: the actual colour assignment lives in web/script.js so the bar and the list
#: always agree.
DECK_COLORS = (
    "#8B7CFF",
    "#2DD4BF",
    "#F472B6",
    "#FBBF24",
    "#60A5FA",
    "#34D399",
    "#FB7185",
    "#A78BFA",
)
