# Polybot

An autonomous Polymarket trading bot with a multi-agent architecture: scout
agents find opportunities, a head-trader agent decides, a code-level risk
manager enforces hard limits, and an execution layer trades — on paper by
default, live once you fund a wallet and explicitly opt in.

> **Reality check:** prediction markets are competitive and professional
> bots already arbitrage them. Nothing here guarantees profit. Paper trade
> until the bot has a track record you trust, and only ever fund the wallet
> with money you can afford to lose.

## Architecture

```
                 ┌────────────────────────────────────────────┐
                 │                market data                  │
                 │   Gamma API (markets) + CLOB API (books)    │
                 └──────┬─────────────┬─────────────┬─────────┘
                        ▼             ▼             ▼
       ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
       │ arbitrage   │ │ value scout │ │ news scout  │ │ copy-trade  │
       │ scanner     │ │ (Claude +   │ │ (Claude +   │ │ scout (top  │
       │ (pure code) │ │ web search) │ │ web search) │ │ sports PnL) │
       └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
              └───────────────┴───────┬───────┴───────────────┘
                                     ▼  structured reports
                          ┌────────────────────┐
                          │ head trader agent  │  (Claude — proposes trades)
                          └─────────┬──────────┘
                                    ▼
                          ┌────────────────────┐
                          │   risk manager     │  (code — clamps & vetoes,
                          │   + kill switch    │   daily-loss halt)
                          └─────────┬──────────┘
                                    ▼
                          ┌────────────────────┐
                          │     execution      │  paper sim ⇄ py-clob-client
                          └─────────┬──────────┘
                                    ▼
                       portfolio state + cycle logs (state/)
```

The LLM agents only ever **propose**. Position size caps, exposure caps, the
max-open-positions limit, spread/liquidity filters, and the daily-loss kill
switch are enforced in `risk.py` and cannot be overridden by a model.

## Setup

```bash
cd ~/polybot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
```

## Usage

```bash
python run.py watch             # alert-only mode: notify me when watched traders bet (free, no API key)
python run.py scan              # free: see candidates + arbitrage scan (no API key)
python run.py cycle --no-llm    # one cycle, arb + copy-trade scouts only (free)
python run.py cycle             # one full cycle with all agents
python run.py loop --interval 60  # run a cycle every hour
python run.py status            # portfolio snapshot
python run.py traders           # show/refresh the copy-trade watchlist
```

### Copy-trading

The copy-trade scout follows the most profitable sports bettors on Polymarket
(all wallet activity is public). Discovery pulls the monthly PnL leaderboard
as the candidate pool, then vets each wallet across timeframes: non-negative
7-day PnL, minimum monthly ROI on volume, **minimum all-time profit and
all-time ROI** (a hot month on a lifetime-losing account is variance, not
skill — this filter rejects most of the monthly leaderboard), and a minimum
profit earned *before* the current month (filters brand-new hot accounts).

The decisive filter is a **verified win rate**: the bot reconstructs each
candidate's per-market win/loss record from their on-chain trade history
joined against actual market resolutions (paging thousands of trades deep
for high-frequency bettors). A trader must win ≥55% of resolved markets
*and* beat their average entry price — buying favorites at 0.92 and winning
90% of the time is a losing strategy, so win rate is always measured against
the break-even rate their own prices imply. Survivors must also place most
of their trades on sports markets; the final list is ranked by win rate.
Market resolutions are cached in `state/resolutions.json` since sports
bettors trade the same game slates. Each cycle
the bot checks the watchlist for fresh, sizable buys and reports them to the
head trader — but only when the bet is **pre-game**, recent (≤90 min), and
the current ask hasn't drifted from the bettor's entry. The head trader
decides whether to copy; a watched trader exiting a market we copied is
reported as an exit signal. Tune everything under `copytrade` in
`config.json` (`"enabled": false` turns it off).

**Alert-only mode** (`python run.py watch`) is the no-risk way to start: it
polls the watched wallets every few minutes and alerts you whenever one of
them places a qualifying pre-game bet — you copy manually if you agree. A
sizable sell by a watched trader triggers an exit alert. Same quality
filters as the automated scout; no API key, no LLM cost, no trading.

Alerts fire to (in priority order): a **Discord webhook** if
`DISCORD_WEBHOOK_URL` is set in `.env` (rich colored embeds, pushes to your
phone — works regardless of OS or where the bot runs), a **desktop
notification** (macOS banner or Windows 11 toast), and always a line in
`state/alerts.log` with a Polymarket link. Verify your wiring without
waiting for a real signal:

```bash
python run.py watch --test     # fires one sample buy + sell to every channel
```

To use Discord: in your server, **Server Settings → Integrations → Webhooks
→ New Webhook**, copy the URL into `.env` as `DISCORD_WEBHOOK_URL=...`, then
run the test above. Set `"desktop_notifications": false` in `config.json` if
you want Discord only.

Caveat: leaderboard PnL is not proof of skill — one good month can be
variance, and you copy at a slightly worse price with a delay. The
discovery filters (consistency, ROI, sample size) reduce but don't remove
this.

Every cycle writes a full JSON report to `state/logs/` — scout reports, the
head trader's reasoning, vetoes, and fills. Portfolio state persists in
`state/portfolio.json`.

### Cost note

Each full cycle makes 3 Claude calls (2 scouts with web search + 1 decision),
roughly $0.50–$2.00 per cycle depending on search volume. At hourly cycles
that's ~$15–50/day — tune `--interval`, `markets_per_scout`, or switch
`scout_model` in `config.json` if you want to trade cost against coverage.

## Going live (when you're ready)

1. Paper trade for at least a few weeks. Check `python run.py status` and the
   cycle logs. If the paper track record isn't profitable, the live one won't be.
2. Create/fund a Polygon wallet with USDC for Polymarket. Note Polymarket's
   geographic/eligibility rules apply to you, not the bot.
3. `pip install py-clob-client`
4. In `.env`, set `POLYGON_PRIVATE_KEY` (and `POLYMARKET_FUNDER` +
   `POLYMARKET_SIGNATURE_TYPE` if your account uses Polymarket's proxy wallet
   — i.e. you signed up by email or browser wallet rather than trading from a
   raw EOA). For raw EOA wallets you must also approve USDC/CTF allowances for
   Polymarket's exchange contracts once (proxy-wallet accounts skip this).
5. Set `"mode": "live"` in `config.json` and `POLYBOT_CONFIRM_LIVE=yes` in `.env`.
6. Set `bankroll_usd` to the actual USDC you deposited — all position sizing
   keys off it.
7. Start small: lower `max_position_usd` (e.g. $10–25) for the first weeks.

Live orders are placed fill-or-kill, so the bot never leaves resting orders
unattended.

## Risk limits (config.json)

| key | meaning |
|---|---|
| `max_position_usd` / `max_position_pct` | per-market cost cap (lower of the two) |
| `max_total_exposure_pct` | max share of bankroll in open positions |
| `max_open_positions` | concurrent position cap |
| `daily_loss_limit_pct` | kill switch — halts new positions for the day |
| `min_edge` | required fair-value-vs-price gap before opening |
| `min_liquidity_usd`, `max_spread` | thin/wide market filters |
| `min_price`, `max_price` | tradeable price band |
