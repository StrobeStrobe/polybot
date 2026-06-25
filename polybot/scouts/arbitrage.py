"""Pure-code arbitrage scanner. No LLM involved — these are mechanical
pricing inconsistencies:

1. Intra-market: best ask(YES) + best ask(NO) < $1. Buying both sides
   guarantees a $1 payout at resolution for less than $1.
2. Neg-risk events (mutually exclusive multi-outcome): sum of best YES asks
   across all outcomes < $1. Exactly one outcome pays $1.
3. Neg-risk "buy all NO": sum of best NO asks < (n - 1). All but one NO
   pays $1.

Real arbs are rare and get taken fast by professional bots; the buffer
(`arb_min_profit`) filters out noise that would be eaten by fees/slippage.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..market_data import Market

log = logging.getLogger("polybot.arb")


@dataclass
class ArbLeg:
    market_id: str
    question: str
    token_id: str
    outcome: str
    price: float            # ask we'd pay
    available_size: float   # shares at that ask


@dataclass
class ArbOpportunity:
    kind: str               # "intra_market" | "event_yes_sweep" | "event_no_sweep"
    description: str
    legs: List[ArbLeg] = field(default_factory=list)
    cost_per_set: float = 0.0     # cost to buy one "set"
    payout_per_set: float = 1.0   # guaranteed payout per set
    max_sets: float = 0.0         # limited by thinnest leg

    @property
    def profit_per_set(self) -> float:
        return self.payout_per_set - self.cost_per_set

    def to_dict(self) -> dict:
        return {
            "type": "arbitrage",
            "kind": self.kind,
            "description": self.description,
            "cost_per_set": round(self.cost_per_set, 4),
            "payout_per_set": round(self.payout_per_set, 4),
            "profit_per_set": round(self.profit_per_set, 4),
            "max_sets": round(self.max_sets, 2),
            "legs": [
                {
                    "market_id": leg.market_id,
                    "question": leg.question,
                    "token_id": leg.token_id,
                    "outcome": leg.outcome,
                    "buy_at": leg.price,
                    "available_size": leg.available_size,
                }
                for leg in self.legs
            ],
        }


def scan_arbitrage(markets: List[Market], min_profit: float = 0.01) -> List[ArbOpportunity]:
    opps: List[ArbOpportunity] = []

    # --- 1. Intra-market YES+NO < 1 ---
    for m in markets:
        if len(m.outcomes) != 2 or not m.accepting_orders:
            continue
        b0, b1 = m.book_for_outcome(0), m.book_for_outcome(1)
        if not b0 or not b1 or b0.best_ask is None or b1.best_ask is None:
            continue
        cost = b0.best_ask + b1.best_ask
        if cost < 1.0 - min_profit:
            opps.append(
                ArbOpportunity(
                    kind="intra_market",
                    description=f"YES+NO under $1 in '{m.question}'",
                    legs=[
                        ArbLeg(m.id, m.question, m.token_ids[0], m.outcomes[0], b0.best_ask, b0.ask_size),
                        ArbLeg(m.id, m.question, m.token_ids[1], m.outcomes[1], b1.best_ask, b1.ask_size),
                    ],
                    cost_per_set=cost,
                    payout_per_set=1.0,
                    max_sets=min(b0.ask_size, b1.ask_size),
                )
            )

    # --- 2/3. Neg-risk event sweeps ---
    by_event: Dict[str, List[Market]] = defaultdict(list)
    for m in markets:
        if m.neg_risk and m.event_id and m.accepting_orders and len(m.outcomes) == 2:
            by_event[m.event_id].append(m)

    for event_id, group in by_event.items():
        if len(group) < 3:
            continue  # need the (near-)complete outcome set; small groups are partial
        yes_legs: List[ArbLeg] = []
        no_legs: List[ArbLeg] = []
        complete = True
        for m in group:
            try:
                yes_idx = next(i for i, o in enumerate(m.outcomes) if o.lower() == "yes")
                no_idx = 1 - yes_idx
            except StopIteration:
                complete = False
                break
            yb, nb = m.book_for_outcome(yes_idx), m.book_for_outcome(no_idx)
            if not yb or yb.best_ask is None or not nb or nb.best_ask is None:
                complete = False
                break
            label = m.question
            yes_legs.append(ArbLeg(m.id, label, m.token_ids[yes_idx], "Yes", yb.best_ask, yb.ask_size))
            no_legs.append(ArbLeg(m.id, label, m.token_ids[no_idx], "No", nb.best_ask, nb.ask_size))
        if not complete:
            continue

        title = group[0].event_title or f"event {event_id}"
        n = len(group)

        # NOTE: a sweep is only a true arb if the listed outcomes are exhaustive.
        # Partial books (e.g. only top candidates listed) make "buy all YES"
        # look cheap when it isn't. We require the YES prices to roughly sum
        # near 1 as an exhaustiveness sanity check.
        yes_price_sum = sum(m.prices[0] if m.prices else 0 for m in group)
        exhaustive = 0.97 <= yes_price_sum <= 1.10

        yes_cost = sum(l.price for l in yes_legs)
        if exhaustive and yes_cost < 1.0 - min_profit:
            opps.append(
                ArbOpportunity(
                    kind="event_yes_sweep",
                    description=f"Buy YES on all {n} outcomes of '{title}' for ${yes_cost:.3f}",
                    legs=yes_legs,
                    cost_per_set=yes_cost,
                    payout_per_set=1.0,
                    max_sets=min(l.available_size for l in yes_legs),
                )
            )

        no_cost = sum(l.price for l in no_legs)
        if exhaustive and no_cost < (n - 1) - min_profit * n:
            opps.append(
                ArbOpportunity(
                    kind="event_no_sweep",
                    description=f"Buy NO on all {n} outcomes of '{title}' for ${no_cost:.3f} (pays ${n - 1})",
                    legs=no_legs,
                    cost_per_set=no_cost,
                    payout_per_set=float(n - 1),
                    max_sets=min(l.available_size for l in no_legs),
                )
            )

    opps.sort(key=lambda o: o.profit_per_set * o.max_sets, reverse=True)
    log.info("arbitrage scan: %d opportunities", len(opps))
    return opps
