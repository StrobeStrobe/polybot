"""Risk manager: deterministic, code-enforced limits applied to every
decision the LLM produces. A decision that violates a limit is clamped if
possible and vetoed otherwise. The kill switch halts all opening trades for
the rest of the day when the daily loss limit is hit."""

import logging
from typing import Dict, List, Optional, Tuple

from .config import Config
from .market_data import Book
from .portfolio import Portfolio

log = logging.getLogger("polybot.risk")


def check_kill_switch(cfg: Config, portfolio: Portfolio) -> bool:
    """True = trading halted for the day."""
    dd = portfolio.todays_drawdown_pct()
    if dd >= cfg.risk.daily_loss_limit_pct:
        log.warning("KILL SWITCH: daily drawdown %.1f%% >= limit %.1f%% — no new positions today",
                    dd * 100, cfg.risk.daily_loss_limit_pct * 100)
        return True
    return False


def vet_decisions(cfg: Config, portfolio: Portfolio, decisions: List[dict],
                  books: Dict[str, Book]) -> Tuple[List[dict], List[str]]:
    """Returns (approved_decisions, veto_log). Approved opens may have
    size_usd / limit_price clamped."""
    approved: List[dict] = []
    vetoes: List[str] = []
    halted = check_kill_switch(cfg, portfolio)

    bankroll = portfolio.equity
    max_pos = min(cfg.risk.max_position_usd, bankroll * cfg.risk.max_position_pct)
    max_exposure = bankroll * cfg.risk.max_total_exposure_pct
    planned_spend = 0.0
    planned_new_positions = 0
    planned_by_market: Dict[str, float] = {}

    for d in decisions:
        label = f"{d.get('action')} {d.get('outcome')} @ {d.get('limit_price')} on '{d.get('question', '')[:60]}'"
        token_id = d.get("token_id", "")
        book: Optional[Book] = books.get(token_id)

        if d.get("action") == "close":
            if token_id not in portfolio.positions:
                vetoes.append(f"VETO (no such position): {label}")
                continue
            approved.append(d)
            continue

        # ----- opens -----
        if halted:
            vetoes.append(f"VETO (kill switch active): {label}")
            continue

        price = float(d.get("limit_price") or 0)
        if not (cfg.risk.min_price <= price <= cfg.risk.max_price):
            vetoes.append(f"VETO (price {price} outside [{cfg.risk.min_price}, {cfg.risk.max_price}]): {label}")
            continue

        if book is None or book.best_ask is None:
            vetoes.append(f"VETO (no orderbook data): {label}")
            continue

        if book.spread is not None and book.spread > cfg.risk.max_spread and d.get("source") != "arbitrage":
            vetoes.append(f"VETO (spread {book.spread:.3f} > {cfg.risk.max_spread}): {label}")
            continue

        # Don't let the model cross deep through the book.
        if price > book.best_ask + 0.02:
            d["limit_price"] = round(book.best_ask + 0.01, 3)

        size = float(d.get("size_usd") or 0)
        if size <= 0:
            vetoes.append(f"VETO (non-positive size): {label}")
            continue
        if size > max_pos:
            d["size_usd"] = round(max_pos, 2)
            size = d["size_usd"]

        # Existing exposure in the same market counts toward the cap,
        # including spend planned earlier in this same batch.
        market_id = d.get("market_id", "")
        existing = sum(p.cost_basis for p in portfolio.positions.values()
                       if p.market_id == market_id)
        existing += planned_by_market.get(market_id, 0.0)
        if existing + size > max_pos:
            size = max_pos - existing
            if size < 1.0:
                vetoes.append(f"VETO (market already at position cap): {label}")
                continue
            d["size_usd"] = round(size, 2)

        if portfolio.exposure + planned_spend + size > max_exposure:
            vetoes.append(f"VETO (would exceed total exposure cap ${max_exposure:.0f}): {label}")
            continue

        open_count = len(portfolio.positions) + planned_new_positions
        if token_id not in portfolio.positions and open_count >= cfg.risk.max_open_positions:
            vetoes.append(f"VETO (max open positions {cfg.risk.max_open_positions}): {label}")
            continue

        if size > portfolio.cash - planned_spend:
            vetoes.append(f"VETO (insufficient cash): {label}")
            continue

        planned_spend += size
        planned_by_market[market_id] = planned_by_market.get(market_id, 0.0) + size
        if token_id not in portfolio.positions:
            planned_new_positions += 1
        approved.append(d)

    for v in vetoes:
        log.info(v)
    return approved, vetoes
