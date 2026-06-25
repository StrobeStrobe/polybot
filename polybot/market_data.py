"""Market data layer: Polymarket Gamma API (market metadata) and CLOB API
(orderbooks). Both are public read endpoints — no credentials needed."""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from .config import CLOB_API, GAMMA_API

log = logging.getLogger("polybot.data")

_session = requests.Session()
_session.headers["User-Agent"] = "polybot/0.1"


@dataclass
class Book:
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid if self.best_bid is not None else self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class Market:
    id: str
    question: str
    slug: str
    end_date: Optional[str]
    liquidity: float
    volume_24h: float
    outcomes: List[str]
    prices: List[float]            # gamma's last-trade-ish prices per outcome
    token_ids: List[str]
    neg_risk: bool
    event_id: Optional[str]
    event_title: Optional[str]
    accepting_orders: bool
    min_tick: float = 0.01
    books: Dict[str, Book] = field(default_factory=dict)  # token_id -> Book

    @property
    def days_to_resolution(self) -> Optional[float]:
        if not self.end_date:
            return None
        try:
            end = datetime.fromisoformat(self.end_date.replace("Z", "+00:00"))
            return (end - datetime.now(timezone.utc)).total_seconds() / 86400
        except ValueError:
            return None

    def book_for_outcome(self, idx: int) -> Optional[Book]:
        if idx < len(self.token_ids):
            return self.books.get(self.token_ids[idx])
        return None

    def summary(self) -> dict:
        """Compact JSON-able summary for LLM prompts."""
        out = {
            "market_id": self.id,
            "question": self.question,
            "event": self.event_title,
            "end_date": self.end_date,
            "days_to_resolution": round(self.days_to_resolution, 1) if self.days_to_resolution else None,
            "liquidity_usd": round(self.liquidity),
            "volume_24h_usd": round(self.volume_24h),
            "outcomes": [],
        }
        for i, name in enumerate(self.outcomes):
            o = {
                "outcome": name,
                "token_id": self.token_ids[i] if i < len(self.token_ids) else None,
                "price": self.prices[i] if i < len(self.prices) else None,
            }
            b = self.book_for_outcome(i)
            if b:
                o["best_bid"] = b.best_bid
                o["best_ask"] = b.best_ask
            out["outcomes"].append(o)
        return out


def _parse_json_field(raw, default):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
    return raw if raw is not None else default


def fetch_markets(limit: int = 250) -> List[Market]:
    """Fetch active, order-book-enabled markets ordered by 24h volume."""
    markets: List[Market] = []
    offset = 0
    page = min(limit, 100)
    while len(markets) < limit:
        resp = _session.get(
            f"{GAMMA_API}/markets",
            params={
                "closed": "false",
                "active": "true",
                "order": "volume24hr",
                "ascending": "false",
                "limit": page,
                "offset": offset,
            },
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for m in rows:
            if not m.get("enableOrderBook"):
                continue
            outcomes = _parse_json_field(m.get("outcomes"), [])
            prices = [float(p) for p in _parse_json_field(m.get("outcomePrices"), [])]
            token_ids = _parse_json_field(m.get("clobTokenIds"), [])
            if not outcomes or not token_ids:
                continue
            events = m.get("events") or []
            markets.append(
                Market(
                    id=str(m.get("id")),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    end_date=m.get("endDate"),
                    liquidity=float(m.get("liquidityNum") or 0),
                    volume_24h=float(m.get("volume24hr") or 0),
                    outcomes=outcomes,
                    prices=prices,
                    token_ids=token_ids,
                    neg_risk=bool(m.get("negRisk")),
                    event_id=str(events[0]["id"]) if events else None,
                    event_title=events[0].get("title") if events else None,
                    accepting_orders=bool(m.get("acceptingOrders")),
                    min_tick=float(m.get("orderPriceMinTickSize") or 0.01),
                )
            )
        offset += page
        if len(rows) < page:
            break
    log.info("fetched %d active markets from Gamma", len(markets))
    return markets[:limit]


def fetch_books(token_ids: List[str]) -> Dict[str, Book]:
    """Fetch orderbooks for a list of token ids via CLOB POST /books (batched)."""
    books: Dict[str, Book] = {}
    for i in range(0, len(token_ids), 50):
        chunk = token_ids[i : i + 50]
        try:
            resp = _session.post(
                f"{CLOB_API}/books",
                json=[{"token_id": t} for t in chunk],
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("batch book fetch failed (%s); falling back to per-token", e)
            payload = []
            for t in chunk:
                try:
                    r = _session.get(f"{CLOB_API}/book", params={"token_id": t}, timeout=15)
                    if r.ok:
                        payload.append(r.json())
                except requests.RequestException:
                    pass
                time.sleep(0.1)
        for raw in payload:
            token = raw.get("asset_id")
            if not token:
                continue
            book = Book(token_id=token)
            bids = raw.get("bids") or []
            asks = raw.get("asks") or []
            if bids:
                # CLOB returns bids/asks unsorted-ish; best bid = max price, best ask = min
                best = max(bids, key=lambda x: float(x["price"]))
                book.best_bid, book.bid_size = float(best["price"]), float(best["size"])
            if asks:
                best = min(asks, key=lambda x: float(x["price"]))
                book.best_ask, book.ask_size = float(best["price"]), float(best["size"])
            books[token] = book
    log.info("fetched %d orderbooks", len(books))
    return books


def attach_books(markets: List[Market], max_books: int = 120) -> None:
    """Attach orderbooks to the top markets (by 24h volume) in place."""
    ranked = sorted(markets, key=lambda m: m.volume_24h, reverse=True)
    token_ids: List[str] = []
    chosen: List[Market] = []
    for m in ranked:
        if len(token_ids) + len(m.token_ids) > max_books:
            continue
        token_ids.extend(m.token_ids)
        chosen.append(m)
        if len(token_ids) >= max_books:
            break
    books = fetch_books(token_ids)
    for m in chosen:
        m.books = {t: books[t] for t in m.token_ids if t in books}


def fetch_market_by_id(market_id: str) -> Optional[Market]:
    """Refresh a single market (used to mark positions / detect resolution)."""
    try:
        resp = _session.get(f"{GAMMA_API}/markets/{market_id}", timeout=15)
        if not resp.ok:
            return None
        m = resp.json()
    except requests.RequestException:
        return None
    outcomes = _parse_json_field(m.get("outcomes"), [])
    prices = [float(p) for p in _parse_json_field(m.get("outcomePrices"), [])]
    token_ids = _parse_json_field(m.get("clobTokenIds"), [])
    events = m.get("events") or []
    return Market(
        id=str(m.get("id")),
        question=m.get("question", ""),
        slug=m.get("slug", ""),
        end_date=m.get("endDate"),
        liquidity=float(m.get("liquidityNum") or 0),
        volume_24h=float(m.get("volume24hr") or 0),
        outcomes=outcomes,
        prices=prices,
        token_ids=token_ids,
        neg_risk=bool(m.get("negRisk")),
        event_id=str(events[0]["id"]) if events else None,
        event_title=events[0].get("title") if events else None,
        accepting_orders=bool(m.get("acceptingOrders")),
        min_tick=float(m.get("orderPriceMinTickSize") or 0.01),
    )
