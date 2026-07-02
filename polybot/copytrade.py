"""Copy-trade scout: identify consistently profitable sports bettors on
Polymarket from the public leaderboard, watch their wallets, and report
their fresh bets to the head trader as a "smart money" signal.

All data comes from Polymarket's public data API — every wallet's trades are
on-chain and queryable. No LLM calls in this module.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .config import GAMMA_API, Config
from .market_data import fetch_books

log = logging.getLogger("polybot.copytrade")

DATA_API = "https://data-api.polymarket.com"
LB_API = "https://lb-api.polymarket.com"

# Per-season "series" ids for out-of-season sports (current leaderboards can't
# surface them). Found via a known game event's `series` field.
SEASON_SERIES = {"NFL": 10187, "CFB": 10210, "UFC": 38, "MLB": 3}

_session = requests.Session()
_session.headers["User-Agent"] = "polybot/0.1"
# Auto-retry transient network failures (connection resets, 5xx, 429) with
# backoff. A discovery scan makes ~2000 calls over many minutes — without this
# a single blip aborts the whole refresh.
_retry = requests.adapters.Retry(
    total=4, backoff_factor=1.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False,
)
_session.mount("https://", requests.adapters.HTTPAdapter(max_retries=_retry))
_session.mount("http://", requests.adapters.HTTPAdapter(max_retries=_retry))

# Event-slug prefixes / keywords that mark a market as sports.
SPORTS_PREFIXES = (
    "nba-", "nfl-", "mlb-", "nhl-", "wnba-", "mls-", "epl-", "ucl-", "uel-",
    "laliga-", "la-liga-", "seriea-", "serie-a-", "bundesliga-", "ligue1-",
    "ligue-1-", "ufc-", "atp-", "wta-", "cfb-", "cbb-", "f1-", "nascar-",
    "pga-", "boxing-",
)
SPORTS_KEYWORDS = (
    "world-cup", "champions-league", "premier-league", "super-bowl", "tennis",
    "grand-slam", "wimbledon", "us-open", "stanley-cup", "nba-finals",
    "march-madness", "olympic",
)


def is_sports_slug(event_slug: str) -> bool:
    s = (event_slug or "").lower()
    return s.startswith(SPORTS_PREFIXES) or any(k in s for k in SPORTS_KEYWORDS)


# Map the league-code slug prefix to a readable sport bucket. Event slugs are
# clean league codes (e.g. "nba-sas-nyk-...", "atp-...", "fifwc-jpn-swe-...").
_SPORT_BY_PREFIX = {
    "mlb": "MLB", "nba": "NBA", "wnba": "WNBA", "nfl": "NFL", "nhl": "NHL",
    "cbb": "NCAAB", "cfb": "NCAAF",
    "atp": "Tennis", "wta": "Tennis",
    "epl": "Soccer", "lal": "Soccer", "laliga": "Soccer", "bun": "Soccer",
    "bundesliga": "Soccer", "ucl": "Soccer", "uel": "Soccer", "seriea": "Soccer",
    "ligue1": "Soccer", "ligue": "Soccer", "mls": "Soccer", "fifwc": "Soccer",
    "fifa": "Soccer", "wcq": "Soccer", "concacaf": "Soccer", "copa": "Soccer",
    "ufc": "MMA", "boxing": "Boxing", "f1": "Motorsport", "nascar": "Motorsport",
    "pga": "Golf",
}


def sport_from_slug(event_slug: str) -> str:
    """Readable sport/league bucket for an event slug, or 'Other'."""
    s = (event_slug or "").lower()
    prefix = s.split("-", 1)[0]
    if prefix in _SPORT_BY_PREFIX:
        return _SPORT_BY_PREFIX[prefix]
    # keyword fallbacks for non-prefixed slugs
    if "world-cup" in s or "premier-league" in s or "champions-league" in s:
        return "Soccer"
    if "tennis" in s or "wimbledon" in s or "-open" in s:
        return "Tennis"
    return "Other"


@dataclass
class WatchedTrader:
    wallet: str
    pseudonym: str
    pnl_1m: float
    pnl_1w: float
    pnl_all: float = 0.0       # all-time profit (the durability filter)
    roi_1m: float = 0.0        # monthly pnl / monthly volume
    roi_all: float = 0.0       # all-time pnl / all-time volume
    win_rate: float = 0.0      # share of resolved markets they made money on
    winrate_edge: float = 0.0  # win_rate minus break-even rate from avg entry
    avg_entry: float = 0.0     # avg price paid (= break-even win rate)
    resolved_markets: int = 0  # sample size behind win_rate
    by_sport: dict = field(default_factory=dict)  # sport -> {markets, wins, win_rate, pnl}
    sports_share: float = 0.0  # fraction of recent trades that are sports
    style: str = ""            # bot / uncertain / human (trade-cadence heuristic)
    trades_per_day: float = 0.0
    quiet_hours: int = 0       # longest daily no-trading stretch (humans sleep)
    trades_sampled: int = 0
    avg_trade_usd: float = 0.0
    added_at: str = ""
    last_seen_ts: int = 0      # newest trade timestamp already reported


# ------------------------------------------------------- track record ----

class ResolutionCache:
    """conditionId -> list of final outcome prices (0/1) for resolved markets.
    Persisted because watched sports bettors trade the same game slates —
    lookups overlap heavily across traders and refresh runs."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, List[float]] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))

    def get(self, condition_id: str) -> Optional[List[float]]:
        """Final prices per outcome index, or None if unresolved/unknown."""
        if condition_id in self.data:
            return self.data[condition_id]
        try:
            resp = _session.get(f"https://clob.polymarket.com/markets/{condition_id}", timeout=15)
            if not resp.ok:
                return None
            m = resp.json()
        except (requests.RequestException, ValueError):
            return None
        tokens = m.get("tokens") or []
        if not m.get("closed") or not any(t.get("winner") for t in tokens):
            return None  # open or resolved without a winner (e.g. 50/50) — don't cache
        finals = [1.0 if t.get("winner") else 0.0 for t in tokens]
        self.data[condition_id] = finals
        return finals


