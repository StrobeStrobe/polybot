#!/usr/bin/env python3
"""Polybot CLI.

  python run.py cycle            # run one full trading cycle
  python run.py cycle --no-llm   # scan + arbitrage only (free, no API key needed)
  python run.py loop --interval 60
  python run.py status           # portfolio snapshot
  python run.py scan             # show what the scouts would look at + arb scan
"""

import argparse
import json
import logging
import sys

from polybot.config import load_config
from polybot.market_data import attach_books, fetch_markets
from polybot.portfolio import Portfolio
from polybot.runner import run_cycle, run_loop
from polybot.scouts.arbitrage import scan_arbitrage
from polybot.scouts.llm_scout import _filter_candidates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket trading bot")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cycle = sub.add_parser("cycle", help="run one trading cycle")
    p_cycle.add_argument("--no-llm", action="store_true",
                         help="skip LLM scouts and decisions (arb scan only)")

    p_loop = sub.add_parser("loop", help="run cycles forever")
    p_loop.add_argument("--interval", type=int, default=60, help="minutes between cycles")

    sub.add_parser("status", help="show portfolio")
    sub.add_parser("scan", help="dry scan: candidates + arbitrage, no trading")
    p_traders = sub.add_parser("traders", help="show/refresh the copy-trade watchlist")
    p_traders.add_argument("--refresh", action="store_true", help="force rebuild from leaderboard")
    p_watch = sub.add_parser("watch", help="alert-only mode: notify when watched traders bet (no trading, no API key)")
    p_watch.add_argument("--interval", type=float, default=3, help="poll interval in minutes (default 3)")
    p_watch.add_argument("--test", action="store_true", help="send one sample alert to all channels and exit")
    sub.add_parser("once", help="run a single poll then exit (for GitHub Actions / cron)")
    p_track = sub.add_parser("track", help="manage manually-tracked wallets (raw activity mirror)")
    track_sub = p_track.add_subparsers(dest="track_cmd", required=True)
    pt_add = track_sub.add_parser("add", help="track a wallet (address or profile URL)")
    pt_add.add_argument("wallet")
    pt_add.add_argument("--label", default="", help="friendly name shown in alerts")
    pt_rm = track_sub.add_parser("remove", help="stop tracking a wallet")
    pt_rm.add_argument("wallet")
    track_sub.add_parser("list", help="list tracked wallets")

    args = parser.parse_args()
    cfg = load_config()
    portfolio = Portfolio(cfg.state_file, cfg.bankroll_usd)

    if args.cmd == "status":
        print(json.dumps(portfolio.status(), indent=2))
        return

    if args.cmd == "scan":
        markets = fetch_markets(cfg.scout.scan_market_limit)
        attach_books(markets, cfg.scout.book_fetch_limit)
        arbs = scan_arbitrage(markets, cfg.risk.arb_min_profit)
        cands = _filter_candidates(markets, cfg)
        print(f"\n{len(markets)} markets scanned, {len(cands)} scout candidates, {len(arbs)} arbs\n")
        print("Top scout candidates:")
        for m in sorted(cands, key=lambda x: x.volume_24h, reverse=True)[:15]:
            print(f"  [{m.id}] {m.question[:70]}  vol24h=${m.volume_24h:,.0f}  prices={m.prices}")
        if arbs:
            print("\nArbitrage opportunities:")
            for a in arbs[:10]:
                print(f"  {a.kind}: {a.description}  profit/set=${a.profit_per_set:.3f} max_sets={a.max_sets:.0f}")
        return

    if args.cmd == "watch":
        from polybot.alerts import send_test_alert, watch_loop
        if args.test:
            send_test_alert(cfg)
        else:
            watch_loop(cfg, args.interval)
        return

    if args.cmd == "once":
        from polybot.alerts import run_once
        run_once(cfg)
        return

    if args.cmd == "track":
        from polybot.tracked import TrackedList, normalize_wallet
        tl = TrackedList(cfg.tracked_wallets_file)
        if args.track_cmd == "list":
            if not tl.wallets:
                print("No tracked wallets. Add one: run.py track add <wallet> --label name")
            for w in tl.wallets:
                print(f"  {w.label or '(no label)':20} {w.wallet}")
            return
        wallet = normalize_wallet(args.wallet)
        if not wallet:
            print(f"Not a valid wallet address or profile URL: {args.wallet}")
            return
        if args.track_cmd == "add":
            tl.add(wallet, args.label)
            print(f"Now tracking {args.label or wallet}  ({wallet})")
            print("Alerts begin on their NEXT trade. Restart the watcher to pick it up live.")
        elif args.track_cmd == "remove":
            print(f"Removed {wallet}" if tl.remove(wallet) else f"Not tracked: {wallet}")
        return

    if args.cmd == "traders":
        from polybot.copytrade import Watchlist
        wl = Watchlist(cfg.watchlist_file)
        if args.refresh or wl.stale(cfg.copytrade.refresh_days):
            print("Building watchlist from leaderboard (this takes a minute)...")
            wl.refresh(cfg)
        print(f"\nWatchlist (refreshed {wl.refreshed_at}):\n")
        for t in wl.traders:
            print(f"  {t.pseudonym[:22]:22} win {t.win_rate:>5.1%} over {t.resolved_markets:>3} mkts "
                  f"(entry {t.avg_entry:.2f}, edge {t.winrate_edge:+.1%})  "
                  f"all-time ${t.pnl_all:>11,.0f} (roi {t.roi_all:.1%})  sports {t.sports_share:.0%}  "
                  f"[{t.style or '?'}: {t.trades_per_day:g}/day, sleeps {t.quiet_hours}h]")
        if not wl.traders:
            print("  (none matched the filters — try lowering copytrade.min_monthly_pnl)")
        return

    if args.cmd == "cycle":
        if cfg.live:
            print(f"*** LIVE MODE — real orders will be placed (bankroll anchor ${cfg.bankroll_usd}) ***")
        report = run_cycle(cfg, portfolio, skip_llm=args.no_llm)
        print("\n=== CYCLE SUMMARY ===")
        print(f"Commentary: {report['commentary']}")
        print(f"Proposed: {len(report['decisions_proposed'])}, "
              f"Approved: {len(report['decisions_approved'])}, "
              f"Vetoed: {len(report['vetoes'])}")
        for note in report["execution_notes"]:
            print(f"  {note}")
        print(json.dumps(report["portfolio"], indent=2))
        return

    if args.cmd == "loop":
        run_loop(cfg, portfolio, args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
