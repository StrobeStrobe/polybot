"""Head-trader decision agent. Receives all scout reports plus portfolio
state and risk limits, and returns structured trade decisions. Its output is
then clamped/vetoed by the risk manager — the model proposes, code disposes."""

import json
import logging
from datetime import datetime, timezone
from typing import List

import anthropic

from .config import Config
from .portfolio import Portfolio

log = logging.getLogger("polybot.decision")

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["open", "close"]},
                    "market_id": {"type": "string"},
                    "question": {"type": "string"},
                    "token_id": {"type": "string"},
                    "outcome": {"type": "string"},
                    "limit_price": {"type": "number", "description": "max price willing to pay (open) or min to accept (close)"},
                    "size_usd": {"type": "number", "description": "dollar amount for opens; ignored for closes (full close)"},
                    "rationale": {"type": "string"},
                    "source": {"type": "string", "description": "which scout report this comes from: arbitrage|value|news|portfolio"},
                },
                "required": ["action", "market_id", "question", "token_id", "outcome",
                             "limit_price", "size_usd", "rationale", "source"],
                "additionalProperties": False,
            },
        },
        "commentary": {"type": "string", "description": "brief cycle summary: what you saw, what you passed on and why"},
    },
    "required": ["decisions", "commentary"],
    "additionalProperties": False,
}

DECISION_SYSTEM = """You are the head trader of a small, risk-disciplined Polymarket trading operation.
Scout agents send you opportunity reports each cycle; you decide what (if anything) to trade.

Decision principles:
- Capital preservation first. The fastest way to fail is overtrading. Passing on every report is a
  completely acceptable decision and should be common.
- Trust arbitrage reports most (mechanical edges), value reports next, news reports least —
  news-based edges decay fast and may already be priced in by the time we act.
- Copytrade reports are "smart money" signals: sports bettors with verified records just placed
  these bets. Each trader carries a win_rate measured over resolved markets and a winrate_edge
  (win rate minus their average entry price, i.e. how much they beat break-even). Weight
  winrate_edge and sample size most — a 60% win rate at 0.50 average entry is far stronger
  than 75% at 0.72. Copy only when the record is strong, their bet was large relative to their
  average, and the current ask is close to their entry. Multiple watched traders on the same side is the
  strongest version of this signal. Copytrade exit_signals (a watched trader selling a market
  we hold) should usually trigger a close of our copied position.
- Demand edge: don't open a position unless the scout's fair value exceeds the entry price by at
  least the configured min_edge, after considering the scout's confidence. Discount low-confidence
  fair values heavily.
- Size conservatively: a fraction of bankroll proportional to edge and confidence, never more than
  the configured per-position cap. When in doubt, size smaller.
- Diversify: avoid concentrating in correlated markets (same event, same underlying driver).
- Manage existing positions: recommend closing when the thesis has played out (price moved to fair
  value), the thesis is broken, or better opportunities need the capital.
- Set limit_price with care: for opens, at or slightly above the current best ask is fine for small
  size; never cross more than 1-2 cents through the book.
- You are spending real money. Every decision should be one you could defend to a risk committee."""


def decide(cfg: Config, portfolio: Portfolio, scout_reports: List[dict],
           arb_opportunities: List[dict]) -> dict:
    client = anthropic.Anthropic()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    payload = {
        "current_time": now,
        "risk_limits": {
            "max_position_usd": cfg.risk.max_position_usd,
            "max_position_pct_of_bankroll": cfg.risk.max_position_pct,
            "max_total_exposure_pct": cfg.risk.max_total_exposure_pct,
            "max_open_positions": cfg.risk.max_open_positions,
            "min_edge": cfg.risk.min_edge,
        },
        "portfolio": portfolio.status(),
        "arbitrage_opportunities": arb_opportunities,
        "scout_reports": scout_reports,
    }

    response = client.messages.create(
        model=cfg.decision_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
        },
        system=DECISION_SYSTEM,
        messages=[{
            "role": "user",
            "content": "Cycle input:\n" + json.dumps(payload, indent=1)
                       + "\n\nReview the reports and current portfolio, then output your decisions.",
        }],
    )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        log.warning("decision agent returned no text (stop_reason=%s)", response.stop_reason)
        return {"decisions": [], "commentary": "decision agent produced no output"}
    try:
        result = json.loads(text)
    except ValueError:
        log.warning("decision agent returned unparseable JSON")
        return {"decisions": [], "commentary": "unparseable decision output"}

    log.info("decision agent: %d decisions", len(result.get("decisions", [])))
    return result