def compute_track_record(trades: List[dict], cache: ResolutionCache,
                         max_markets: int = 60,
                         unresolved_memo: Optional[set] = None) -> dict:
    """Reconstruct per-market results from a trader's trade history.

    A market counts as a win if the trader's net result there was positive:
    sell proceeds + final value of net shares at resolution - buy cost.
    Markets where net shares go negative (they sold tokens bought before our
    sample window) are skipped as incomplete.

    Also returns avg_entry — the volume-weighted price they paid, which is
    the win rate needed to break even. A 90% win rate buying at 0.92 is a
    losing strategy; win_rate must beat avg_entry to indicate skill.
    """
    per_market: Dict[str, dict] = {}
    order: List[str] = []  # most recent first (trades come newest-first)
    for t in trades:
        cid = t.get("conditionId")
        idx = t.get("outcomeIndex")
        if not cid or idx is None:
            continue
        if cid not in per_market:
            per_market[cid] = {"buy_usd": 0.0, "sell_usd": 0.0,
                               "buy_shares": 0.0, "net": {},
                               "sport": sport_from_slug(t.get("eventSlug", ""))}
            order.append(cid)
        rec = per_market[cid]
        shares = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        if t.get("side") == "BUY":
            rec["buy_usd"] += shares * price
            rec["buy_shares"] += shares
            rec["net"][idx] = rec["net"].get(idx, 0.0) + shares
        elif t.get("side") == "SELL":
            rec["sell_usd"] += shares * price
            rec["net"][idx] = rec["net"].get(idx, 0.0) - shares

    if unresolved_memo is None:
        unresolved_memo = set()
    wins = losses = 0
    entry_usd = entry_shares = 0.0
    by_sport: Dict[str, dict] = {}  # sport -> {markets, wins, pnl}
    # Walk newest-first until we've scored max_markets RESOLVED markets —
    # for high-frequency traders the newest markets are mostly still open.
    for cid in order:
        if wins + losses >= max_markets:
            break
        rec = per_market[cid]
        if rec["buy_shares"] <= 0:
            continue
        if any(v < -1e-6 for v in rec["net"].values()):
            continue  # incomplete: sold shares bought before the sample window
        if cid in unresolved_memo:
            continue
        finals = cache.get(cid)
        if finals is None:
            unresolved_memo.add(cid)  # don't re-query within this run
            continue
        final_value = sum(shares * (finals[i] if i < len(finals) else 0.0)
                          for i, shares in rec["net"].items())
        pnl = rec["sell_usd"] + final_value - rec["buy_usd"]
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        entry_usd += rec["buy_usd"]
        entry_shares += rec["buy_shares"]
        sp = by_sport.setdefault(rec["sport"], {"markets": 0, "wins": 0, "pnl": 0.0,
                                                "entry_usd": 0.0, "entry_shares": 0.0})
        sp["markets"] += 1
        sp["wins"] += 1 if pnl > 0 else 0
        sp["pnl"] += pnl
        sp["entry_usd"] += rec["buy_usd"]
        sp["entry_shares"] += rec["buy_shares"]

    resolved = wins + losses
    win_rate = wins / resolved if resolved else 0.0
    avg_entry = entry_usd / entry_shares if entry_shares else 0.0
    # Finalize per-sport: win rate, avg entry, and edge (win rate − avg entry).
    for sp in by_sport.values():
        sp["pnl"] = round(sp["pnl"], 2)
        sp["win_rate"] = round(sp["wins"] / sp["markets"], 4) if sp["markets"] else 0.0
        sp["avg_entry"] = round(sp["entry_usd"] / sp["entry_shares"], 4) if sp["entry_shares"] else 0.0
        sp["edge"] = round(sp["win_rate"] - sp["avg_entry"], 4)
        del sp["entry_usd"]
        del sp["entry_shares"]
    return {
        "resolved_markets": resolved,
        "win_rate": round(win_rate, 4),
        "avg_entry": round(avg_entry, 4),
        "winrate_edge": round(win_rate - avg_entry, 4),
        "by_sport": by_sport,
    }


# ------------------------------------------------------ style analysis ----

def classify_trading_style(trades: List[dict]) -> dict:
    """Heuristic bot-vs-human classification from trade cadence.

    The API reports fills, so a single order sweeping several resting orders
    shows as multiple same-second rows — dedupe to logical trades first
    (one per timestamp+market+side). Then look at the two strongest tells:
    sustained trade rate (humans don't place 100+ bets/day) and a daily
    quiet window (humans sleep; bots fire around the clock).
    """
    seen = set()
    logical: List[int] = []
    for t in trades:
        ts = int(t.get("timestamp") or 0)
        key = (ts, t.get("conditionId"), t.get("side"))
        if ts and key not in seen:
            seen.add(key)
            logical.append(ts)
    logical.sort()
    n = len(logical)
    if n < 20:
        return {"style": "uncertain", "trades_per_day": 0.0, "quiet_hours": 0}

    span_days = max((logical[-1] - logical[0]) / 86400, 0.25)
    per_day = n / span_days

    from collections import Counter
    hours = Counter(time.gmtime(ts).tm_hour for ts in logical)
    quiet = {h for h in range(24) if hours.get(h, 0) < 0.015 * n}
    longest = cur = 0
    for h in range(48):  # wrap past midnight
        if (h % 24) in quiet:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    quiet_hours = min(longest, 24)

    gaps = [b - a for a, b in zip(logical, logical[1:])]
    fast_share = sum(1 for g in gaps if g < 60) / len(gaps) if gaps else 0.0

    if per_day > 100 or (quiet_hours < 3 and n >= 200):
        style = "bot"
    elif per_day > 30 or quiet_hours < 5 or fast_share > 0.5:
        style = "uncertain"
    else:
        style = "human"
    return {"style": style, "trades_per_day": round(per_day, 1),
            "quiet_hours": quiet_hours}


# ------------------------------------------------------------ discovery ----

