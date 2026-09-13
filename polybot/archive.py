"""Append-only trade archive for tracked wallets.

Polymarket's trades API serves at most ~10,500 fills per wallet — a hard
ceiling, not a paging limit. For a heavy trader that's only weeks of history
(Talvez10 runs 143 fills/day, so 10,500 fills reaches back 73 days). Anything
older is simply gone from the API.

So we keep our own copy. Every poll already pulls each tracked wallet's new
fills; we append them here, deduped. The archive only grows, so a wallet's
usable record extends past what the API will serve for as long as we keep
watching. Records are computed from archive ∪ live-API, so nothing is lost
if the archive is empty or the API is briefly unreachable.

One JSONL file per wallet under STATE_DIR/trades/, which lives on the Railway
volume and therefore survives deploys.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, List

from .config import Config

log = logging.getLogger("polybot.archive")

# Only the fields the record math actually uses — a full trade row is ~3x
# larger and the rest (avatars, pseudonyms) is reconstructible.
FIELDS = ("timestamp", "conditionId", "asset", "outcomeIndex", "side",
          "size", "price", "eventSlug", "title", "outcome", "transactionHash")


def _dir(cfg: Config) -> Path:
    d = Path(cfg.alerts_log_file).parent / "trades"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(cfg: Config, wallet: str) -> Path:
    return _dir(cfg) / f"{wallet.lower()}.jsonl"


def _key(t: dict) -> tuple:
    """Identity of a single fill. No unique id is exposed, so use the tx plus
    the leg details — one transaction can settle several fills."""
    return (t.get("transactionHash"), t.get("asset"), t.get("side"),
            t.get("size"), t.get("price"), t.get("timestamp"))


def load(cfg: Config, wallet: str) -> List[dict]:
    p = _path(cfg, wallet)
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue          # tolerate a torn final line
    return out


def append(cfg: Config, wallet: str, trades: Iterable[dict]) -> int:
    """Add any fills we haven't archived. Returns how many were new."""
    trades = list(trades)
    if not trades:
        return 0
    have = {_key(t) for t in load(cfg, wallet)}
    fresh = [t for t in trades if _key(t) not in have]
    if not fresh:
        return 0
    with _path(cfg, wallet).open("a") as f:
        for t in fresh:
            f.write(json.dumps({k: t.get(k) for k in FIELDS},
                               separators=(",", ":")) + "\n")
    return len(fresh)


def merged(cfg: Config, wallet: str, live: Iterable[dict]) -> List[dict]:
    """Archive ∪ live API pull, deduped, newest first — the fullest history
    available for this wallet."""
    by: Dict[tuple, dict] = {}
    for t in list(live) + load(cfg, wallet):
        by.setdefault(_key(t), t)
    return sorted(by.values(), key=lambda t: int(t.get("timestamp") or 0),
                  reverse=True)


def stats(cfg: Config, wallet: str) -> dict:
    rows = load(cfg, wallet)
    if not rows:
        return {"fills": 0, "from": "", "to": ""}
    ts = [int(r.get("timestamp") or 0) for r in rows if r.get("timestamp")]
    from datetime import datetime, timezone
    return {"fills": len(rows),
            "from": datetime.fromtimestamp(min(ts), timezone.utc).strftime("%Y-%m-%d"),
            "to": datetime.fromtimestamp(max(ts), timezone.utc).strftime("%Y-%m-%d"),
            "bytes": _path(cfg, wallet).stat().st_size}
