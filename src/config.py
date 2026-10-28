# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Configuration loading, validation and persistence.

Anki's add-on manager already merges ``config.json`` with the user's saved
configuration.  We still perform a deep merge + normalisation because:

* older versions of the config may be missing newer fields;
* users may hand-edit ``meta.json`` / ``config.json``;
* the settings dialog and the engine need a stable, fully-populated schema.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from . import constants

logger = logging.getLogger(__name__)

_ADDON_ROOT = Path(__file__).resolve().parent.parent
_DEFAULTS_PATH = _ADDON_ROOT / "config.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def _as_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def _deep_merge(defaults: dict, user: dict) -> dict:
    """Recursively merge *user* over *defaults* without mutating either input."""
    result = copy.deepcopy(defaults)
    for key, value in user.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _normalise_decks(raw: Any) -> list[dict]:
    """Accept the modern list of objects and the friendly list of names."""
    if not isinstance(raw, list):
        return []

    result: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            name, weight = entry, 1.0
        elif isinstance(entry, dict):
            name = entry.get("name", "")
            weight = entry.get("weight", 1.0)
        else:
            continue

        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "weight": _as_float(
                    weight,
                    1.0,
                    constants.MIN_DECK_WEIGHT,
                    constants.MAX_DECK_WEIGHT,
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_config() -> dict:
    """Return a fresh copy of the packaged defaults."""
    try:
        with _DEFAULTS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return _deep_merge(constants.FALLBACK_CONFIG, data)
    except (OSError, ValueError):
        logger.warning("Could not read config.json; using built-in defaults")

    return copy.deepcopy(constants.FALLBACK_CONFIG)


def normalize_config(raw: dict | None) -> dict:
    """Validate and complete a user configuration.

    Unknown keys are preserved.  Numeric values are clamped to safe ranges so
    a hand-edited config can never build a gigantic filtered deck by accident.
    """
    defaults = default_config()
    user = raw if isinstance(raw, dict) else {}
    merged = _deep_merge(defaults, user)

    # Friendly aliases used by early builds / the mission brief.
    if "review_percent" not in user:
        if "review_ratio" in user:
            try:
                merged["review_percent"] = float(user["review_ratio"]) * 100
            except (TypeError, ValueError):
                pass
        elif "ratio" in user:
            try:
                merged["review_percent"] = float(user["ratio"]) * 100
            except (TypeError, ValueError):
                pass

    if "daily_target" not in user:
        for alias in ("quota", "target"):
            if alias in user:
                merged["daily_target"] = user[alias]
                break

    if "order_strategy" not in user and "order" in user:
        merged["order_strategy"] = user["order"]

    merged["daily_target"] = _as_int(
        merged.get("daily_target"),
        constants.DEFAULT_DAILY_TARGET,
        constants.MIN_DAILY_TARGET,
        constants.MAX_DAILY_TARGET,
    )
    merged["review_percent"] = _as_int(
        merged.get("review_percent"),
        constants.DEFAULT_REVIEW_PERCENT,
        0,
        100,
    )

    strategy = str(merged.get("order_strategy", "")).strip().lower()
    strategy = {
        "random": constants.ORDER_INTERLEAVED,
        "shuffle": constants.ORDER_INTERLEAVED,
        "mixed": constants.ORDER_INTERLEAVED,
        "by_deck": constants.ORDER_SEQUENTIAL,
        "by deck": constants.ORDER_SEQUENTIAL,
        "deck": constants.ORDER_SEQUENTIAL,
    }.get(strategy, strategy)
    if strategy not in constants.ORDER_STRATEGIES:
        strategy = constants.DEFAULT_ORDER_STRATEGY
    merged["order_strategy"] = strategy

    merged["decks"] = _normalise_decks(merged.get("decks"))

    for key, default in (
        ("include_subdecks", True),
        ("backfill", True),
        ("cleanup_on_start", True),
        ("cleanup_on_close", True),
        ("show_toolbar_button", True),
    ):
        merged[key] = _as_bool(merged.get(key), default)

    name = merged.get("mix_deck_name")
    if not isinstance(name, str) or not name.strip():
        name = constants.MIX_DECK_NAME
    merged["mix_deck_name"] = name.strip()

    shortcut = merged.get("shortcut")
    if not isinstance(shortcut, str):
        shortcut = constants.DEFAULT_SHORTCUT
    # An empty string is intentional: it disables the global shortcut.
    merged["shortcut"] = shortcut.strip()

    return merged


def load_config() -> dict:
    """Load the current user config, falling back to packaged defaults."""
    try:
        from aqt import mw  # imported lazily so this module stays testable
    except Exception:  # pragma: no cover - Anki is always available in-app
        return normalize_config(None)

    if mw is None or getattr(mw, "addonManager", None) is None:
        return normalize_config(None)

    try:
        raw = mw.addonManager.getConfig(__name__)
    except Exception:
        logger.exception("Failed to read add-on config")
        raw = None

    return normalize_config(raw)


def save_config(config: dict) -> dict:
    """Normalise and persist *config*, returning the normalised version."""
    from aqt import mw

    normalized = normalize_config(config)
    if mw is None or getattr(mw, "addonManager", None) is None:
        raise RuntimeError("Anki's add-on manager is not available")

    mw.addonManager.writeConfig(__name__, normalized)
    logger.info("Daily Mix settings saved")
    return normalized


def config_as_json(config: dict) -> str:
    """Pretty JSON used by the native config editor / debugging."""
    return json.dumps(normalize_config(config), ensure_ascii=False, indent=2)