def _leaderboard(period: str, depth: int = 100, rank_type: str = "pnl") -> List[dict]:
    """period: day | week | month | all; rank_type: pnl | vol.
    The API caps each page at 50 rows, so page via offset up to `depth`."""
    rows: List[dict] = []
    offset = 0
    while offset < depth:
        resp = _session.get(
            f"{DATA_API}/v1/leaderboard",
            params={"timePeriod": period, "limit": 50, "offset": offset,
                    "rankType": rank_type},
            timeout=30,
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        rows.extend(page)
        if len(page) < 50:
            break
        offset += len(page)
    return rows


def _wallet_stat(endpoint: str, window: str, wallet: str) -> float:
    """Per-wallet profit or volume from lb-api. endpoint: 'profit' | 'volume';
    window: '1d' | '7d' | '30d' | 'all'."""
    try:
        resp = _session.get(f"{LB_API}/{endpoint}",
                            params={"window": window, "address": wallet}, timeout=15)
        if resp.ok:
            rows = resp.json()
            if rows:
                return float(rows[0].get("amount") or 0)
    except (requests.RequestException, ValueError):
        pass
    return 0.0


def _recent_trades(wallet: str, limit: int = 100, offset: int = 0) -> List[dict]:
    # Never let one wallet's network failure abort a whole discovery scan —
    # skip the candidate instead (retries already attempted at the adapter).
    try:
        resp = _session.get(
            f"{DATA_API}/trades",
            # takerOnly=false is REQUIRED — the default returns only taker
            # (market-order) fills and silently omits maker/limit fills, which
            # sharp bettors use heavily (this missed DaBossHogg's $12k Spain bet).
            params={"user": wallet, "limit": limit, "offset": offset,
                    "takerOnly": "false"},
            timeout=30,
        )
        if not resp.ok:
            return []
        return resp.json()
    except (requests.RequestException, ValueError):
        return []


def discover_sports_traders(cfg: Config,
                            grandfather_wallets: Optional[List[str]] = None) -> List[WatchedTrader]:
    """Build a watchlist of consistently profitable, sports-focused traders.

    grandfather_wallets: current watchlist members. They're re-vetted even if
    they've slipped off the leaderboards or below a dollar floor, so we never
    drop a verified winner on a technicality. They are exempt ONLY from the
    cheap leaderboard/dollar pre-filters — they must still pass the real gates:
    activity, sports focus, bot screen, and verified win rate. A member whose
    win rate has actually collapsed still gets cut.
    """
    cc = cfg.copytrade
    cache = ResolutionCache(cfg.resolution_cache_file)
    gf = {w.lower() for w in (grandfather_wallets or [])}
    # Two candidate pools, merged: the monthly PnL board (biggest winners) and
    # the monthly volume board (most active — catches skilled grinders whose
    # absolute PnL is too small for the PnL board). Both row types carry
    # monthly pnl + vol, so the same cheap filters apply.
    by_pnl = _leaderboard("month", cc.leaderboard_depth, "pnl")
    by_vol = _leaderboard("month", cc.leaderboard_depth, "vol")
    seen_wallets = set()
    candidates: List[dict] = []
    for row in by_pnl + by_vol:
        w = row["proxyWallet"].lower()
        if w not in seen_wallets:
            seen_wallets.add(w)
            candidates.append(row)
    # Add grandfathered members not on either board, synthesizing their stats
    # from the per-wallet endpoints (30d ≈ the monthly leaderboard window).
    for w in gf:
        if w not in seen_wallets:
            seen_wallets.add(w)
            candidates.append({
                "proxyWallet": w,
                "pnl": _wallet_stat("profit", "30d", w),
                "vol": _wallet_stat("volume", "30d", w),
            })
    # Vet grandfathered members first (so the watchlist-size cap never crowds
    # them out), then everyone else by monthly PnL.
    candidates.sort(key=lambda r: (r["proxyWallet"].lower() in gf,
                                   float(r.get("pnl") or 0)), reverse=True)
    log.info("vetting %d unique candidates (%d grandfathered) from leaderboards",
             len(candidates), len(gf))

    traders: List[WatchedTrader] = []
    for row in candidates:
        wallet = row["proxyWallet"].lower()
        grandfathered = wallet in gf
        pnl_1m = float(row.get("pnl") or 0)
        vol_1m = float(row.get("vol") or 0)
        pnl_1w = _wallet_stat("profit", "7d", wallet)
        roi = pnl_1m / vol_1m if vol_1m > 0 else 0.0
        pnl_all = _wallet_stat("profit", "all", wallet)
        vol_all = _wallet_stat("volume", "all", wallet)
        roi_all = pnl_all / vol_all if vol_all > 0 else 0.0

        # Cheap pre-filters: proxies for "is this a real winner". Grandfathered
        # members are already verified, so they skip these and go straight to
        # the real gates (activity / sports / bot / win rate) below.
        if not grandfathered:
            if pnl_1m < cc.min_monthly_pnl:
                continue
            # Circuit-breaker, not a consistency filter: a skilled sports bettor
            # is routinely down in any single week from variance. Only cut a
            # weekly loss that's a real collapse vs. their monthly profit.
            if pnl_1w < -cc.max_weekly_loss_vs_month * pnl_1m:
                log.info("rejected %s: weekly loss $%.0f exceeds %.0fx monthly profit $%.0f",
                         row.get("userName", wallet[:10]), pnl_1w,
                         cc.max_weekly_loss_vs_month, pnl_1m)
                continue
            # ROI proxy: profitable on huge volume can still be a thin edge.
            if vol_1m > 0 and roi < cc.min_roi:
                continue
            # Durability: a hot month on a lifetime-losing account is variance.
            if pnl_all < cc.min_alltime_pnl:
                log.info("rejected %s: 1m pnl $%.0f but all-time pnl $%.0f",
                         row.get("userName", wallet[:10]), pnl_1m, pnl_all)
                continue
            # Profit must predate this month (not a brand-new hot account).
            if pnl_all - pnl_1m < cc.min_prior_pnl:
                log.info("rejected %s: only $%.0f profit before the current month",
                         row.get("userName", wallet[:10]), pnl_all - pnl_1m)
                continue
            if vol_all > 0 and roi_all < cc.min_alltime_roi:
                log.info("rejected %s: all-time roi %.2f%% below floor",
                         row.get("userName", wallet[:10]), roi_all * 100)
                continue

        trades = _recent_trades(wallet, cc.trades_sample)
        if len(trades) < cc.min_trades_sampled:
            continue
        # Activity gate: only worth watching wallets that still bet. Most
        # recent trade must be fresh and the month must show real activity.
        now_ts = int(time.time())
        newest_ts = max((int(t.get("timestamp") or 0) for t in trades), default=0)
        if now_ts - newest_ts > cc.max_days_inactive * 86400:
            log.info("rejected %s: inactive %.1f days",
                     row.get("userName", wallet[:10]), (now_ts - newest_ts) / 86400)
            continue
        trades_30d = sum(1 for t in trades if int(t.get("timestamp") or 0) > now_ts - 30 * 86400)
        if trades_30d < cc.min_trades_30d:
            log.info("rejected %s: only %d trades in past 30 days",
                     row.get("userName", wallet[:10]), trades_30d)
            continue
        sports = [t for t in trades if is_sports_slug(t.get("eventSlug", ""))]
        share = len(sports) / len(trades)
        if share < cc.min_sports_share:
            continue

        # Bot screen: a speed/scalping bot's win rate isn't copyable — its
        # edge is reaction time we don't have. Done before the expensive
        # win-rate stage (bots would trigger deep history paging there).
        style = classify_trading_style(trades)
        if cc.exclude_bots and style["style"] == "bot":
            log.info("rejected %s: bot-like trading (%.0f trades/day, quiet window %dh)",
                     row.get("userName", wallet[:10]),
                     style["trades_per_day"], style["quiet_hours"])
            continue

        # Win-rate vetting: reconstruct their per-market record on resolved
        # markets. Win rate must clear the floor AND beat their average entry
        # price (the break-even rate) — otherwise they're just buying
        # favorites, which looks like winning until it doesn't.
        unresolved: set = set()
        record = compute_track_record(trades, cache, cc.max_markets_checked, unresolved)
        name = row.get("userName") or trades[0].get("pseudonym") or wallet[:10]
        # High-frequency traders burn through 500 trades in hours — too few
        # resolved markets to judge. Page deeper into their history until the
        # sample is big enough or we hit the depth cap.
        offset = len(trades)
        while (record["resolved_markets"] < cc.min_resolved_markets
               and offset < cc.max_trades_depth):
            more = _recent_trades(wallet, cc.trades_sample, offset)
            if not more:
                break
            trades.extend(more)
            offset += len(more)
            record = compute_track_record(trades, cache, cc.max_markets_checked, unresolved)
        if record["resolved_markets"] < cc.min_resolved_markets:
            log.info("rejected %s: only %d resolved markets in %d trades",
                     name, record["resolved_markets"], len(trades))
            continue
        if record["win_rate"] < cc.min_win_rate:
            log.info("rejected %s: win rate %.0f%% over %d markets below %.0f%% floor",
                     name, record["win_rate"] * 100, record["resolved_markets"],
                     cc.min_win_rate * 100)
            continue
        # Edge gate. Unproven traders must clear a margin so a thin edge isn't
        # just sample noise. A trader who's *proven* it with real money (big
        # all-time PnL + solid ROI over thousands of bets) only needs a positive
        # edge — their realized track record already settles the noise question.
        proven = pnl_all >= cc.proven_pnl_usd and roi_all >= cc.proven_roi
        edge_req = cc.proven_min_edge if proven else cc.min_winrate_edge
        if record["winrate_edge"] < edge_req:
            log.info("rejected %s: edge %+.1f%% below %.1f%% (avg entry %.2f%s)",
                     name, record["winrate_edge"] * 100, edge_req * 100,
                     record["avg_entry"], ", proven" if proven else "")
            continue

        # Qualified — now page deeper to fill the full per-sport sample
        # (max_markets_checked). Done only here so we never deep-fetch the many
        # candidates rejected above.
        offset = len(trades)
        while (record["resolved_markets"] < cc.max_markets_checked
               and offset < cc.max_trades_depth):
            more = _recent_trades(wallet, cc.trades_sample, offset)
            if not more:
                break
            trades.extend(more)
            offset += len(more)
            record = compute_track_record(trades, cache, cc.max_markets_checked, unresolved)

        sized = [t["size"] * t["price"] for t in trades if t.get("size") and t.get("price")]
        traders.append(
            WatchedTrader(
                wallet=wallet,
                pseudonym=name,
                pnl_1m=round(pnl_1m, 2),
                pnl_1w=round(pnl_1w, 2),
                pnl_all=round(pnl_all, 2),
                roi_1m=round(roi, 4),
                roi_all=round(roi_all, 4),
                win_rate=record["win_rate"],
                winrate_edge=record["winrate_edge"],
                avg_entry=record["avg_entry"],
                resolved_markets=record["resolved_markets"],
                by_sport=record.get("by_sport", {}),
                sports_share=round(share, 3),
                style=style["style"],
                trades_per_day=style["trades_per_day"],
                quiet_hours=style["quiet_hours"],
                trades_sampled=len(trades),
                avg_trade_usd=round(sum(sized) / len(sized), 2) if sized else 0.0,
                added_at=datetime.now(timezone.utc).isoformat(),
                last_seen_ts=max((int(t.get("timestamp") or 0) for t in trades), default=0),
            )
        )
        time.sleep(0.2)  # be polite to the public API
        if len(traders) >= cc.watchlist_size:
            break

    cache.save()
    # Win rate first — a verified record of being right matters more than
    # the dollar size of wins. Profitability floors already guaranteed above.
    # Rank by edge (profit per bet), not raw win rate — a high-win-rate
    # favorites bettor can be less profitable than a lower-win-rate underdog
    # specialist. Win rate breaks ties.
    traders.sort(key=lambda t: (t.winrate_edge, t.win_rate), reverse=True)
    log.info("discovered %d sports traders from leaderboard", len(traders))
    return traders


def sport_leaders(cfg: Config, sports: List[str], min_bets: int = 30,
                  top_n: int = 3, reuse_cache: bool = False) -> tuple:
    """Find the top traders per sport, ranked by per-sport PnL (edge tiebreaker).

    Qualify gate: ≥ min_bets resolved bets in the sport, positive edge, AND
    positive PnL (a money-loser can't be a "top" trader). Reuses the same
    candidate pool + cheap quality gates + bot screen as the watchlist.
    Read-only — touches no watchlist. Returns (results, n_evaluated).

    The expensive per-candidate evaluation is cached to state/sport_eval.json
    so ranking tweaks (min_bets, metric) can re-run instantly via reuse_cache.
    """
    cc = cfg.copytrade
    eval_path = Path(cfg.resolution_cache_file).parent / "sport_eval.json"

    if reuse_cache and eval_path.exists():
        evaluated = json.loads(eval_path.read_text()).get("evaluated", [])
        log.info("sport-leaders: reusing %d cached evaluations", len(evaluated))
        return _rank_sport_leaders(evaluated, sports, min_bets, top_n), len(evaluated)

    cache = ResolutionCache(cfg.resolution_cache_file)
    by_pnl = _leaderboard("month", cc.leaderboard_depth, "pnl")
    by_vol = _leaderboard("month", cc.leaderboard_depth, "vol")
    seen = set()
    candidates: List[dict] = []
    for row in by_pnl + by_vol:
        w = row["proxyWallet"].lower()
        if w not in seen:
            seen.add(w)
            candidates.append(row)
    candidates.sort(key=lambda r: float(r.get("pnl") or 0), reverse=True)
    log.info("sport-leaders: evaluating up to %d candidates for %s",
             len(candidates), ", ".join(sports))

    evaluated: List[dict] = []
    now_ts = int(time.time())
    for row in candidates:
        wallet = row["proxyWallet"].lower()
        pnl_1m = float(row.get("pnl") or 0)
        vol_1m = float(row.get("vol") or 0)
        # Same cheap gates as the watchlist — real, durable, active, non-bot
        # sports traders. (No overall win-rate gate: a trader can be elite at
        # one sport while mediocre overall.)
        if pnl_1m < cc.min_monthly_pnl:
            continue
        pnl_1w = _wallet_stat("profit", "7d", wallet)
        if pnl_1w < -cc.max_weekly_loss_vs_month * pnl_1m:
            continue
        roi = pnl_1m / vol_1m if vol_1m > 0 else 0.0
        if vol_1m > 0 and roi < cc.min_roi:
            continue
        pnl_all = _wallet_stat("profit", "all", wallet)
        if pnl_all < cc.min_alltime_pnl:
            continue
        if pnl_all - pnl_1m < cc.min_prior_pnl:
            continue
        vol_all = _wallet_stat("volume", "all", wallet)
        roi_all = pnl_all / vol_all if vol_all > 0 else 0.0
        if vol_all > 0 and roi_all < cc.min_alltime_roi:
            continue
        trades = _recent_trades(wallet, cc.trades_sample)
        if len(trades) < cc.min_trades_sampled:
            continue
        newest_ts = max((int(t.get("timestamp") or 0) for t in trades), default=0)
        if now_ts - newest_ts > cc.max_days_inactive * 86400:
            continue
        trades_30d = sum(1 for t in trades if int(t.get("timestamp") or 0) > now_ts - 30 * 86400)
        if trades_30d < cc.min_trades_30d:
            continue
        share = sum(1 for t in trades if is_sports_slug(t.get("eventSlug", ""))) / len(trades)
        if share < cc.min_sports_share:
            continue
        style = classify_trading_style(trades)
        if cc.exclude_bots and style["style"] == "bot":
            continue

        # Passed the gates — compute the full per-sport record (deep to cap).
        unresolved: set = set()
        rec = compute_track_record(trades, cache, cc.max_markets_checked, unresolved)
        offset = len(trades)
        while (rec["resolved_markets"] < cc.max_markets_checked
               and offset < cc.max_trades_depth):
            more = _recent_trades(wallet, cc.trades_sample, offset)
            if not more:
                break
            trades.extend(more)
            offset += len(more)
            rec = compute_track_record(trades, cache, cc.max_markets_checked, unresolved)
        name = row.get("userName") or trades[0].get("pseudonym") or wallet[:10]
        evaluated.append({
            "name": name, "wallet": wallet, "pnl_all": round(pnl_all, 2),
            "style": style["style"], "by_sport": rec["by_sport"],
        })
        log.info("sport-leaders: evaluated %d (%s)", len(evaluated), name)
        time.sleep(0.1)

    cache.save()
    eval_path.write_text(json.dumps(
        {"computed_at": datetime.now(timezone.utc).isoformat(), "evaluated": evaluated}))
    return _rank_sport_leaders(evaluated, sports, min_bets, top_n), len(evaluated)


def _rank_sport_leaders(evaluated: List[dict], sports: List[str],
                        min_bets: int, top_n: int) -> Dict[str, List[dict]]:
    """Rank evaluated candidates within each sport: gate on min bets + positive
    edge + positive PnL, then rank by PnL (edge tiebreaker)."""
    results: Dict[str, List[dict]] = {}
    for sport in sports:
        ranked = []
        for e in evaluated:
            r = e["by_sport"].get(sport)
            if not r or r["markets"] < min_bets or r["edge"] <= 0 or r["pnl"] <= 0:
                continue
            ranked.append({**e, "sport_rec": r})
        ranked.sort(key=lambda x: (x["sport_rec"]["pnl"], x["sport_rec"]["edge"]),
                    reverse=True)
        results[sport] = ranked[:top_n]
    return results


def _is_game_moneyline(q: str) -> bool:
    """True only for the full game/fight moneyline ("Team A vs. Team B" or
    "UFC ...: A vs. B"). Excludes spreads, totals, props, and sub-period
    markets like "A vs. B: 1H Moneyline".

    Distinguisher: the real moneyline has no colon, or a title-prefix colon
    *before* the "vs" (UFC). Sub-period markets put the colon *after* the
    matchup.
    """
    ql = q.lower()
    if " vs" not in ql or "spread" in ql or "o/u" in ql:
        return False
    if ":" in q and q.index(":") > ql.index(" vs"):
        return False
    return True


def _is_full_game_alt(q: str) -> bool:
    """Full-game total (O/U) or spread. Excludes sub-period (innings) markets
    and yes/no props, which all contain "inning" in MLB."""
    ql = q.lower()
    if "inning" in ql:
        return False
    if " vs" in ql and "o/u" in ql:          # "Team vs. Team: O/U 8.5"
        return True
    if ql.strip().startswith("spread:"):     # "Spread: Team (-1.5)"
        return True
    return False


def _season_moneyline_markets(series_id: int, since: Optional[str] = None,
                              include_alt: bool = False,
                              min_volume: float = 0.0) -> List[tuple]:
    """All closed game markets in a series, as
    [(conditionId, finals_prices, event_slug)]. `since` (YYYY-MM-DD) bounds an
    ongoing series (e.g. UFC) to recent events; None enumerates the whole
    series (bounded season series like NFL/CFB). With include_alt=True, also
    picks up full-game totals (O/U) and spreads — but then min_volume should be
    set to skip the many dead alt-lines each game carries."""
    out: List[tuple] = []
    off = 0
    params = {"series_id": series_id, "closed": "true", "limit": 100}
    if since:
        params.update({"order": "endDate", "ascending": "false"})
    while True:
        p = dict(params)
        p["offset"] = off
        resp = _session.get(f"{GAMMA_API}/events", params=p, timeout=30)
        if not resp.ok:
            break
        evs = resp.json()
        if not evs:
            break
        stop = False
        for ev in evs:
            if since and (ev.get("endDate") or "")[:10] < since:
                stop = True  # desc order — everything after this is older too
                break
            for m in ev.get("markets", []):
                q = m.get("question") or ""
                ok = _is_game_moneyline(q) or (include_alt and _is_full_game_alt(q))
                if not m.get("closed") or not ok:
                    continue
                if min_volume:
                    try:
                        if float(m.get("volume") or 0) < min_volume:
                            continue
                    except (TypeError, ValueError):
                        continue
                cid = m.get("conditionId")
                try:
                    finals = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                except (ValueError, TypeError):
                    finals = []
                if cid and finals and max(finals) >= 0.99:  # cleanly resolved
                    out.append((cid, finals, ev.get("slug")))
        off += len(evs)
        if len(evs) < 100 or stop:
            break
    return out


def _trades_since(wallet: str, since_ts: int, max_trades: int = 4000) -> List[dict]:
    """A wallet's trades newer than since_ts, newest-first. Pages until the
    cutoff (or the cap) — a single 100-trade page silently drops fills for
    hyperactive position-builders whenever the watcher was down a few hours."""
    out: List[dict] = []
    off = 0
    while off < max_trades:
        page = _recent_trades(wallet, 500, off)
        if not page:
            break
        out.extend(page)
        off += len(page)
        if int(page[-1].get("timestamp") or 0) <= since_ts:
            break
    return [t for t in out if int(t.get("timestamp") or 0) > since_ts]


def _current_mid(token_id: str) -> Optional[float]:
    """Current CLOB midpoint for a token, or None. Used to show price drift
    between a tracked trader's entry and 'now' in alerts."""
    if not token_id:
        return None
    try:
        r = _session.get("https://clob.polymarket.com/midpoint",
                         params={"token_id": token_id}, timeout=10)
        if r.ok:
            return float(r.json().get("mid"))
    except (requests.RequestException, ValueError, TypeError):
        pass
    return None


def positions_trades_gap(wallet: str, min_usd: float = 1000,
                         lookback_days: float = 21) -> List[dict]:
    """Data-completeness tripwire: open positions >= min_usd whose market never
    appears in the wallet's recent trade feed. The takerOnly bug hid maker
    fills exactly this way (visible in /positions, absent from /trades) and
    went unnoticed for months. A hit means either the trades API is dropping
    fills again, or the position predates the lookback (e.g. a futures bet) —
    either way, worth an eyeball rather than silence."""
    try:
        r = _session.get(f"{DATA_API}/positions",
                         params={"user": wallet, "limit": 500}, timeout=30)
        poss = r.json() if r.ok else []
    except (requests.RequestException, ValueError):
        return []
    since = int(time.time() - lookback_days * 86400)
    seen = {t.get("conditionId") for t in _trades_since(wallet, since, max_trades=4000)}
    gaps = []
    for p in poss:
        usd = float(p.get("size") or 0) * float(p.get("avgPrice") or 0)
        if usd < min_usd or p.get("conditionId") in seen or p.get("redeemable"):
            continue
        gaps.append({"conditionId": p.get("conditionId"),
                     "title": p.get("title") or p.get("slug") or "?",
                     "usd": round(usd, 2)})
    return gaps


def weekly_wallet_pnl(cfg: Config, days: float = 7) -> List[dict]:
    """For each tracked wallet: net PnL over the last `days` (Polymarket's
    official figure) plus a per-sport breakdown of bets placed this week that
    have resolved. Read-only."""
    from .tracked import TrackedList
    tracked = TrackedList(cfg.tracked_wallets_file)
    cache = ResolutionCache(cfg.resolution_cache_file)
    cutoff = int(time.time()) - int(days * 86400)
    window = "7d" if abs(days - 7) < 0.5 else ("30d" if abs(days - 30) < 2 else "all")
    reports: List[dict] = []
    for w in tracked.wallets:
        recent = _trades_since(w.wallet, cutoff - 1, max_trades=6000)
        rec = compute_track_record(recent, cache, 100000)
        bet_cids = {t.get("conditionId") for t in recent if t.get("conditionId")}
        reports.append({
            "label": w.label or w.wallet[:10], "wallet": w.wallet,
            "net_official": round(_wallet_stat("profit", window, w.wallet), 2),
            "net_30d": round(_wallet_stat("profit", "30d", w.wallet), 2),
            "resolved_pnl": round(sum(x["pnl"] for x in rec["by_sport"].values()), 2),
            "by_sport": rec["by_sport"],
            "resolved_markets": rec["resolved_markets"],
            "pending": max(0, len(bet_cids) - rec["resolved_markets"]),
        })
    cache.save()
    reports.sort(key=lambda r: r["net_official"], reverse=True)
    return reports, window


def _market_trades(cid: str, max_pages: int = 60) -> List[dict]:
    """All trades on a market, paginated. Cap is high because busy game markets
    can carry well over 4k fills — truncating drops heavy bettors' positions and
    made position-builders (e.g. Talvez10) vanish from the reverse-lookup. Light
    markets still exit early once a short page is returned."""
    trades: List[dict] = []
    off = 0
    for _ in range(max_pages):
        try:
            resp = _session.get(f"{DATA_API}/trades",
                                params={"market": cid, "limit": 500, "offset": off,
                                        "takerOnly": "false"}, timeout=30)
            if not resp.ok:
                break
            page = resp.json()
        except (requests.RequestException, ValueError):
            break
        if not page:
            break
        trades.extend(page)
        off += len(page)
        if len(page) < 500:
            break
    return trades


# Heavy-bettor rescue: markets too busy to fully page are EXCLUDED from
# reconstruction (see season_sport_leaders) so partial data can't garble
# anyone's record — but that also thins the records of exactly the traders
# who live in those mega markets (Talvez10: true MLB +$408k, invisible).
# Wallets that fail the gates but wagered meaningfully get a second chance
# on their true, wallet-side record during vetting.
HEAVY_RESCUE_WAGERED = 10_000
HEAVY_RESCUE_MAX = 60

# Season scan names -> compute_track_record's sport buckets (slug-derived).
_SEASON_TO_BUCKET = {"UFC": "MMA", "CFB": "NCAAF"}


def _walletside_sport_record(cfg: Config, wallet: str, sport_bucket: str,
                             since: str) -> Optional[dict]:
    """A wallet's TRUE record for one sport since a date, from its own complete
    trade history — the accurate accounting the market-side scan can't give."""
    cut = int(datetime.fromisoformat(since).replace(
        tzinfo=timezone.utc).timestamp())
    trades = _trades_since(wallet, cut, max_trades=20000)
    if not trades:
        return None
    cache = ResolutionCache(cfg.resolution_cache_file)
    rec = compute_track_record(trades, cache, 100000)
    cache.save()
    return rec["by_sport"].get(sport_bucket)


def _sort_cands(cands: List[dict], rank_by: str, min_avg_bet: float) -> List[dict]:
    """Filter out noise micro-bettors (tiny avg bet = high edge is just variance)
    then sort by the chosen metric."""
    cands = [c for c in cands if c.get("avg_bet", 0) >= min_avg_bet]
    _key = {"pnl": lambda x: (x["pnl"], x["edge"]),
            "edge": lambda x: (x["edge"], x["pnl"]),
            "winrate": lambda x: (x["win_rate"], x["pnl"])}.get(rank_by)
    cands.sort(key=_key or (lambda x: (x["pnl"], x["edge"])), reverse=True)
    return cands


def season_sport_leaders(cfg: Config, sport: str, min_bets: int = 20,
                         top_n: int = 3, since: Optional[str] = None,
                         rank_by: str = "pnl", reuse_cache: bool = False,
                         min_avg_bet: float = 0.0, include_alt: bool = False,
                         min_volume: float = 0.0) -> tuple:
    """Reverse-lookup top traders for a sport over a season via its game markets:
    enumerate the markets, pull every trade, reconstruct each wallet's record,
    gate (min bets + positive edge + positive PnL + min avg bet), rank by
    `rank_by` (pnl | edge | winrate), then bot-screen the leaders. min_avg_bet
    cuts noise micro-bettors whose high edge is variance on $1-$40 wagers.
    `since` bounds an ongoing series like UFC. With include_alt=True the scan
    also covers full-game totals & spreads (set min_volume to skip dead alt-
    lines) — this surfaces totals/spreads specialists the moneyline-only scan
    misses. Cached (separately per market scope) for instant re-ranking.
    Returns (ranked, n_markets, n_wallets)."""
    cc = cfg.copytrade
    tag = "_all" if include_alt else ""
    eval_path = Path(cfg.resolution_cache_file).parent / f"season_eval_{sport}{tag}.json"
    if reuse_cache and eval_path.exists():
        data = json.loads(eval_path.read_text())
        cands = _sort_cands(data["cands"], rank_by, min_avg_bet)
        ranked = _vet_season_leaders(cfg, cands, top_n, sport, since, min_bets,
                                     data.get("heavy", []), rank_by)
        return ranked, data.get("n_markets", 0), data.get("n_wallets", 0)
    series_id = SEASON_SERIES[sport]
    markets = _season_moneyline_markets(series_id, since=since,
                                        include_alt=include_alt, min_volume=min_volume)
    log.info("%s: %d resolved game markets in the season", sport, len(markets))
    finals_by_cid = {cid: finals for cid, finals, _ in markets}

    # Aggregate every wallet's per-market position across the whole season.
    per: Dict[str, Dict[str, dict]] = {}
    truncated: set = set()
    for i, (cid, _finals, _slug) in enumerate(markets):
        mkt_trades = _market_trades(cid)
        if len(mkt_trades) >= 30_000:  # hit the page cap: data incomplete.
            # Partial fills mis-score every wallet in the market (seen buys
            # without the rest -> wrong PnL sign). Better no data than wrong
            # data: drop the market; heavy-rescue re-checks the big fish.
            truncated.add(cid)
            continue
        for t in mkt_trades:
            w = (t.get("proxyWallet") or "").lower()
            idx = t.get("outcomeIndex")
            if not w or idx is None:
                continue
            rec = per.setdefault(w, {}).setdefault(
                cid, {"buy_usd": 0.0, "sell_usd": 0.0, "buy_shares": 0.0, "net": {}})
            sh = float(t.get("size") or 0)
            pr = float(t.get("price") or 0)
            if t.get("side") == "BUY":
                rec["buy_usd"] += sh * pr
                rec["buy_shares"] += sh
                rec["net"][idx] = rec["net"].get(idx, 0.0) + sh
            elif t.get("side") == "SELL":
                rec["sell_usd"] += sh * pr
                rec["net"][idx] = rec["net"].get(idx, 0.0) - sh
        if (i + 1) % 25 == 0:
            log.info("%s: processed %d/%d markets, %d wallets so far",
                     sport, i + 1, len(markets), len(per))

    # Per-wallet season record.
    cands = []
    heavy_pool: List[dict] = []
    for w, mkts in per.items():
        wins = losses = 0
        entry_usd = entry_shares = pnl_total = 0.0
        for cid, rec in mkts.items():
            if rec["buy_shares"] <= 0 or any(v < -1e-6 for v in rec["net"].values()):
                continue
            finals = finals_by_cid[cid]
            fv = sum(sh * (finals[i] if i < len(finals) else 0.0)
                     for i, sh in rec["net"].items())
            pnl = rec["sell_usd"] + fv - rec["buy_usd"]
            wins += 1 if pnl > 0 else 0
            losses += 0 if pnl > 0 else 1
            entry_usd += rec["buy_usd"]
            entry_shares += rec["buy_shares"]
            pnl_total += pnl
        n = wins + losses
        if n <= 0:
            continue
        wr = wins / n
        ae = entry_usd / entry_shares if entry_shares else 0.0
        edge = wr - ae
        row = {"wallet": w, "markets": n, "win_rate": round(wr, 4),
               "avg_entry": round(ae, 4), "edge": round(edge, 4),
               "pnl": round(pnl_total, 2),
               "wagered": round(entry_usd, 2),
               "avg_bet": round(entry_usd / n, 2)}
        if n < min_bets or edge <= 0 or pnl_total <= 0:
            # Thin or failed market-side record but meaningful money — often
            # a mega-market specialist whose markets we dropped as unpageable.
            # Vetting re-checks these wallet-side (true record, real gates).
            if entry_usd >= HEAVY_RESCUE_WAGERED:
                heavy_pool.append(row)
            continue
        cands.append(row)
    heavy_pool.sort(key=lambda x: -x["wagered"])
    heavy_pool = heavy_pool[:HEAVY_RESCUE_MAX]
    if truncated:
        log.info("%s: %d/%d markets dropped as unpageable (>30k fills) — "
                 "records computed from clean markets only", sport,
                 len(truncated), len(markets))
    # Cache the full qualifying list so we can re-rank by any metric instantly.
    eval_path = Path(cfg.resolution_cache_file).parent / f"season_eval_{sport}{tag}.json"
    eval_path.write_text(json.dumps({
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_markets": len(markets), "n_wallets": len(per),
        "cands": cands, "heavy": heavy_pool}))
    cands = _sort_cands(cands, rank_by, min_avg_bet)
    log.info("%s: %d wallets cleared gates, %d heavy-rescue (ranking by %s)",
             sport, len(cands), len(heavy_pool), rank_by)

    ranked = _vet_season_leaders(cfg, cands, top_n, sport, since, min_bets,
                                 heavy_pool, rank_by)
    return ranked, len(markets), len(per)


def _vet_season_leaders(cfg: Config, cands: List[dict], top_n: int,
                        sport: Optional[str] = None, since: Optional[str] = None,
                        min_bets: int = 0, heavy: Optional[List[dict]] = None,
                        rank_by: str = "pnl") -> List[dict]:
    """Vet top candidates (all-time profitability + bot screen). When `since`
    is given, each finalist's sport record is additionally RE-VERIFIED from
    their own complete trade history and the board is ranked on the verified
    numbers — the market-side reconstruction garbles heavy position-builders.
    `heavy` wallets (gate-failed but big wagers) get the same wallet-side
    second look and join the board if their true record passes."""
    cc = cfg.copytrade
    bucket = _SEASON_TO_BUCKET.get(sport or "", sport)
    verify = bool(since and bucket)

    def _vet_one(c: dict) -> Optional[dict]:
        pnl_all = _wallet_stat("profit", "all", c["wallet"])
        vol_all = _wallet_stat("volume", "all", c["wallet"])
        roi_all = pnl_all / vol_all if vol_all > 0 else 0.0
        if pnl_all < cc.min_alltime_pnl:
            return None
        sample = _recent_trades(c["wallet"], 500)
        style = classify_trading_style(sample) if sample else {"style": "uncertain"}
        if cc.exclude_bots and style["style"] == "bot":
            return None
        name = (sample[0].get("pseudonym") if sample else "") or c["wallet"][:10]
        entry = {**c, "name": name, "pnl_all": round(pnl_all, 2),
                 "roi_all": round(roi_all, 4), "style": style["style"]}
        if verify:
            ws = _walletside_sport_record(cfg, c["wallet"], bucket, since)
            if (not ws or ws["markets"] < min_bets
                    or ws["pnl"] <= 0 or ws["edge"] <= 0):
                return None  # true record too thin or doesn't hold up
            entry.update(markets=ws["markets"], win_rate=ws["win_rate"],
                         avg_entry=ws["avg_entry"], edge=ws["edge"],
                         pnl=round(ws["pnl"], 2), verified=True)
        return entry

    verified: List[dict] = []
    want = max(top_n * 2, top_n + 3)  # buffer: verified stats reshuffle ranks
    for c in cands[:250]:  # look well past bot-heavy top ranks (esp. MLB)
        e = _vet_one(c)
        if e:
            verified.append(e)
            if len(verified) >= want:
                break
    if verify and heavy:
        seen = {v["wallet"] for v in verified}
        for c in heavy:
            if c["wallet"] in seen:
                continue
            e = _vet_one(c)
            if e:
                e["rescued"] = True
                verified.append(e)
    verified = _sort_cands(verified, rank_by, 0.0)
    return verified[:top_n]


# ------------------------------------------------------------ watchlist ----

class Watchlist:
    def __init__(self, path: str):
        self.path = Path(path)
        self.traders: List[WatchedTrader] = []
        self.refreshed_at: Optional[str] = None
        self.last_attempt_at: Optional[str] = None  # last refresh ATTEMPT (success or fail)
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.refreshed_at = data.get("refreshed_at")
            self.last_attempt_at = data.get("last_attempt_at")
            self.traders = [WatchedTrader(**t) for t in data.get("traders", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "refreshed_at": self.refreshed_at,
            "last_attempt_at": self.last_attempt_at,
            "traders": [asdict(t) for t in self.traders],
        }, indent=1))

    def stale(self, max_age_days: float) -> bool:
        if not self.refreshed_at or not self.traders:
            return True
        age = datetime.now(timezone.utc) - datetime.fromisoformat(self.refreshed_at)
        return age.total_seconds() > max_age_days * 86400

    def should_refresh(self, max_age_days: float, retry_hours: float) -> bool:
        """Refresh only if the list is stale AND we haven't just attempted one.
        Without the attempt cooldown, a refresh that fails (e.g. a network
        blip mid-scan) would retry the ~40-min scan every poll cycle, starving
        signal polling. We keep alerting on the existing valid list meanwhile.
        Exception: if we have no usable list at all, always try."""
        if not self.traders:
            return True
        if not self.stale(max_age_days):
            return False
        if self.last_attempt_at:
            since = datetime.now(timezone.utc) - datetime.fromisoformat(self.last_attempt_at)
            if since.total_seconds() < retry_hours * 3600:
                return False
        return True

    def refresh(self, cfg: Config) -> None:
        # Record the attempt up front (and persist) so a failure partway
        # through still starts the retry cooldown.
        self.last_attempt_at = datetime.now(timezone.utc).isoformat()
        self.save()
        # Preserve last_seen_ts for wallets that stay on the list so we
        # don't re-report old trades after a refresh.
        seen = {t.wallet: t.last_seen_ts for t in self.traders}
        # Grandfather current members: re-vet them even if they've slipped off
        # the leaderboards or a dollar floor, so we never lose a verified
        # winner to a technicality (they still must pass the real gates).
        new_traders = discover_sports_traders(cfg, grandfather_wallets=list(seen.keys()))
        for t in new_traders:
            t.last_seen_ts = max(t.last_seen_ts, seen.get(t.wallet, 0))
        self.traders = new_traders
        self.refreshed_at = datetime.now(timezone.utc).isoformat()
        self.save()


