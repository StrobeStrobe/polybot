"""Execution layer.

Paper mode (default): simulates fills against the live orderbook — a buy
fills at the best ask (up to the limit price) for the available size; no
fill if the limit doesn't reach the ask. This is intentionally conservative.

Live mode: places real limit orders through Polymarket's CLOB via
py-clob-client. Requires POLYGON_PRIVATE_KEY (+ POLYMARKET_FUNDER and
POLYMARKET_SIGNATURE_TYPE for proxy-wallet accounts) and the explicit
POLYBOT_CONFIRM_LIVE=yes opt-in. Install extras: pip install py-clob-client
"""

import logging
from typing import Dict, List, Optional

from .config import Config
from .market_data import Book
from .portfolio import Portfolio

log = logging.getLogger("polybot.exec")


# ---------------------------------------------------------------- paper ----

def _paper_buy(d: dict, book: Optional[Book], portfolio: Portfolio) -> Optional[str]:
    if book is None or book.best_ask is None:
        return "no book data"
    limit = float(d["limit_price"])
    if limit < book.best_ask:
        return f"limit {limit} below best ask {book.best_ask} — no fill (paper mode doesn't rest orders)"
    fill_price = book.best_ask
    size_usd = float(d["size_usd"])
    shares = size_usd / fill_price
    if book.ask_size and shares > book.ask_size:
        shares = book.ask_size  # partial fill at top of book only
        size_usd = shares * fill_price
    portfolio.apply_buy(
        market_id=d["market_id"], question=d["question"], token_id=d["token_id"],
        outcome=d["outcome"], shares=shares, price=fill_price, mode="paper",
        rationale=d.get("rationale", ""), source=d.get("source", ""),
    )
    log.info("PAPER BUY %.2f shares of %s '%s' @ %.3f ($%.2f)",
             shares, d["outcome"], d["question"][:50], fill_price, size_usd)
    return None


def _paper_sell(d: dict, book: Optional[Book], portfolio: Portfolio) -> Optional[str]:
    pos = portfolio.positions.get(d["token_id"])
    if not pos:
        return "no such position"
    if book is None or book.best_bid is None:
        return "no book data"
    limit = float(d["limit_price"])
    if book.best_bid < limit:
        return f"best bid {book.best_bid} below limit {limit} — no fill"
    fill_price = book.best_bid
    sold_shares = pos.shares
    portfolio.apply_sell(d["token_id"], sold_shares, fill_price, "paper",
                         rationale=d.get("rationale", ""))
    log.info("PAPER SELL %.2f shares of %s '%s' @ %.3f",
             sold_shares, pos.outcome, pos.question[:50], fill_price)
    return None


# ----------------------------------------------------------------- live ----

_live_client = None


def _get_live_client(cfg: Config):
    global _live_client
    if _live_client is not None:
        return _live_client
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        raise SystemExit("live mode needs py-clob-client: pip install py-clob-client")

    kwargs = {"key": cfg.polygon_private_key, "chain_id": 137}
    if cfg.polymarket_signature_type:
        kwargs["signature_type"] = cfg.polymarket_signature_type
        kwargs["funder"] = cfg.polymarket_funder
    client = ClobClient("https://clob.polymarket.com", **kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    _live_client = client
    return client


def _live_order(cfg: Config, d: dict, side: str, shares: float, portfolio: Portfolio) -> Optional[str]:
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL

    client = _get_live_client(cfg)
    price = round(float(d["limit_price"]), 3)
    args = OrderArgs(
        token_id=d["token_id"],
        price=price,
        size=round(shares, 2),
        side=BUY if side == "buy" else SELL,
    )
    try:
        signed = client.create_order(args)
        resp = client.post_order(signed, OrderType.FOK)  # fill-or-kill: no resting risk
    except Exception as e:  # noqa: BLE001 — surface any client error as a skip
        return f"order error: {e}"

    if not resp or not resp.get("success"):
        return f"order rejected: {resp}"
    order_id = resp.get("orderID", "")
    if side == "buy":
        portfolio.apply_buy(
            market_id=d["market_id"], question=d["question"], token_id=d["token_id"],
            outcome=d["outcome"], shares=shares, price=price, mode="live",
            rationale=d.get("rationale", ""), source=d.get("source", ""), order_id=order_id,
        )
    else:
        portfolio.apply_sell(d["token_id"], shares, price, "live",
                             rationale=d.get("rationale", ""), order_id=order_id)
    log.info("LIVE %s %.2f shares @ %.3f on '%s' (order %s)",
             side.upper(), shares, price, d["question"][:50], order_id)
    return None


# ------------------------------------------------------------- dispatch ----

def execute(cfg: Config, portfolio: Portfolio, decisions: List[dict],
            books: Dict[str, Book]) -> List[str]:
    """Execute approved decisions. Returns a list of skip/error notes."""
    notes: List[str] = []
    for d in decisions:
        book = books.get(d["token_id"])
        if d["action"] == "open":
            if cfg.live:
                price = float(d["limit_price"])
                shares = float(d["size_usd"]) / price if price > 0 else 0
                err = _live_order(cfg, d, "buy", shares, portfolio)
            else:
                err = _paper_buy(d, book, portfolio)
        else:  # close
            pos = portfolio.positions.get(d["token_id"])
            if cfg.live:
                err = _live_order(cfg, d, "sell", pos.shares if pos else 0, portfolio)
            else:
                err = _paper_sell(d, book, portfolio)
        if err:
            notes.append(f"SKIP {d['action']} '{d['question'][:50]}': {err}")
            log.info(notes[-1])
    portfolio.save()
    return notes
