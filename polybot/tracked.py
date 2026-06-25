"""Manually-tracked wallets: a raw activity mirror, separate from the
auto-vetted copy scout. You name the wallets; every new trade above a minimum
size (any market, buy or sell) fires a Discord alert. No win-rate vetting —
these are wallets you've explicitly chosen to follow.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import Config
from .copytrade import _recent_trades

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


def scan_tracked(cfg: Config, tracked: TrackedList) -> List[dict]:
    """Return alert dicts for every new trade (any market, buy or sell) above
    the minimum size across all tracked wallets. Advances last_seen_ts."""
    alerts: List[dict] = []
    for w in tracked.wallets:
        try:
            trades = _recent_trades(w.wallet, 100)
        except Exception as e:  # noqa: BLE001 — one bad wallet shouldn't stop the rest
            log.warning("tracked scan failed for %s: %s", w.label or w.wallet[:10], e)
            continue
        if not trades:
            continue
        newest = max(int(t.get("timestamp") or 0) for t in trades)
        fresh = [t for t in trades if int(t.get("timestamp") or 0) > w.last_seen_ts]
        for t in sorted(fresh, key=lambda x: int(x.get("timestamp") or 0)):
            usd = float(t.get("size") or 0) * float(t.get("price") or 0)
            if usd < cfg.tracked_min_usd:
                continue
            alerts.append({
                "label": w.label or w.wallet[:10],
                "wallet": w.wallet,
                "side": t.get("side"),
                "title": t.get("title"),
                "outcome": t.get("outcome"),
                "price": float(t.get("price") or 0),
                "usd": round(usd, 2),
                "event_slug": t.get("eventSlug"),
                "ts": int(t.get("timestamp") or 0),
            })
        w.last_seen_ts = max(w.last_seen_ts, newest)
    tracked.save()
    log.info("tracked scan: %d new trades across %d wallets",
             len(alerts), len(tracked.wallets))
    return alerts