def _parse_game_start(raw: Optional[str]) -> Optional[datetime]:
    """Gamma returns gameStartTime like '2026-06-10 22:35:00+00' (or ISO-ish
    variants). Returns an aware datetime or None."""
    if not raw:
        return None
    s = raw.strip().replace(" ", "T", 1).replace("Z", "+00:00")
    # normalize a bare '+00' / '+0000' offset to '+00:00'
    if s.endswith("+00"):
        s += ":00"
    elif s.endswith("+0000"):
        s = s[:-5] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _market_by_slug(slug: Optional[str]) -> Optional[dict]:
    if not slug:
        return None
    try:
        resp = _session.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
        if resp.ok:
            rows = resp.json()
            return rows[0] if rows else None
    except requests.RequestException:
        pass
    return None


# ----------------------------------------------------------- copy scout ----

def run_copytrade_scout(cfg: Config, watchlist: Watchlist, held_market_ids: List[str],
                        report_all_sells: bool = False) -> dict:
    """Check watched wallets for new trades. Returns a scout-report dict:
    fresh sports buys (entry signals) and sells (exit signals).

    report_all_sells=True reports every sizable sports sell by a watched
    trader, not just ones in markets we hold — used by manual watch/alert
    mode, where the bot can't know what the human copied."""
    cc = cfg.copytrade
    if watchlist.should_refresh(cc.refresh_days, cc.refresh_retry_hours):
        log.info("watchlist stale — refreshing from leaderboard")
        try:
            watchlist.refresh(cfg)
        except requests.RequestException as e:
            # last_attempt_at was already persisted in refresh(), so the retry
            # cooldown is in effect; we keep polling the existing list.
            log.error("watchlist refresh failed (will retry in %.0fh): %s",
                      cc.refresh_retry_hours, e)

    signals: List[dict] = []
    exit_signals: List[dict] = []
    held = set(held_market_ids)

    for trader in watchlist.traders:
        try:
            trades = _recent_trades(trader.wallet, 50)
        except requests.RequestException:
            continue
        fresh = [t for t in trades if int(t.get("timestamp") or 0) > trader.last_seen_ts]
        if trades:
            trader.last_seen_ts = max(trader.last_seen_ts,
                                      max(int(t.get("timestamp") or 0) for t in trades))
        for t in fresh:
            usd = float(t.get("size") or 0) * float(t.get("price") or 0)
            slug_ok = is_sports_slug(t.get("eventSlug", ""))
            sig = {
                "market_slug": t.get("slug"),
                "trader": trader.pseudonym,
                "trader_wallet": trader.wallet,
                "trader_win_rate": trader.win_rate,
                "trader_winrate_edge": trader.winrate_edge,
                "trader_resolved_markets": trader.resolved_markets,
                "trader_pnl_1m": trader.pnl_1m,
                "trader_sports_share": trader.sports_share,
                "side": t.get("side"),
                "title": t.get("title"),
                "event_slug": t.get("eventSlug"),
                "token_id": t.get("asset"),
                "outcome": t.get("outcome"),
                "their_price": float(t.get("price") or 0),
                "their_usd": round(usd, 2),
                "ts": int(t.get("timestamp") or 0),
            }
            if t.get("side") == "SELL":
                # Smart money leaving: relevant if we hold anything (the
                # decision agent matches against the portfolio) or, in manual
                # watch mode, for any sizable sports sell.
                if held or (report_all_sells and slug_ok and usd >= cc.min_their_trade_usd):
                    exit_signals.append(sig)
                continue
            if t.get("side") != "BUY" or not slug_ok:
                continue
            if usd < cc.min_their_trade_usd:
                continue
            if not (cc.min_copy_price <= sig["their_price"] <= cc.max_copy_price):
                continue
            signals.append(sig)
    watchlist.save()

    # Verify current prices: only keep signals where the market hasn't already
    # run away from the watched trader's entry.
    reports: List[dict] = []
    if signals:
        books = fetch_books(list({s["token_id"] for s in signals if s["token_id"]}))
        # Aggregate by token: multiple smart wallets on the same side is a stronger signal.
        by_token: Dict[str, List[dict]] = {}
        for s in signals:
            by_token.setdefault(s["token_id"], []).append(s)
        now_ts = int(time.time())
        for token_id, sigs in by_token.items():
            book = books.get(token_id)
            if not book or book.best_ask is None:
                continue
            # Stale signals: by the time we act, the edge is the market's, not ours.
            newest = max(s["ts"] for s in sigs)
            if now_ts - newest > cc.max_signal_age_minutes * 60:
                continue
            avg_entry = sum(s["their_price"] * s["their_usd"] for s in sigs) / sum(s["their_usd"] for s in sigs)
            drift = book.best_ask - avg_entry
            # Symmetric: up-drift means we missed the move; down-drift means new
            # information arrived against the thesis (often the game itself).
            if abs(drift) > cc.max_price_drift:
                log.info("copy signal skipped (price drifted %+.3f): %s", drift, sigs[0]["title"])
                continue
            market = _market_by_slug(sigs[0].get("market_slug"))
            if not market or not market.get("acceptingOrders"):
                continue
            # Never copy into a game already underway — in-game prices move
            # faster than an hourly bot. Pre-game entries only.
            start_dt = _parse_game_start(market.get("gameStartTime"))
            if start_dt and start_dt <= datetime.now(timezone.utc):
                log.info("copy signal skipped (game already started): %s", sigs[0]["title"])
                continue
            reports.append({
                "market_id": str(market.get("id")),
                "end_date": market.get("endDate"),
                "title": sigs[0]["title"],
                "event_slug": sigs[0]["event_slug"],
                "token_id": token_id,
                "outcome": sigs[0]["outcome"],
                "current_best_ask": book.best_ask,
                "current_best_bid": book.best_bid,
                "smart_money_avg_entry": round(avg_entry, 4),
                "num_watched_traders": len({s["trader_wallet"] for s in sigs}),
                "total_smart_money_usd": round(sum(s["their_usd"] for s in sigs), 2),
                "traders": [
                    {"name": s["trader"], "win_rate": s["trader_win_rate"],
                     "winrate_edge": s["trader_winrate_edge"],
                     "win_rate_sample": s["trader_resolved_markets"],
                     "pnl_1m": s["trader_pnl_1m"],
                     "bet_usd": s["their_usd"], "at_price": s["their_price"]}
                    for s in sigs
                ],
            })

    log.info("copytrade scout: %d entry signals, %d exit signals", len(reports), len(exit_signals))
    return {
        "scout": "copytrade",
        "reports": reports,
        "exit_signals": exit_signals,
        "scan_notes": f"watching {len(watchlist.traders)} sports traders "
                      f"(refreshed {watchlist.refreshed_at})",
    }
