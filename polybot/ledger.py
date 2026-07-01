"""Copy-performance ledger: record every tracked-wallet alert, settle BUYs
against market resolutions, and detect consensus (2+ tracked wallets on the
same market & outcome within 48h — the strongest copy signal we have).

Answers the question the alert stream alone can't: "if I'd blindly copied
every alert with $100, would I be up or down?" — the validation needed
before real money goes in.
"""

import json
import logging
import time
from pathlib import Path

from .config import Config
from .copytrade import ResolutionCache

log = logging.getLogger("polybot.ledger")

STAKE = 100.0          # hypothetical $ copied per BUY alert
CONSENSUS_WINDOW_S = 48 * 3600


def _path(cfg: Config) -> Path:
    return Path(cfg.alerts_log_file).parent / "ledger.json"


def _load(cfg: Config) -> dict:
    p = _path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            log.warning("ledger unreadable — starting fresh")
    return {"entries": []}


def _save(cfg: Config, data: dict) -> None:
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))


def record_alerts(cfg: Config, alerts: list) -> list:
    """Append this cycle's alerts to the ledger and return consensus hits:
    groups where a NEW tracked wallet just joined 1+ others on the same
    market+outcome BUY within the window."""
    if not alerts:
        return []
    data = _load(cfg)
    for a in alerts:
        data["entries"].append({
            "ts": a.get("ts") or int(time.time()),
            "wallet": a.get("wallet"), "label": a.get("label"),
            "cid": a.get("cid"), "title": a.get("title"),
            "side": a.get("side"), "outcome": a.get("outcome"),
            "outcome_index": a.get("outcome_index"),
            "price": a.get("price"), "usd": a.get("usd"),
            "sport": a.get("sport"), "event_slug": a.get("event_slug"),
            "settled": None,          # None = open; else {"win", "copy_pnl"}
            "consensus_fired": False,
        })

    hits = []
    cut = int(time.time()) - CONSENSUS_WINDOW_S
    groups: dict = {}
    for e in data["entries"]:
        if (e.get("ts") or 0) < cut or e.get("side") != "BUY" or not e.get("cid"):
            continue
        groups.setdefault((e["cid"], e.get("outcome")), []).append(e)
    for (cid, outcome), es in groups.items():
        wallets = {e["wallet"] for e in es}
        if len(wallets) < 2:
            continue
        # fire only when a wallet with no already-flagged entry joins the club
        old = {e["wallet"] for e in es if e.get("consensus_fired")}
        if not (wallets - old):
            continue
        hits.append({
            "cid": cid, "outcome": outcome,
            "title": es[-1].get("title"),
            "event_slug": es[-1].get("event_slug"),
            "labels": sorted({e["label"] for e in es}),
            "total_usd": round(sum(e.get("usd") or 0 for e in es), 2),
            "avg_price": round(sum((e.get("price") or 0) * (e.get("usd") or 0)
                                   for e in es) / max(sum(e.get("usd") or 0
                                                          for e in es), 1), 3),
        })
        for e in es:
            e["consensus_fired"] = True
    _save(cfg, data)
    return hits


def settle(cfg: Config) -> dict:
    """Resolve open BUY entries against final prices and return a summary of
    the last 7 days for the weekly report."""
    data = _load(cfg)
    cache = ResolutionCache(cfg.resolution_cache_file)
    changed = False
    for e in data["entries"]:
        if e.get("settled") is not None or e.get("side") != "BUY":
            continue
        cid, idx = e.get("cid"), e.get("outcome_index")
        if not cid or idx is None:
            e["settled"] = {"win": None, "copy_pnl": 0.0}  # can't score
            changed = True
            continue
        finals = cache.get(cid)
        if finals is None:
            continue  # still open
        win = idx < len(finals) and finals[idx] >= 0.99
        p = float(e.get("price") or 0)
        pnl = round(STAKE * (1 - p) / p, 2) if (win and p > 0) else -STAKE
        e["settled"] = {"win": bool(win), "copy_pnl": pnl}
        changed = True
    cache.save()
    if changed:
        _save(cfg, data)

    cut = int(time.time()) - 7 * 86400
    week = [e for e in data["entries"]
            if (e.get("ts") or 0) >= cut and e.get("side") == "BUY"]
    scored = [e for e in week
              if e.get("settled") and e["settled"].get("win") is not None]
    return {
        "stake": STAKE,
        "settled": len(scored),
        "wins": sum(1 for e in scored if e["settled"]["win"]),
        "copy_pnl": round(sum(e["settled"]["copy_pnl"] for e in scored), 2),
        "open": sum(1 for e in week if e.get("settled") is None),
    }
