"""Orchestrator: one full trading cycle.

  market scan -> mark/settle positions -> arbitrage scan -> LLM scouts
  -> head-trader decision -> risk vetting -> execution -> cycle report
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .config import Config
from .copytrade import Watchlist, run_copytrade_scout
from .decision import decide
from .execution import execute
from .market_data import Book, attach_books, fetch_books, fetch_market_by_id, fetch_markets
from .portfolio import Portfolio
from .risk import vet_decisions
from .scouts import run_news_scout, run_value_scout, scan_arbitrage

log = logging.getLogger("polybot.runner")


def _mark_and_settle(cfg: Config, portfolio: Portfolio, books: Dict[str, Book]) -> None:
    """Mark open positions to market; settle positions in resolved markets."""
    held = list(portfolio.positions.values())
    missing = [p.token_id for p in held if p.token_id not in books]
    if missing:
        books.update(fetch_books(missing))

    for pos in held:
        book = books.get(pos.token_id)
        if book and book.mid is not None:
            portfolio.mark(pos.token_id, book.mid)
            continue
        # No book — market may have resolved. Check Gamma.
        m = fetch_market_by_id(pos.market_id)
        if m is None:
            continue
        try:
            idx = m.token_ids.index(pos.token_id)
            final = m.prices[idx]
        except (ValueError, IndexError):
            continue
        if not m.accepting_orders and (final >= 0.99 or final <= 0.01):
            settle_price = 1.0 if final >= 0.99 else 0.0
            portfolio.apply_sell(pos.token_id, pos.shares, settle_price, cfg.mode,
                                 rationale="market resolved", action="settle")
            log.info("SETTLED '%s' %s at %.0f", pos.question[:50], pos.outcome, settle_price)
        else:
            portfolio.mark(pos.token_id, final)


def run_cycle(cfg: Config, portfolio: Portfolio, skip_llm: bool = False) -> dict:
    started = datetime.now(timezone.utc)
    portfolio.record_day_start()

    # 1. Data
    markets = fetch_markets(cfg.scout.scan_market_limit)
    attach_books(markets, cfg.scout.book_fetch_limit)
    books: Dict[str, Book] = {}
    for m in markets:
        books.update(m.books)

    # 2. Mark existing positions / settle resolved ones
    _mark_and_settle(cfg, portfolio, books)
    portfolio.save()

    # 3. Arbitrage scan (pure code)
    arbs = [o.to_dict() for o in scan_arbitrage(markets, cfg.risk.arb_min_profit)]

    # 4. Copy-trade scout (pure data, no LLM cost)
    scout_reports: List[dict] = []
    if cfg.copytrade.enabled:
        try:
            watchlist = Watchlist(cfg.watchlist_file)
            held_markets = [p.market_id for p in portfolio.positions.values()]
            copy_report = run_copytrade_scout(cfg, watchlist, held_markets)
            scout_reports.append(copy_report)
            # Make copy-signal markets vettable: fetch their books too.
            copy_tokens = [r["token_id"] for r in copy_report.get("reports", [])
                           if r.get("token_id") and r["token_id"] not in books]
            if copy_tokens:
                books.update(fetch_books(copy_tokens))
        except Exception as e:  # noqa: BLE001 — a dead scout shouldn't kill the cycle
            log.error("copytrade scout failed: %s", e, exc_info=True)
            scout_reports.append({"scout": "copytrade", "reports": [],
                                  "scan_notes": f"copytrade scout error: {e}"})

    # 5. LLM scouts (run both in parallel)
    if not skip_llm:
        with ThreadPoolExecutor(max_workers=2) as pool:
            value_f = pool.submit(run_value_scout, cfg, markets)
            news_f = pool.submit(run_news_scout, cfg, markets)
            for fut, name in ((value_f, "value"), (news_f, "news")):
                try:
                    scout_reports.append(fut.result())
                except Exception as e:  # noqa: BLE001 — a dead scout shouldn't kill the cycle
                    log.error("%s scout failed: %s", name, e)
                    scout_reports.append({"reports": [], "scan_notes": f"{name} scout error: {e}"})

    # 6. Decision
    if skip_llm:
        decision = {"decisions": [], "commentary": "LLM stages skipped (--no-llm)"}
    else:
        decision = decide(cfg, portfolio, scout_reports, arbs)

    # 7. Risk vetting
    approved, vetoes = vet_decisions(cfg, portfolio, decision.get("decisions", []), books)

    # 8. Execution
    exec_notes = execute(cfg, portfolio, approved, books)

    report = {
        "ts": started.isoformat(),
        "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "mode": cfg.mode,
        "markets_scanned": len(markets),
        "arbitrage_opportunities": arbs,
        "scout_reports": scout_reports,
        "commentary": decision.get("commentary", ""),
        "decisions_proposed": decision.get("decisions", []),
        "decisions_approved": approved,
        "vetoes": vetoes,
        "execution_notes": exec_notes,
        "portfolio": portfolio.status(),
    }

    log_path = Path(cfg.log_dir) / f"cycle_{started.strftime('%Y%m%d_%H%M%S')}.json"
    log_path.write_text(json.dumps(report, indent=1))
    log.info("cycle complete in %ss — %d proposed, %d approved, equity $%.2f",
             report["duration_s"], len(report["decisions_proposed"]),
             len(approved), portfolio.equity)
    return report


def run_loop(cfg: Config, portfolio: Portfolio, interval_minutes: int) -> None:
    log.info("starting loop: cycle every %d minutes (mode=%s)", interval_minutes, cfg.mode)
    while True:
        try:
            run_cycle(cfg, portfolio)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 — keep the loop alive
            log.error("cycle failed: %s", e, exc_info=True)
        time.sleep(interval_minutes * 60)
