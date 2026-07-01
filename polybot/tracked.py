"""Manually-tracked wallets: a raw activity mirror, separate from the
auto-vetted copy scout. You name the wallets; every new trade above a minimum
size (any market, buy or sell) fires a Discord alert. No win-rate vetting —
these are wallets you've explicitly chosen to follow.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import Config
from .copytrade import (_current_mid, _recent_trades, _trades_since,
                        compute_track_record, ResolutionCache, sport_from_slug)

log = logging.getLogger("polybot.tracked")

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_wallet(raw: str) -> Optional[str]:
    """Accept a bare 0x address or a polymarket.com/profile/<addr> URL."""
    s = (raw or "").strip()
    if "/profile/" in s:
        s = s.split("/profile/", 1)[1].split("/")[0].split("?")[0]
    s = s.strip()
    return s.lower() if _ADDR_RE.match(s) else None


@dataclass
class TrackedWallet:
    wallet: str
    label: str = ""
    added_at: str = ""
    last_seen_ts: int = 0      # newest trade timestamp already alerted
    by_sport: dict = field(default_factory=dict)   # sport -> {markets, wins, win_rate, pnl}
    sports_refreshed_at: str = ""                   # when by_sport was last computed
    # Per-position alert state for fill-coalescing: "cid|side|outcome" ->
    # {alerted_usd, pending_usd, ts (last alert), fill_ts (last fill seen)}.
    # Position-builders place one bet as dozens of fills; without this every
    # fill >= tracked_min_usd would fire its own Discord alert.
    open_alerts: dict = field(default_factory=dict)


class TrackedList:
    def __init__(self, path: str):
        self.path = Path(path)
        self.wallets: List[TrackedWallet] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.wallets = [TrackedWallet(**w) for w in data.get("wallets", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"wallets": [asdict(w) for w in self.wallets]}, indent=1))

    def find(self, wallet: str) -> Optional[TrackedWallet]:
        return next((w for w in self.wallets if w.wallet == wallet), None)

    def add(self, wallet: str, label: str = "") -> TrackedWallet:
        existing = self.find(wallet)
        if existing:
            if label:
                existing.label = label
            self.save()
            return existing
        # Seed last_seen_ts to now so we only alert on trades from here on,
        # not the wallet's entire backlog.
        w = TrackedWallet(wallet=wallet, label=label,
                          added_at=datetime.now(timezone.utc).isoformat(),
                          last_seen_ts=int(time.time()))
        self.wallets.append(w)
        self.save()
        return w

    def remove(self, wallet: str) -> bool:
        w = self.find(wallet)
        if not w:
            return False
        self.wallets.remove(w)
        self.save()
        return True


def refresh_tracked_sports(cfg: Config, tracked: TrackedList, force: bool = False,
                           max_refresh: int = 0) -> None:
    """Compute/refresh each tracked wallet's per-sport record so alerts can be
    tagged profitable/unprofitable for the bet's sport. Self-gating: only
    recomputes a wallet whose record is missing or older than refresh_days.
    max_refresh > 0 caps how many wallets are recomputed per call — the watcher
    passes 1 so a stale list refreshes gradually instead of blocking alert
    polling for minutes while all ten recompute at once."""
    cc = cfg.copytrade
    cache = ResolutionCache(cfg.resolution_cache_file)
    now = datetime.now(timezone.utc)
    changed = False
    refreshed = 0
    for w in tracked.wallets:
        if max_refresh and refreshed >= max_refresh:
            break
        if not force and w.sports_refreshed_at:
            try:
                age = (now - datetime.fromisoformat(w.sports_refreshed_at)).total_seconds()
                if age < cc.refresh_days * 86400:
                    continue
            except ValueError:
                pass
        try:
            trades = _recent_trades(w.wallet, cc.trades_sample)
            unresolved: set = set()
            rec = compute_track_record(trades, cache, cc.max_markets_checked, unresolved)
            offset = len(trades)
            while (rec["resolved_markets"] < cc.max_markets_checked
                   and offset < cc.max_trades_depth):
                more = _recent_trades(w.wallet, cc.trades_sample, offset)
                if not more:
                    break
                trades.extend(more)
                offset += len(more)
                rec = compute_track_record(trades, cache, cc.max_markets_checked, unresolved)
            w.by_sport = rec["by_sport"]
            w.sports_refreshed_at = now.isoformat()
            changed = True
            refreshed += 1
            log.info("computed per-sport record for %s (%d sports)",
                     w.label or w.wallet[:10], len(w.by_sport))
        except Exception as e:  # noqa: BLE001 — one wallet's failure shouldn't block the rest
            log.warning("per-sport refresh failed for %s: %s", w.label or w.wallet[:10], e)
    cache.save()
    if changed:
        tracked.save()


# Fill-coalescing knobs: re-alert an already-alerted position only when it
# doubles, or after a quiet spell; forget positions with no fills for 2 days.
REALERT_GROWTH = 2.0
REALERT_QUIET_S = 6 * 3600
PRUNE_S = 48 * 3600


def scan_tracked(cfg: Config, tracked: TrackedList) -> List[dict]:
    """Return alert dicts for tracked wallets' new activity, coalescing fills:
    one alert per position change (market+side+outcome), not per fill.
    A position-builder splitting a $30k bet into 60 fills gets one alert at
    $100+, then again only when the position doubles (or resumes after 6h
    quiet) — instead of 60 pings. Sub-minimum fills accumulate and alert once
    they collectively cross tracked_min_usd. Advances last_seen_ts."""
    # Self-gating, and at most one recompute per cycle so a stale list never
    # blocks alert polling for minutes while all ten wallets recompute at once.
    refresh_tracked_sports(cfg, tracked, max_refresh=1)
    alerts: List[dict] = []
    now = int(time.time())
    for w in tracked.wallets:
        try:
            # Page until last_seen_ts: one 100-trade page loses fills for
            # hyperactive wallets whenever the watcher was down a few hours.
            fresh = _trades_since(w.wallet, w.last_seen_ts, max_trades=2000)
        except Exception as e:  # noqa: BLE001 — one bad wallet shouldn't stop the rest
            log.warning("tracked scan failed for %s: %s", w.label or w.wallet[:10], e)
            continue
        if not fresh:
            continue
        newest = max(int(t.get("timestamp") or 0) for t in fresh)

        # Coalesce this cycle's fills by position (market + side + outcome).
        groups: dict = {}
        for t in fresh:
            cid = t.get("conditionId") or t.get("eventSlug") or "?"
            key = f"{cid}|{t.get('side')}|{t.get('outcome')}"
            g = groups.setdefault(key, {
                "side": t.get("side"), "title": t.get("title"),
                "outcome": t.get("outcome"), "event_slug": t.get("eventSlug"),
                "cid": t.get("conditionId"), "asset": t.get("asset"),
                "outcome_index": t.get("outcomeIndex"),
                "usd": 0.0, "shares": 0.0, "fills": 0, "ts": 0,
            })
            sh = float(t.get("size") or 0)
            g["usd"] += sh * float(t.get("price") or 0)
            g["shares"] += sh
            g["fills"] += 1
            g["ts"] = max(g["ts"], int(t.get("timestamp") or 0))

        for key, g in groups.items():
            st = w.open_alerts.get(key) or {"alerted_usd": 0.0, "pending_usd": 0.0,
                                            "ts": 0, "fill_ts": 0}
            st["pending_usd"] += g["usd"]
            st["fill_ts"] = now
            total = st["alerted_usd"] + st["pending_usd"]
            quiet = now - int(st.get("ts") or 0) > REALERT_QUIET_S
            fire = (st["pending_usd"] >= cfg.tracked_min_usd
                    and (st["alerted_usd"] <= 0
                         or total >= REALERT_GROWTH * st["alerted_usd"]
                         or quiet))
            if fire:
                sport = sport_from_slug(g["event_slug"] or "")
                alerts.append({
                    "label": w.label or w.wallet[:10],
                    "wallet": w.wallet,
                    "side": g["side"],
                    "title": g["title"],
                    "outcome": g["outcome"],
                    "cid": g["cid"],
                    "outcome_index": g["outcome_index"],
                    "price": round(g["usd"] / g["shares"], 3) if g["shares"] else 0.0,
                    "now_price": _current_mid(g["asset"]),     # drift vs entry
                    "usd": round(st["pending_usd"], 2),        # new since last alert
                    "position_usd": round(total, 2),           # cumulative position
                    "fills": g["fills"],
                    "event_slug": g["event_slug"],
                    "ts": g["ts"],
                    "sport": sport,
                    "sport_record": w.by_sport.get(sport),
                    "sport_asof": (w.sports_refreshed_at or "")[:10],
                })
                st["alerted_usd"] = total
                st["pending_usd"] = 0.0
                st["ts"] = now
            w.open_alerts[key] = st

        # Forget positions with no fills in 2 days (games resolve well within).
        w.open_alerts = {k: v for k, v in w.open_alerts.items()
                         if now - int(v.get("fill_ts") or 0) < PRUNE_S}
        w.last_seen_ts = max(w.last_seen_ts, newest)
    tracked.save()
    log.info("tracked scan: %d position alerts across %d wallets",
             len(alerts), len(tracked.wallets))
    return alerts
