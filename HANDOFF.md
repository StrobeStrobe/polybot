# Polybot — Handoff for the Desktop Assistant

You're being asked to help Cole get **Polybot** running on his **Windows 11
desktop**. This document is the complete context: what it is, how it works, how
to set it up, and how to verify it. It was built with another Claude on a Mac
and moved here as a zip. Read it fully before acting.

---

## TL;DR — what to do

Polybot is currently in **alert-only mode**: it watches a vetted list of
profitable Polymarket sports bettors and sends a **Discord alert** whenever one
places a bet worth copying. Cole copies manually. **It does not trade, move
money, or need an API key in this mode.**

To get it running on this Windows machine:

1. Make sure **Python 3.11+** is installed (python.org, "Add python.exe to
   PATH" checked). Check: `python --version` in PowerShell.
2. The project folder should be at something like `C:\Users\<you>\polybot`.
3. Create a file named **`.env`** in that folder with one line (Cole has the
   webhook URL — ask him for it; it is NOT in the zip for security):
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```
4. **Double-click `setup-autostart.bat`** (once). It builds the Python
   environment and registers a hidden Windows scheduled task that runs the
   watcher at every logon and restarts it on crash.
5. Verify: double-click **`test-alert.bat`** → a green + red embed should
   appear in Cole's Discord channel within seconds.

That's the whole job. Everything below is detail and troubleshooting.

---

## What this project is

A copy-trading assistant for **Polymarket** (a prediction market where people
bet on sports/events at prices from $0.01–$0.99 that resolve to $0 or $1).

The core insight: every wallet's trades on Polymarket are **public and
on-chain**. So Polybot identifies bettors with a *verified* track record of
winning, watches their wallets, and alerts Cole when they place a fresh bet he
could copy in his own account.

**Operating mode right now: alert-only.** No automated trading. No real money.
No Anthropic API key required. The bot only reads public data and posts to
Discord. (A full autonomous paper/live-trading pipeline also exists in the code
but is intentionally parked — see "Do not" below.)

---

## How a trader earns a spot on the watchlist

Discovery (`run.py traders --refresh`, ~40 min) scans up to ~2,000 candidates
and applies these filters in order. Win rate is the decisive one.

1. **Candidate pool** — pages BOTH the monthly *PnL* leaderboard and the
   monthly *volume* leaderboard, 1000 rows deep each (the API caps pages at 50,
   so it paginates by offset). Volume board catches skilled small-stakes
   grinders the PnL board misses.
2. **Profitability floors** — modest dollar floors (win rate is the real gate):
   profitable this month, this week non-negative, profitable all-time, decent
   all-time ROI, and meaningful profit *before* the current month (rejects
   one-hot-month variance accounts).
3. **Activity** — most recent trade within 7 days, ≥30 trades in the last 30
   days. We can only copy people still betting.
4. **Sports focus** — ≥60% of recent trades on sports markets.
5. **Bot screen** — rejects wallets that look like trading bots (>100
   trades/day, or no daily "sleep" gap). A bot's edge is reaction speed Cole
   can't match manually, so its win rate isn't copyable. (Trade *fills* are
   deduped into logical trades first, so a big human order filling against many
   counterparties isn't mistaken for bot activity.)
6. **Verified win rate** — THE key filter. Reconstructs the trader's actual
   per-market win/loss record from their on-chain trades joined against real
   market resolutions (`tokens[].winner` from the CLOB API). A trader must:
   - win ≥55% of resolved markets (≥20-market sample), AND
   - **beat their own average entry price** — i.e. win rate must exceed the
     break-even rate their prices imply. Winning 90% buying at $0.92 is a losing
     strategy, so raw win rate alone is never trusted.
7. **Ranked by win rate.** The list auto-refreshes every 3 days; the heartbeat
   and signal flow handle the in-between.

Result: ~8 verified, active, human sports bettors. The current list is saved in
`state/traders.json` (it shipped in the zip, so the desktop doesn't have to
re-run the 40-min scan).

---

## When an alert actually fires

Each poll (default every 3 min), the watcher checks each watched wallet for new
trades and only alerts on ones Cole could realistically act on:

- **Buy alert (🟢, green embed)** requires ALL of:
  - It's a sports market buy ≥ $500 (a conviction bet, not dust).
  - **Pre-game** — the game hasn't started (in-game prices move faster than a
    human copier).
  - **Fresh** — placed within the last 90 minutes.
  - **Low drift** — the current ask is within ~3¢ of the trader's entry (in
    either direction), so Cole isn't chasing a move that already happened or
    one new info has reversed.
  - Multiple watched traders on the same side aggregate into one stronger
    signal.
- **Exit alert (🔴, red embed)** — a watched trader sells a sizable sports
  position. "If you copied this market, consider exiting."

Alerts go to: **Discord** (primary, rich embeds, pushes to phone), and always
the terminal + `state/alerts.log`. Desktop toasts are turned OFF
(`desktop_notifications: false` in config.json) per Cole's preference —
Discord only.

---

## Files and layout

```
polybot/
  run.py                  # CLI entry point (all commands below)
  config.json             # tunable settings (mode, risk, copytrade knobs)
  requirements.txt        # Python deps
  .env                    # SECRET — you create this; holds DISCORD_WEBHOOK_URL
  .env.example            # template
  setup-autostart.bat     # ← run once: sets up env + registers autostart
  setup-autostart.ps1     # (called by the .bat; does the real work)
  stop-autostart.bat      # remove autostart / stop the watcher
  test-alert.bat          # fire one sample alert to verify Discord
  polybot/
    alerts.py             # watch loop, Discord/desktop notify, heartbeat
    copytrade.py          # trader discovery, vetting, signal generation
    config.py             # config loading (env + config.json)
    ... (market_data, scouts/, decision, risk, portfolio, execution = the
         parked autonomous-trading pipeline; not used in alert mode)
  state/
    traders.json          # the vetted watchlist (ships pre-built)
    resolutions.json      # cache of resolved-market winners (speeds re-vetting)
    last_check.txt        # heartbeat: written every poll (liveness proof)
    alerts.log            # append-only log of every alert fired
```

---

## Commands (run from the polybot folder)

On Windows the Python interpreter lives at `.venv\Scripts\python.exe`. After
`setup-autostart.bat` has built the env, you can run any of these in PowerShell:

```powershell
.venv\Scripts\python.exe run.py watch            # foreground watcher (Ctrl+C to stop)
.venv\Scripts\python.exe run.py watch --test     # one sample alert, then exit
.venv\Scripts\python.exe run.py traders          # show current watchlist
.venv\Scripts\python.exe run.py traders --refresh  # rebuild watchlist (~40 min)
```

In normal operation Cole does NOT run `watch` manually — the scheduled task runs
it hidden. Use `watch --test` and `traders` for verification.

---

## How autostart works (what setup-autostart.bat did)

It registers a Windows Task Scheduler task named **"Polybot Watcher"** that:
- runs at every **logon**,
- executes `.venv\Scripts\pythonw.exe run.py watch` (pythonw = windowless
  Python, so there's no console),
- **restarts on crash** (up to 999 times, 1-min interval),
- has no execution time limit.

Because it's hidden, the only way to *see* it's alive is the heartbeat (below)
or Task Scheduler → "Polybot Watcher" → Status: Running.

To remove it: double-click `stop-autostart.bat`.

Caveat: it runs at **logon**, so Cole must be logged into Windows for it to
poll (a locked-but-logged-in session is fine; powered off is not).

---

## Verifying it works

1. **Discord test**: `test-alert.bat` → green + red embed in the channel.
2. **Heartbeat**: open `state\last_check.txt`. It updates every poll with a
   line like `last checked 2026-06-18 13:17:36 UTC — 8 traders, 0 buy / 0 exit
   signals`. A recent timestamp = it's alive. (Most polls find 0 signals; that's
   normal — real alerts cluster in the afternoon/evening before game slates.)
3. **Task status**: Task Scheduler → Task Scheduler Library → "Polybot Watcher".

---

## Tuning (config.json → "copytrade" section)

- `min_their_trade_usd` (default 500): raise to ~1000–2000 if alerts are too
  frequent — keeps only larger-conviction bets.
- `watchlist_size` (default 15; ~8 survive vetting): how many to follow.
- `min_win_rate` (0.55), `min_winrate_edge` (0.02): the quality gate. Lower
  slightly for more traders, raise for a stricter list.
- `refresh_days` (3): how often the watchlist auto-rebuilds.
- After editing config.json, the change takes effect on the next poll; no
  restart needed for most values, but restart the task to be safe
  (stop-autostart.bat then setup-autostart.bat, or restart the task in Task
  Scheduler).

---

## Do NOT do these (safety)

- **Do not set `"mode": "live"` in config.json.** That switches the parked
  pipeline to placing REAL orders with REAL money. It also requires a funded
  wallet private key and explicit env confirmation, so it can't happen by
  accident — but don't go there unless Cole explicitly asks and understands it.
- **Do not commit or share `.env`** — it holds the Discord webhook (anyone with
  the URL can post to the channel). It's gitignored.
- **Do not execute trades or move money on Cole's behalf.** This tool only
  alerts; copying a bet is a manual action Cole takes himself.
- The autonomous trading pipeline (`scouts/`, `decision.py`, `execution.py`,
  etc.) needs an Anthropic API key and costs money per cycle. It is NOT part of
  alert mode. Leave it alone unless Cole asks to revisit it.

---

## Likely issues on Windows

- **"python is not recognized"** → Python isn't installed or not on PATH.
  Install from python.org with "Add to PATH" checked, reopen PowerShell.
- **`.env` saved as `.env.txt`** → Notepad appends .txt. In the Save dialog set
  "Save as type: All Files" and name it exactly `.env`.
- **No Discord embeds on test** → webhook URL wrong/deleted, or `.env` not in
  the polybot folder / misnamed. Recreate the webhook in Discord (Server
  Settings → Integrations → Webhooks) and update `.env`.
- **PowerShell blocks the .ps1** → it's launched via the .bat with
  `-ExecutionPolicy Bypass`, so use the .bat, don't run the .ps1 directly.
- **pip fails on `winotify`** → harmless in Discord-only mode; that package is
  only for Windows desktop toasts, which are disabled. Alerts still work.

---

## One sentence to give Cole when done

"Polybot is set up and will start automatically every time you log in — watch
your Discord channel for green buy / red exit signals, and check
`state\last_check.txt` if you ever want to confirm it's still running."
