# Anki Daily Mix

[![Version](https://img.shields.io/badge/version-1.0.1-blue.svg)](https://github.com/phunguyen1006/anki-daily-mix/releases/tag/v1.0.1)
[![Anki](https://img.shields.io/badge/anki-%3E%3D%2023.10-success.svg)](https://apps.ankiweb.net/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-purple.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)](tests/)

A daily, deck-spanning study session for Anki 23.10 and newer.

Daily Mix gathers due reviews and new cards from the decks you choose, builds a
temporary filtered deck, and opens it for study. One quota per day, one click,
and Anki's own scheduler handles the rest.

## Features

- A **Daily Mix** entry in the Tools menu, the toolbar, and an optional global
  shortcut (`Ctrl+Shift+D` by default).
- A dashboard with today's quota, progress ring, due/new breakdown, per-deck
  allocation, the current mix deck, and one large **Start Daily Mix** button
  (`Space` or `Ctrl+Enter`).
- Reviews and new cards are mixed according to a configurable ratio (80/20 by
  default).
- Interleaved ordering (shuffle between decks) or sequential ordering (positions
  assigned deck by deck, in your configured order).
- Deck tree with per-deck weights, plus an "All decks" mode that weights each
  top-level deck by how much work it has available.
- Optional top-up when one side of the ratio runs out, so the daily quota is
  still met when possible.
- Automatic cleanup of stale mix cards after an abrupt close, on profile open,
  on profile close, before a new session, and on demand from the dashboard.
- Light and dark theme support through Anki's own theme variables.

## Installation

### Recommended: install the packaged add-on

```powershell
python tools/build_addon.py
```

Then in Anki choose **Tools → Add-ons → Install from file…** and select
`dist/anki_daily_mix.ankiaddon`. Restart Anki if the Tools menu entry does not
appear immediately.

The archive installs into `addons21/anki_daily_mix`. The repository folder is
named `anki-daily-mix`, which is not a valid Python package name, so avoid
copying the checkout directly into `addons21`.

### Manual installation

Copy the project into your add-ons folder and rename the folder to
`anki_daily_mix`:

```text
addons21/
  anki_daily_mix/
    __init__.py
    manifest.json
    config.json
    config.md
    LICENSE
    src/
    web/
```

## Usage

1. Open the dashboard with **Tools → Daily Mix**, the toolbar link, or the
   shortcut.
2. Review the quota and the per-deck allocation.
3. Press **Start Daily Mix** (or `Space` / `Ctrl+Enter`).

Studying the mix updates each card's home deck normally. Cards that are not
answered stay in the temporary filtered deck until you return to the dashboard,
start another mix, or Anki closes (depending on your cleanup settings).

If you reach the quota, the dashboard shows the next reset time. Cards answered
outside the mix count toward the same daily quota.

## Settings

Open **Tools → Daily Mix Settings…**, or use Anki's **Config** button on the
add-on entry.

| Setting | Default | Description |
| --- | --- | --- |
| Daily target | 100 | Cards answered per day, including cards studied outside the mix. Step size 10. |
| Review / new | 80% / 20% | Share of the remaining quota reserved for reviews and new cards. |
| Order | Interleaved | Shuffle between decks, or assign positions deck by deck. Anki may still intersperse new cards among reviews. |
| Decks | All decks | Choose all decks, or check specific decks in the tree. |
| Deck weights | 1.0 | Relative share of the quota. A weight of 2.0 receives roughly twice as many cards as 1.0. |
| Include subdecks | On | A selected parent covers its subtree. A separately selected subdeck keeps its own weight and is claimed first. |
| Backfill | On | Replace an unavailable category with cards from the other category, without exceeding the quota. |
| Cleanup on Anki open | On | Return unanswered mix cards to their home decks after an abrupt close. |
| Cleanup on Anki close | On | Return unanswered mix cards when the profile closes. |
| Mix deck name | Daily Mix | Name of the temporary filtered deck. Fallback names are used if a normal deck already owns it. |
| Toolbar button | On | Show the Daily Mix link in Anki's top toolbar. |
| Shortcut | `Ctrl+Shift+D` | Shortcut for the dashboard. Clear it to disable the shortcut. |

The same values can be edited in `config.json`. See `config.md` for a
field-by-field reference.

## How it works

- Card selection uses only numeric `did:` and `cid:` searches, never deck names,
  so special characters in deck names cannot break a query.
- The mix is materialised through Anki's official filtered-deck API with
  `reschedule=True`. Answered cards are rescheduled in their home deck by Anki's
  normal scheduler.
- Daily Mix never writes `ivl`, `ease`, `factor`, or FSRS memory state. Card
  order inside the filtered deck uses the position field, which is separate from
  scheduling when rescheduling is enabled.
- Temporary positions are stored in `cards.due`; `original_due` is preserved for
  scheduling and restored when the filtered deck is emptied.
- Progress is the number of distinct cards answered today, using Anki's day
  cutoff rather than local midnight.
- The filtered deck id is remembered in the collection config so it can be
  reused across sessions and cleaned up safely.

## Edge cases

- **No due cards:** the dashboard explains that nothing is waiting and disables
  the start button.
- **Fewer cards than the quota:** the mix uses everything available and marks
  the session as a lighter mix.
- **Quota already met:** the dashboard switches to the all-done state and still
  reports what is waiting.
- **Stale mix deck after a crash:** cleanup runs automatically on the next
  profile open or session start.
- **Deck name conflict:** if a normal deck is called "Daily Mix", the add-on
  tries `Daily Mix (Filtered)`, `Daily Mix (Auto)`, and `Daily Mix (Session)`.
- **Deleted selected decks:** the dashboard asks you to choose decks again
  instead of silently studying the wrong ones.
- **Filtered deck backend error:** the dashboard shows a clear message and
  suggests running Check Database.

## Development

Requirements: Python 3.9+ for the add-on code, and Anki 23.10+ for manual
testing.

```powershell
# Compile-check every Python file
python -m compileall -q __init__.py src tests tools

# Run the engine tests (no Anki or Qt required)
python -m unittest discover -s tests -v

# Build the installable archive
python tools/build_addon.py
```

The engine lives in `src/engine.py` and has no Qt imports. The dashboard is a
thin `QDialog` plus `AnkiWebView`; the settings dialog is native Qt. Both use
`aqt.qt` imports so the add-on works with the Qt version bundled with Anki.

```text
__init__.py           Add-on entry point, menu/toolbar/shortcut, profile hooks
config.json           Packaged defaults
config.md             Configuration reference
manifest.json         Add-on metadata
src/constants.py      Shared constants
src/config.py         Load, validate, normalise, persist configuration
src/engine.py         Selection, planning, filtered deck, dashboard payload
src/ui_dashboard.py   Dashboard window and pycmd bridge
src/ui_settings.py    Native settings dialog
web/                  Dashboard HTML, CSS, JS, and icon
tests/                Engine tests with an in-memory fake collection
tools/build_addon.py  Deterministic .ankiaddon builder
```

## Notes and limitations

- Daily Mix is designed around Anki's filtered decks. If another add-on also
  manages filtered decks, cleanup is limited to the deck id stored by Daily Mix.
- The day boundary follows Anki's "next day starts at" setting.
- New cards are chosen from cards Anki still considers new. Because they are
  studied through a filtered deck, a deck's own new-cards-per-day limit does not
  cap the session; Daily Mix's daily target does.
- Suspended and buried cards are always excluded. If the collection changes
  between planning and building (for example a sync arrives, or a card is
  deleted), the mix can contain fewer cards than the dashboard predicted.

## Changelog

### v1.0.1
- **Settings Dialog Overhaul:** Wrapped configuration sections in a native `QScrollArea` and permanently pinned action buttons (`Save`, `Cancel`) to the bottom of the dialog. Added automatic screen-geometry clamping (`_clamp_to_screen`) to prevent oversized windows previously cached in Anki's `prefs21.db` from pushing buttons off-screen.
- **Anki Rust Search Query Fix:** Formatted multi-deck search queries using valid parenthesized OR expressions (`(did:1 or did:2)`) instead of comma-separated `did:1,2` which broke Anki's Rust search parser and produced whole-collection card counts.
- **Review and New Card Mixing:** Strictly isolated due review cards with `(-is:new is:due)`, ensuring due reviews and new cards are reliably blended up to the target quota (with backfill when reviews or new cards run short).
- **UI/UX Refinement:** Clean native Anki desktop styling across the web dashboard and dialogs with zero AI-slop gradients.

### v1.0.0
- Initial release: daily filtered deck study session, dashboard with progress ring, deck tree with custom weights, sequential & interleaved ordering, and automatic cleanup hooks.

## License

Anki Daily Mix is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. The full text is in [LICENSE](LICENSE), and each source file
carries a short header pointing back to it.
