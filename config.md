# Anki Daily Mix configuration

Daily Mix normally manages this file for you. Open **Tools → Daily Mix
Settings…** (or the gear button on the dashboard) for the friendly editor. The
JSON file is still useful for backups, scripted installs, and troubleshooting.

```json
{
  "daily_target": 100,
  "review_percent": 80,
  "order_strategy": "interleaved",
  "decks": [],
  "include_subdecks": true,
  "backfill": true,
  "cleanup_on_start": true,
  "cleanup_on_close": true,
  "mix_deck_name": "Daily Mix",
  "show_toolbar_button": true,
  "shortcut": "Ctrl+Shift+D"
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `daily_target` | `100` | Cards to answer per day, counted across the whole collection. Clamped to 1–5000. |
| `review_percent` | `80` | Share of the remaining quota reserved for review cards. `0` means new cards only, `100` means reviews only. |
| `order_strategy` | `"interleaved"` | `"interleaved"` shuffles cards between decks for variety. `"sequential"` assigns positions deck by deck in your configured order. Anki's scheduler may still intersperse new cards among reviews. |
| `decks` | `[]` | Decks to draw from. An empty list means **all decks**. Each entry is `{"name": "Deck", "weight": 1.0}`. |
| `include_subdecks` | `true` | When a deck is selected, include its subdecks. A separately selected subdeck keeps its own weight and is resolved first. |
| `backfill` | `true` | If reviews (or new cards) run out, fill the gap from the other category without exceeding the daily target. |
| `cleanup_on_start` | `true` | Return cards left in a stale Daily Mix filtered deck when Anki opens. |
| `cleanup_on_close` | `true` | Return unanswered mix cards to their home decks when Anki closes. |
| `mix_deck_name` | `"Daily Mix"` | Name of the temporary filtered deck. Change it if a normal deck already uses this name. |
| `show_toolbar_button` | `true` | Show a **Daily Mix** link in Anki's top toolbar. |
| `shortcut` | `"Ctrl+Shift+D"` | Keyboard shortcut for the dashboard. An empty string disables it. |

## Weights

Weights are relative, not percentages. A deck with weight `2.0` receives twice
as many cards as a deck with weight `1.0`, provided it has enough cards
available. Weights are clamped to `0.1`–`10.0`.

## How the quota is counted

The daily target covers **all cards answered today**, including cards studied
outside the mix. Daily Mix subtracts that number from the target and only plans
the remainder, so using the add-on never doubles your workload.

Anki's day cutoff (usually 4 a.m. by default) is respected. Change it in Anki's
preferences if your study day starts at a different time.

## Safety

Daily Mix creates a real Anki filtered deck with rescheduling enabled and then
returns unanswered cards to their home decks. It never edits a card's interval,
ease factor, or FSRS memory state directly. Answering a card uses Anki's normal
scheduler, so statistics, undo, syncing, and backups continue to work.
