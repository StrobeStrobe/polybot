"""Claude-powered scout agents.

Two scouts share the same machinery and differ in candidate selection and
mandate:

- Value scout: looks for markets where the crowd price diverges from a
  defensible fair-value estimate (base rates, stale prices, longshot bias).
- News scout: looks for markets where recent developments haven't been
  fully priced in yet.

Each scout gets a slice of candidate markets with live orderbook data, may
use web search to research, and must return a structured report. Reports go
to the head-trader decision agent — scouts never trade.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

import anthropic

from ..config import Config
from ..market_data import Market

log = logging.getLogger("polybot.scout")

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "reports": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "market_id": {"type": "string"},
                    "question": {"type": "string"},
                    "token_id": {"type": "string", "description": "token id of the outcome to BUY"},
                    "outcome": {"type": "string", "description": "outcome name to BUY, e.g. Yes or No"},
                    "current_price": {"type": "number"},
                    "fair_value": {"type": "number", "description": "your probability estimate for this outcome"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "thesis": {"type": "string"},
                    "key_evidence": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "string"},
                },
                "required": [
                    "market_id", "question", "token_id", "outcome",
                    "current_price", "fair_value", "confidence", "thesis",
                    "key_evidence", "risks",
                ],
                "additionalProperties": False,
            },
        },
        "scan_notes": {"type": "string", "description": "brief notes on markets reviewed but passed on"},
    },
    "required": ["reports", "scan_notes"],
    "additionalProperties": False,
}

SCOUT_SYSTEM = """You are a research scout for a small prediction-market trading desk on Polymarket.
Your job is to find genuinely mispriced outcomes, not to find trades for the sake of trading.

Discipline rules:
- Prices on liquid Polymarket markets are usually roughly efficient. Your default conclusion for any market should be "fairly priced — pass". Only report an opportunity when you have concrete evidence the crowd is wrong.
- fair_value is YOUR probability estimate for the outcome you recommend buying. Be calibrated: if you wouldn't bet your own money at that probability, lower it.
- Use web search to verify facts and find recent developments before forming a view. Do not rely on memory for anything time-sensitive.
- An empty reports list is a perfectly good output. Most cycles should produce 0-3 reports, not 10.
- Never recommend buying an outcome priced above 0.97 or below 0.03.
- Account for time value: capital locked until resolution has opportunity cost. A 2% edge on a market resolving in 6 months is worse than no trade.
- In key_evidence, cite specific facts (with dates) you verified, not vibes."""

VALUE_MANDATE = """MANDATE: Value scout. Examine the candidate markets below for pricing that diverges
from a defensible probability estimate. Typical sources of edge: longshot bias (small probabilities
overpriced), stale prices after slow news, base rates the crowd is ignoring, and misread resolution
criteria (read the resolution rules carefully — markets often resolve on technicalities)."""

NEWS_MANDATE = """MANDATE: News scout. For the candidate markets below, search for the LATEST news
(today and the last few days). You are looking for markets where something material happened recently
that the price has not fully adjusted to. If nothing material and recent exists for a market, pass on it."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _candidate_payload(markets: List[Market]) -> str:
    return json.dumps([m.summary() for m in markets], indent=1)


def _run_scout(cfg: Config, markets: List[Market], mandate: str, label: str) -> dict:
    if not markets:
        return {"reports": [], "scan_notes": f"{label}: no candidate markets this cycle"}

    client = _client()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    user_msg = (
        f"Current time: {now}\n\n{mandate}\n\n"
        f"CANDIDATE MARKETS (live prices and best bid/ask from the orderbook):\n"
        f"{_candidate_payload(markets)}"
    )

    messages = [{"role": "user", "content": user_msg}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 12}]

    for _ in range(6):  # pause_turn continuation guard
        response = client.messages.create(
            model=cfg.scout_model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=SCOUT_SYSTEM,
            tools=tools,
            output_config={"format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
            messages=messages,
        )
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": response.content},
            ]
            continue
        break

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        log.warning("%s scout returned no text (stop_reason=%s)", label, response.stop_reason)
        return {"reports": [], "scan_notes": f"{label}: model returned no parseable output"}
    try:
        result = json.loads(text)
    except ValueError:
        log.warning("%s scout returned unparseable JSON", label)
        return {"reports": [], "scan_notes": f"{label}: unparseable output"}

    # Compute edge and stamp the scout name on each report.
    for r in result.get("reports", []):
        r["scout"] = label
        r["edge"] = round(float(r["fair_value"]) - float(r["current_price"]), 4)
    log.info("%s scout: %d reports", label, len(result.get("reports", [])))
    return result


def _filter_candidates(markets: List[Market], cfg: Config, max_days: Optional[int] = None) -> List[Market]:
    out = []
    for m in markets:
        if not m.accepting_orders or not m.books:
            continue
        if m.volume_24h < cfg.scout.min_volume_24h or m.liquidity < cfg.risk.min_liquidity_usd:
            continue
        days = m.days_to_resolution
        limit = max_days or cfg.scout.max_days_to_resolution
        if days is None or days < 0 or days > limit:
            continue
        # Need at least one outcome in the tradeable mid-range.
        if not any(cfg.risk.min_price <= p <= cfg.risk.max_price for p in m.prices):
            continue
        out.append(m)
    return out


def run_value_scout(cfg: Config, markets: List[Market]) -> dict:
    cands = _filter_candidates(markets, cfg)
    cands.sort(key=lambda m: m.volume_24h, reverse=True)
    return _run_scout(cfg, cands[: cfg.scout.markets_per_scout], VALUE_MANDATE, "value")


def run_news_scout(cfg: Config, markets: List[Market]) -> dict:
    # News scout focuses on near-term, high-activity markets.
    cands = _filter_candidates(markets, cfg, max_days=21)
    cands.sort(key=lambda m: m.volume_24h, reverse=True)
    # Skip the exact slice the value scout took if there's enough depth,
    # otherwise overlap is fine — two independent looks at hot markets.
    n = cfg.scout.markets_per_scout
    slice_ = cands[n : 2 * n] if len(cands) >= 2 * n else cands[:n]
    return _run_scout(cfg, slice_, NEWS_MANDATE, "news")
