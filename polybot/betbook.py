"""Per-wallet bet book: every settled bet a tracked wallet has made, one row
per market, kept on the state volume and extended incrementally.

Why: re-downloading and re-scoring a heavy wallet's whole history every few
days (Soarin22: ~90k fills, 1-2 min) pauses alert polling, and the history
never changes. So the first refresh pulls everything once; after that only
fills since the last sync are fetched and folded in, and any position whose
market has since settled is scored and moved into the book.

Rows carry exactly what compute_track_record uses (stake, shares, pnl, win,
sport), so lifetime records match it, and the newest N rows per sport give
recent form ("last 100 bets") with no extra API calls.

Layout: STATE_DIR/bets/<wallet>.json
  synced_to   newest fill timestamp folded in
  tail_keys   identities of fills in the overlap window (dedupe on re-read)
  fills       fills processed so far
  first_ts / last_ts   span of the history
  scored      cid -> {sport, staked, shares, pnl, win, last_ts}
  open        cid -> position not yet settled (same accounting as scoring)
  skipped     cids that can never be scored (void 50/50, incomplete history)
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config
from .copytrade import (ResolutionCache, SIZE_BUCKETS, _bucket_label, _session,
                        _trades_window_full, sport_from_slug, wallet_full_history)

log = logging.getLogger("polybot.betbook")

OVERLAP_S = 3600            # re-read the last hour each sync: late-indexed fills
VOID_CHECK_AFTER_S = 14 * 86400   # an "open" bet this stale may be a void market
RECENT_N = 100


def _path(cfg: Config, wallet: str) -> Path:
    d = Path(cfg.alerts_log_file).parent / "bets"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{wallet.lower()}.json"


def _key(t: dict) -> str:
    return "|".join(str(t.get(k)) for k in
                    ("transactionHash", "asset", "side", "size", "price", "timestamp"))


def load(cfg: Config, wallet: str) -> Optional[dict]:
    p = _path(cfg, wallet)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        log.warning("bet book for %s unreadable — will rebuild", wallet[:10])
        return None


def _save(cfg: Config, wallet: str, book: dict) -> None:
    p = _path(cfg, wallet)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(book, separators=(",", ":")))
    tmp.replace(p)                       # atomic: a crash never leaves half a book


def _apply(book: dict, fills: List[dict]) -> None:
    """Fold fills into open positions — same accounting as compute_track_record."""
    for t in fills:
        cid, idx = t.get("conditionId"), t.get("outcomeIndex")
        ts = int(t.get("timestamp") or 0)
        book["fills"] += 1
        book["first_ts"] = min(book["first_ts"] or ts, ts)
        book["last_ts"] = max(book["last_ts"], ts)
        if not cid or idx is None or cid in book["scored"] or cid in book["skipped"]:
            continue
        p = book["open"].setdefault(cid, {"sport": sport_from_slug(t.get("eventSlug") or ""),
                                          "buy_usd": 0.0, "sell_usd": 0.0,
                                          "buy_shares": 0.0, "net": {}, "last_ts": 0})
        sh, pr = float(t.get("size") or 0), float(t.get("price") or 0)
        k = str(idx)
        if t.get("side") == "BUY":
            p["buy_usd"] += sh * pr
            p["buy_shares"] += sh
            p["net"][k] = p["net"].get(k, 0.0) + sh
        elif t.get("side") == "SELL":
            p["sell_usd"] += sh * pr
            p["net"][k] = p["net"].get(k, 0.0) - sh
        p["last_ts"] = max(p["last_ts"], ts)


def _market_closed(cid: str) -> bool:
    try:
        r = _session.get(f"https://clob.polymarket.com/markets/{cid}", timeout=15)
        return r.ok and bool(r.json().get("closed"))
    except Exception:  # noqa: BLE001
        return False


def _settle(book: dict, cache: ResolutionCache) -> int:
    """Score every open position whose market has resolved. Returns how many."""
    now, moved = time.time(), 0
    for cid in list(book["open"]):
        p = book["open"][cid]
        finals = cache.get(cid)
        if finals is None:
            # Never resolves with a winner (void / 50-50): retire it once it's
            # stale and the market is closed, or it would be re-queried forever.
            if now - p["last_ts"] > VOID_CHECK_AFTER_S and _market_closed(cid):
                book["skipped"].append(cid)
                del book["open"][cid]
            continue
        del book["open"][cid]
        if p["buy_shares"] <= 0 or any(v < -1e-6 for v in p["net"].values()):
            book["skipped"].append(cid)      # sold more than bought: incomplete
            continue
        fv = sum(sh * (finals[int(i)] if int(i) < len(finals) else 0.0)
                 for i, sh in p["net"].items())
        pnl = p["sell_usd"] + fv - p["buy_usd"]
        book["scored"][cid] = {"sport": p["sport"], "staked": round(p["buy_usd"], 2),
                               "shares": round(p["buy_shares"], 4), "pnl": round(pnl, 2),
                               "win": pnl > 0, "last_ts": p["last_ts"]}
        moved += 1
    return moved


def sync(cfg: Config, wallet: str, cache: ResolutionCache, max_fills: int) -> dict:
    """Bring a wallet's bet book up to date. First call pulls the full history
    (slow, once); later calls fetch only fills since the last sync (seconds)."""
    book = load(cfg, wallet)
    if book is None:
        fills = wallet_full_history(wallet, max_fills)
        book = {"wallet": wallet.lower(), "synced_to": 0, "tail_keys": [], "fills": 0,
                "first_ts": 0, "last_ts": 0, "scored": {}, "open": {}, "skipped": []}
        _apply(book, sorted(fills, key=lambda t: int(t.get("timestamp") or 0)))
        newest = max((int(t.get("timestamp") or 0) for t in fills), default=0)
        tail = [t for t in fills if int(t.get("timestamp") or 0) >= newest - OVERLAP_S]
    else:
        lo = max(0, book["synced_to"] - OVERLAP_S)
        got = _trades_window_full({"user": wallet}, lo, int(time.time()) + 86400)
        seen = set(book["tail_keys"])
        fresh = [t for t in got if _key(t) not in seen]
        _apply(book, sorted(fresh, key=lambda t: int(t.get("timestamp") or 0)))
        newest = max([book["synced_to"]] + [int(t.get("timestamp") or 0) for t in got])
        tail = [t for t in got if int(t.get("timestamp") or 0) >= newest - OVERLAP_S]
    book["synced_to"] = newest
    book["tail_keys"] = [_key(t) for t in tail]
    _settle(book, cache)
    _save(cfg, wallet, book)
    return book


def _summarize(rows: List[dict]) -> dict:
    n = len(rows)
    wins = sum(1 for r in rows if r["win"])
    staked = sum(r["staked"] for r in rows)
    shares = sum(r["shares"] for r in rows)
    wr = wins / n if n else 0.0
    entry = staked / shares if shares else 0.0
    return {"markets": n, "wins": wins, "pnl": round(sum(r["pnl"] for r in rows), 2),
            "win_rate": round(wr, 4), "avg_entry": round(entry, 4),
            "edge": round(wr - entry, 4)}


def records(book: dict, recent_n: int = RECENT_N) -> dict:
    """Lifetime by sport, recent form by sport (newest recent_n bets), and
    lifetime by stake size — the shapes TrackedWallet already stores."""
    rows = sorted(book["scored"].values(), key=lambda r: r["last_ts"], reverse=True)
    by_sport: Dict[str, List[dict]] = {}
    for r in rows:
        by_sport.setdefault(r["sport"], []).append(r)
    lifetime = {s: _summarize(rs) for s, rs in by_sport.items()}
    recent = {s: _summarize(rs[:recent_n]) for s, rs in by_sport.items()}
    by_size = {}
    for lo, hi in SIZE_BUCKETS:
        bucket = [r for r in rows if lo <= r["staked"] < hi]
        if bucket:
            s = _summarize(bucket)
            wagered = sum(r["staked"] for r in bucket)
            s.update({"lo": lo, "hi": hi, "label": _bucket_label(lo, hi),
                      "wagered": round(wagered, 2),
                      "roi": round(s["pnl"] / wagered, 4) if wagered else 0.0})
            by_size[s["label"]] = s
    return {"by_sport": lifetime, "recent_sport": recent, "by_size": by_size}
