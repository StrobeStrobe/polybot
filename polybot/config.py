"""Configuration for polybot.

Everything risk-related lives here as hard limits the LLM agents cannot
override. Values load from config.json (next to this package) with env-var
overrides for secrets.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

log = logging.getLogger("polybot.config")

ROOT = Path(__file__).resolve().parent.parent
REPO_STATE = ROOT / "state"
# Persistent state directory. Defaults to the repo's ./state (fine locally);
# on Railway set POLYBOT_STATE_DIR to a mounted volume (e.g. /data) so runtime
# state survives redeploys. seed_state_dir() below copies the committed files
# in the first time the volume is empty.
STATE_DIR = Path(os.environ.get("POLYBOT_STATE_DIR", "").strip() or REPO_STATE)
load_dotenv(ROOT / ".env")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class RiskLimits:
    """Hard limits enforced in code. The decision agent's output is clamped
    or vetoed against these — they are not suggestions."""

    max_position_usd: float = 100.0          # max cost basis per market
    max_position_pct: float = 0.05           # max % of bankroll per market
    max_total_exposure_pct: float = 0.50     # max % of bankroll in open positions
    max_open_positions: int = 15
    daily_loss_limit_pct: float = 0.05       # kill switch: stop trading for the day
    min_edge: float = 0.04                   # required (fair_value - price) edge
    min_liquidity_usd: float = 5000.0        # skip thin markets
    max_spread: float = 0.05                 # skip wide-spread books
    min_price: float = 0.03                  # avoid longshot dust
    max_price: float = 0.97                  # avoid near-certain markets (bad risk/reward)
    arb_min_profit: float = 0.01             # min guaranteed profit per $1 for arbs
    fee_bps: float = 0.0                     # taker fee assumption (most PM markets are 0)


@dataclass
class ScoutConfig:
    markets_per_scout: int = 10              # candidate markets sent to each LLM scout
    max_days_to_resolution: int = 60         # prefer markets that resolve soon
    min_volume_24h: float = 10000.0
    scan_market_limit: int = 250             # markets pulled from Gamma per cycle
    book_fetch_limit: int = 120              # max orderbooks fetched per cycle


@dataclass
class CopytradeConfig:
    enabled: bool = True
    leaderboard_depth: int = 1000            # rows paged per leaderboard during discovery
    watchlist_size: int = 15                 # wallets to follow
    # Dollar floors are deliberately modest — win rate is the quality gate.
    # High floors only select for bankroll size, not skill.
    min_monthly_pnl: float = 5000.0          # min 1-month PnL to qualify
    min_roi: float = 0.02                    # min monthly pnl/volume ratio
    min_alltime_pnl: float = 50000.0         # min lifetime PnL — filters hot streaks
    min_alltime_roi: float = 0.01            # min lifetime pnl/volume ratio
    min_prior_pnl: float = 10000.0           # min profit earned BEFORE the current month
    # Activity requirements: we can only copy people who are still betting.
    max_days_inactive: float = 21.0          # most recent trade must be this fresh
    max_weekly_loss_vs_month: float = 1.0    # cut only if a week's loss exceeds Nx monthly profit
    min_trades_30d: int = 30                 # min trades placed in the past month
    exclude_bots: bool = True                # drop bot-like wallets (speed edge isn't copyable)
    # Edge (win rate - avg entry price) is the real profitability gate. The
    # win-rate floor is only a loose noise screen: a sub-45% record is too
    # coin-flippy to trust the edge estimate over a small sample. A profitable
    # underdog bettor (e.g. 47% wins at $0.35 entry = +12% edge) clears both.
    min_win_rate: float = 0.45               # loose noise screen, NOT the profit test
    min_winrate_edge: float = 0.03           # edge margin required of UNPROVEN traders
    # "Proven" traders (big realized PnL + solid ROI) have settled the noise
    # question with real money — they only need a positive per-bet edge.
    proven_pnl_usd: float = 100000.0         # all-time PnL that earns the exemption
    proven_roi: float = 0.03                 # ...with at least this all-time ROI
    proven_min_edge: float = 0.005           # proven traders just need edge above ~0
    min_resolved_markets: int = 20           # min sample size behind the win rate
    trades_sample: int = 500                 # trade history page size per candidate
    max_trades_depth: int = 2500             # deepest history paged for HF traders
    max_markets_checked: int = 200           # resolved markets scored per candidate (also feeds per-sport)
    # Tracked wallets are few and refreshed one per cycle, so score their FULL
    # reachable history rather than the capped discovery sample. The trades API
    # stops serving past ~3.5k fills anyway, so these are effectively "all of it".
    tracked_max_trades: int = 20000          # page until the API runs out
    tracked_max_markets: int = 100000        # no cap on resolved markets scored
    min_sports_share: float = 0.6            # min fraction of trades that are sports
    min_trades_sampled: int = 20             # need enough history to judge
    refresh_days: float = 3.0                # rebuild watchlist this often
    refresh_retry_hours: float = 6.0         # after a failed refresh, wait this long before retrying
    min_their_trade_usd: float = 500.0       # ignore their small bets (low conviction)
    max_price_drift: float = 0.03            # skip if price moved away from their entry (either way)
    max_signal_age_minutes: float = 90.0     # ignore their trades older than this
    min_copy_price: float = 0.10             # don't copy extreme longshots/locks
    max_copy_price: float = 0.90


@dataclass
class Config:
    mode: str = "paper"                      # "paper" or "live"
    bankroll_usd: float = 1000.0             # paper starting cash / live sizing anchor
    decision_model: str = "claude-opus-4-8"
    scout_model: str = "claude-opus-4-8"
    risk: RiskLimits = field(default_factory=RiskLimits)
    scout: ScoutConfig = field(default_factory=ScoutConfig)
    copytrade: CopytradeConfig = field(default_factory=CopytradeConfig)
    # State lives under STATE_DIR. On an ephemeral host (Railway) every deploy
    # resets the container's disk, wiping computed sport/size records, the
    # resolution cache and the copy-performance ledger. Point POLYBOT_STATE_DIR
    # at a mounted volume to keep them across deploys.
    state_file: str = str(STATE_DIR / "portfolio.json")
    watchlist_file: str = str(STATE_DIR / "traders.json")
    resolution_cache_file: str = str(STATE_DIR / "resolutions.json")
    tracked_wallets_file: str = str(STATE_DIR / "tracked_wallets.json")
    alerts_log_file: str = str(STATE_DIR / "alerts.log")
    log_dir: str = str(STATE_DIR / "logs")

    # Alerting (webhook URL is secret-ish — loaded from env, never config.json)
    discord_webhook_url: Optional[str] = None
    discord_enabled: bool = True             # master switch for Discord posts
    desktop_notifications: bool = True       # also fire local macOS/Windows toasts
    tracked_min_usd: float = 100.0           # min trade size to alert for tracked wallets

    # Live trading (all from env, never from config.json)
    polygon_private_key: Optional[str] = None
    polymarket_funder: Optional[str] = None  # proxy wallet address (email/Magic accounts)
    polymarket_signature_type: int = 0       # 0=EOA, 1=email/Magic proxy, 2=browser wallet proxy

    @property
    def live(self) -> bool:
        return self.mode == "live"


def load_config() -> Config:
    cfg = Config()
    path = ROOT / "config.json"
    if path.exists():
        data = json.loads(path.read_text())
        for key in ("mode", "bankroll_usd", "decision_model", "scout_model",
                    "desktop_notifications", "discord_enabled", "tracked_min_usd"):
            if key in data:
                setattr(cfg, key, data[key])
        for section, target in (("risk", cfg.risk), ("scout", cfg.scout),
                                ("copytrade", cfg.copytrade)):
            for k, v in data.get(section, {}).items():
                if hasattr(target, k):
                    setattr(target, k, v)

    cfg.discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL") or None
    # Env override for deployments (e.g. GitHub Actions) that want Discord on
    # without editing a config.json that's paused locally.
    if "DISCORD_ENABLED" in os.environ:
        cfg.discord_enabled = os.environ["DISCORD_ENABLED"].strip().lower() in ("1", "true", "yes")
    cfg.polygon_private_key = os.environ.get("POLYGON_PRIVATE_KEY")
    cfg.polymarket_funder = os.environ.get("POLYMARKET_FUNDER")
    cfg.polymarket_signature_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

    # Refuse live mode without explicit double opt-in.
    if cfg.mode == "live":
        if not cfg.polygon_private_key:
            raise SystemExit("mode=live but POLYGON_PRIVATE_KEY is not set. Refusing to start.")
        if os.environ.get("POLYBOT_CONFIRM_LIVE") != "yes":
            raise SystemExit(
                "mode=live requires POLYBOT_CONFIRM_LIVE=yes in the environment. "
                "This bot will place real orders with real money. Refusing to start."
            )

    Path(cfg.state_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    if STATE_DIR != REPO_STATE:
        seed_state_dir()
    return cfg


def seed_state_dir() -> None:
    """Reconcile a persistent volume with the files committed in the repo.

    Two different kinds of state live side by side:
      * curated in git — WHO is tracked (wallets, labels, per-wallet floors).
        Git is the source of truth; editing the list and pushing must take
        effect even though the volume has an older copy.
      * computed at runtime — sport/size records, last_seen_ts, open-position
        alert state, the resolution cache, the ledger. These must survive
        redeploys, which is the whole point of the volume.

    So: copy any missing file across verbatim, and for the tracked list take
    the roster from git while preserving each wallet's computed fields.
    """
    for name in ("resolutions.json", "traders.json", "portfolio.json",
                 "ledger.json", "tracked_wallets.json"):
        src, dst = REPO_STATE / name, STATE_DIR / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            log.info("seeded %s into %s", name, STATE_DIR)

    src, dst = REPO_STATE / "tracked_wallets.json", STATE_DIR / "tracked_wallets.json"
    if not (src.exists() and dst.exists()):
        return
    try:
        repo = json.loads(src.read_text()).get("wallets", [])
        vol = {w["wallet"]: w for w in json.loads(dst.read_text()).get("wallets", [])}
    except (ValueError, KeyError) as e:
        log.warning("could not merge tracked wallets (%s) — leaving volume copy", e)
        return
    KEEP = ("by_sport", "by_size", "sports_refreshed_at", "last_seen_ts",
            "open_alerts", "record_from", "record_to", "record_trades")
    merged, kept = [], 0
    for w in repo:                      # roster comes from git
        prev = vol.get(w["wallet"])
        if prev:
            for k in KEEP:              # computed fields come from the volume
                if prev.get(k):
                    w[k] = prev[k]
            kept += 1
        merged.append(w)
    dst.write_text(json.dumps({"wallets": merged}, indent=1))
    dropped = len(vol) - kept
    log.info("tracked wallets: %d from git, kept computed state for %d%s",
             len(merged), kept,
             f", dropped {dropped} no longer tracked" if dropped > 0 else "")
