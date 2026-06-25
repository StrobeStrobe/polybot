"""Portfolio state: cash, open positions, trade log, daily P&L tracking.
Persisted as JSON so the bot survives restarts. Used in both paper and live
mode (in live mode it mirrors what the bot believes it holds)."""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("polybot.portfolio")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Position:
    market_id: str
    question: str
    token_id: str
    outcome: str
    shares: float
    avg_price: float
    opened_at: str
    thesis: str = ""
    source: str = ""           # which scout/strategy produced it
    last_mark: Optional[float] = None

    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_price

    @property
    def market_value(self) -> float:
        mark = self.last_mark if self.last_mark is not None else self.avg_price
        return self.shares * mark

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis


@dataclass
class Trade:
    ts: str
    action: str               # "buy" | "sell" | "settle"
    market_id: str
    question: str
    token_id: str
    outcome: str
    shares: float
    price: float
    usd: float
    mode: str                 # "paper" | "live"
    rationale: str = ""
    order_id: str = ""


class Portfolio:
    def __init__(self, state_file: str, starting_cash: float):
        self.path = Path(state_file)
        self.cash: float = starting_cash
        self.starting_cash: float = starting_cash
        self.positions: Dict[str, Position] = {}   # token_id -> Position
        self.trades: List[Trade] = []
        self.realized_pnl: float = 0.0
        self.daily: Dict[str, float] = {}          # date -> realized pnl that day
        self.day_start_equity: Dict[str, float] = {}
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        self.cash = data["cash"]
        self.starting_cash = data.get("starting_cash", self.cash)
        self.realized_pnl = data.get("realized_pnl", 0.0)
        self.daily = data.get("daily", {})
        self.day_start_equity = data.get("day_start_equity", {})
        self.positions = {t: Position(**p) for t, p in data.get("positions", {}).items()}
        self.trades = [Trade(**t) for t in data.get("trades", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "realized_pnl": self.realized_pnl,
            "daily": self.daily,
            "day_start_equity": self.day_start_equity,
            "positions": {t: asdict(p) for t, p in self.positions.items()},
            "trades": [asdict(t) for t in self.trades[-500:]],  # cap log size
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.path)

    # ---------- accounting ----------

    @property
    def exposure(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    def record_day_start(self) -> None:
        today = _today()
        if today not in self.day_start_equity:
            self.day_start_equity[today] = self.equity

    def todays_drawdown_pct(self) -> float:
        today = _today()
        start = self.day_start_equity.get(today, self.equity)
        if start <= 0:
            return 0.0
        return max(0.0, (start - self.equity) / start)

    def apply_buy(self, market_id: str, question: str, token_id: str, outcome: str,
                  shares: float, price: float, mode: str, rationale: str = "",
                  source: str = "", order_id: str = "") -> None:
        usd = shares * price
        self.cash -= usd
        pos = self.positions.get(token_id)
        if pos:
            total = pos.shares + shares
            pos.avg_price = (pos.cost_basis + usd) / total
            pos.shares = total
        else:
            self.positions[token_id] = Position(
                market_id=market_id, question=question, token_id=token_id,
                outcome=outcome, shares=shares, avg_price=price,
                opened_at=_now(), thesis=rationale, source=source,
            )
        self.trades.append(Trade(_now(), "buy", market_id, question, token_id,
                                 outcome, shares, price, usd, mode, rationale, order_id))

    def apply_sell(self, token_id: str, shares: float, price: float, mode: str,
                   rationale: str = "", order_id: str = "", action: str = "sell") -> None:
        pos = self.positions.get(token_id)
        if not pos:
            log.warning("sell for unknown position %s", token_id)
            return
        shares = min(shares, pos.shares)
        usd = shares * price
        pnl = (price - pos.avg_price) * shares
        self.cash += usd
        self.realized_pnl += pnl
        self.daily[_today()] = self.daily.get(_today(), 0.0) + pnl
        pos.shares -= shares
        self.trades.append(Trade(_now(), action, pos.market_id, pos.question, token_id,
                                 pos.outcome, shares, price, usd, mode, rationale, order_id))
        if pos.shares <= 1e-9:
            del self.positions[token_id]

    def mark(self, token_id: str, price: float) -> None:
        if token_id in self.positions:
            self.positions[token_id].last_mark = price

    def status(self) -> dict:
        return {
            "mode_equity_usd": round(self.equity, 2),
            "cash_usd": round(self.cash, 2),
            "exposure_usd": round(self.exposure, 2),
            "realized_pnl_usd": round(self.realized_pnl, 2),
            "unrealized_pnl_usd": round(sum(p.unrealized_pnl for p in self.positions.values()), 2),
            "total_return_pct": round(100 * (self.equity - self.starting_cash) / self.starting_cash, 2)
            if self.starting_cash else 0.0,
            "open_positions": [
                {
                    "question": p.question,
                    "outcome": p.outcome,
                    "shares": round(p.shares, 2),
                    "avg_price": round(p.avg_price, 4),
                    "last_mark": p.last_mark,
                    "cost_usd": round(p.cost_basis, 2),
                    "unrealized_pnl_usd": round(p.unrealized_pnl, 2),
                    "source": p.source,
                }
                for p in self.positions.values()
            ],
        }
