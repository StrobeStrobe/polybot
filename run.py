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
    p_wk = sub.add_parser("weekly", help="post a per-tracked-wallet PnL scorecard (total + by sport) to Discord")
    p_wk.add_argument("--days", type=float, default=7, help="lookback window in days (default 7)")
    sub.add_parser("selfcheck", help="reconcile tracked wallets' positions vs trade feed (data-gap tripwire)")
    p_sl = sub.add_parser("sport-leaders", help="rank top traders per sport (report only, adds nothing)")
    p_sl.add_argument("sports", nargs="+", help="sport buckets, e.g. MLB Tennis Soccer NFL NCAAF")
    p_sl.add_argument("--min-bets", type=int, default=30, help="min resolved bets in the sport (default 30)")
    p_sl.add_argument("--top", type=int, default=3, help="how many per sport (default 3)")
    p_sl.add_argument("--use-cache", action="store_true", help="re-rank from the last evaluation instead of re-scanning")
    p_fl = sub.add_parser("season-leaders", help="top traders for an out-of-season sport's 2025 season (NFL/CFB)")
    p_fl.add_argument("sports", nargs="+", help="NFL, CFB, and/or UFC")
    p_fl.add_argument("--min-bets", type=int, default=20, help="min games bet in the season (default 20)")
    p_fl.add_argument("--top", type=int, default=3, help="how many per sport (default 3)")
    p_fl.add_argument("--since", default=None, help="YYYY-MM-DD; bound an ongoing series (UFC) to recent cards")
    p_fl.add_argument("--rank-by", choices=["pnl", "edge", "winrate"], default="pnl",
                      help="rank metric: pnl (dollars) | edge (skill per bet) | winrate")
    p_fl.add_argument("--use-cache", action="store_true", help="re-rank from the last evaluation, no re-scan")
    p_fl.add_argument("--min-alltime", type=float, default=None, help="override all-time PnL floor (lower it to surface small-bankroll sharps)")
    p_fl.add_argument("--min-avg-bet", type=float, default=0.0, help="min average bet size — cuts noise micro-bettors")
    p_fl.add_argument("--markets", choices=["moneyline", "all"], default="moneyline",
                      help="'moneyline' (default) or 'all' to also include full-game totals & spreads")
    p_fl.add_argument("--min-volume", type=float, default=20000.0,
                      help="with --markets all, skip alt-line markets below this $ volume (default 20000)")
    p_track = sub.add_parser("track", help="manage manually-tracked wallets (raw activity mirror)")
    track_sub = p_track.add_subparsers(dest="track_cmd", required=True)
    pt_add = track_sub.add_parser("add", help="track a wallet (address or profile URL); re-add to update label/min-usd")
    pt_add.add_argument("wallet")
    pt_add.add_argument("--label", default="", help="friendly name shown in alerts")
    pt_add.add_argument("--min-usd", type=float, default=0.0,
                        help="per-wallet alert floor (default: global tracked_min_usd)")
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

    if args.cmd == "weekly":
        from polybot.copytrade import weekly_wallet_pnl
        from polybot.alerts import post_weekly_report
        from polybot import ledger
        reports, window = weekly_wallet_pnl(cfg, args.days)
        post_weekly_report(cfg, reports, window, ledger.settle(cfg))
        return

    if args.cmd == "selfcheck":
        from polybot.copytrade import positions_trades_gap
        from polybot.tracked import TrackedList
        tl = TrackedList(cfg.tracked_wallets_file)
        clean = True
        for w in tl.wallets:
            gaps = positions_trades_gap(w.wallet)
            for g in gaps:
                clean = False
                print(f"  ⚠️ {w.label or w.wallet[:10]}: ${g['usd']:,.0f} on "
                      f"“{g['title']}” — in /positions but NOT in the trade feed")
            if not gaps:
                print(f"  ✓ {w.label or w.wallet[:10]}: positions ↔ trades consistent")
        if clean:
            print("\nAll tracked wallets clean — no takerOnly-style data gaps.")
        return

    if args.cmd == "sport-leaders":
        from polybot.copytrade import sport_leaders
        results, n = sport_leaders(cfg, args.sports, args.min_bets, args.top, args.use_cache)
        print(f"\n{'#' * 64}\nSPORT LEADERS — top {args.top} by edge (≥{args.min_bets} bets, positive edge)")
        print(f"Evaluated {n} qualifying sports traders from the leaderboards.\n{'#' * 64}")
        for sport in args.sports:
            lst = results.get(sport, [])
            print(f"\n=== {sport} — top {len(lst)} ===")
            if not lst:
                print(f"  (no trader cleared {args.min_bets}+ bets with positive edge)")
                continue
            for i, e in enumerate(lst, 1):
                r = e["sport_rec"]
                print(f"  {i}. {e['name']}")
                print(f"     {e['wallet']}")
                print(f"     https://polymarket.com/profile/{e['wallet']}")
                print(f"     {sport}: {r['win_rate']:.0%} win over {r['markets']} bets | "
                      f"avg entry {r['avg_entry']:.2f} | edge {r['edge']:+.0%} | PnL ${r['pnl']:+,.0f}")
                print(f"     (overall all-time ${e['pnl_all']:,.0f}, style: {e['style']})")
        return

    if args.cmd == "season-leaders":
        from polybot.copytrade import season_sport_leaders, SEASON_SERIES, SEASON_TAGS
        if args.min_alltime is not None:
            cfg.copytrade.min_alltime_pnl = args.min_alltime
        for raw in args.sports:
            sport = raw.upper()
            if sport not in SEASON_SERIES and sport not in SEASON_TAGS:
                print(f"  {sport}: no season series/tag configured "
                      f"(have: {', '.join([*SEASON_SERIES, *SEASON_TAGS])})")
                continue
            # UFC is one ongoing series — bound it to recent cards (default 12mo).
            since = args.since
            if sport == "UFC" and not since:
                from datetime import datetime, timedelta, timezone
                since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
            include_alt = args.markets == "all"
            ranked, n_mkts, n_wallets = season_sport_leaders(
                cfg, sport, args.min_bets, args.top, since, args.rank_by,
                args.use_cache, args.min_avg_bet, include_alt,
                args.min_volume if include_alt else 0.0)
            window = f"since {since}" if since else "2025 season"
            scope = "ML+totals+spreads" if include_alt else "moneyline"
            print(f"\n=== {sport} {window} — top {len(ranked)} by {args.rank_by} "
                  f"({scope}, from {n_mkts} game markets, {n_wallets} wallets) ===")
            if not ranked:
                print(f"  (no wallet cleared {args.min_bets}+ games with positive edge & PnL)")
                continue
            for i, e in enumerate(ranked, 1):
                marks = ("" + (" ✓wallet-verified" if e.get("verified") else "")
                         + (" 🐋rescued" if e.get("rescued") else ""))
                print(f"  {i}. {e['name']}{marks}")
                print(f"     {e['wallet']}")
                print(f"     https://polymarket.com/profile/{e['wallet']}")
                print(f"     {sport}: {e['win_rate']:.0%} win over {e['markets']} games | "
                      f"entry {e['avg_entry']:.2f} | edge {e['edge']:+.0%} | PnL ${e['pnl']:+,.0f}")
                print(f"     avg bet ${e.get('avg_bet',0):,.0f} (${e.get('wagered',0):,.0f} wagered) | "
                      f"all-time ${e['pnl_all']:,.0f}, roi {e['roi_all']:.1%}, style: {e['style']}")
        return

    if args.cmd == "track":
        from polybot.tracked import TrackedList, normalize_wallet
        tl = TrackedList(cfg.tracked_wallets_file)
        if args.track_cmd == "list":
            if not tl.wallets:
                print("No tracked wallets. Add one: run.py track add <wallet> --label name")
            for w in tl.wallets:
                floor = f"  (min ${w.min_usd:,.0f})" if w.min_usd else ""
                print(f"  {w.label or '(no label)':20} {w.wallet}{floor}")
            return
        wallet = normalize_wallet(args.wallet)
        if not wallet:
            print(f"Not a valid wallet address or profile URL: {args.wallet}")
            return
        if args.track_cmd == "add":
            w = tl.add(wallet, args.label, args.min_usd)
            floor = f" (alerts only above ${w.min_usd:,.0f})" if w.min_usd else ""
            print(f"Now tracking {args.label or wallet}  ({wallet}){floor}")
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
            # Per-sport PnL on the sampled recent resolved markets, best first.
            # This is a recent-form sample (capped at max_markets_checked), NOT
            # career totals — it won't sum to all-time PnL.
            if t.by_sport:
                parts = []
                for sp, d in sorted(t.by_sport.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
                    parts.append(f"{sp} {d['win_rate']:.0%}/{d['markets']}mkt ${d['pnl']:+,.0f}")
                sport_total = sum(d["pnl"] for d in t.by_sport.values())
                print(f"        └ by sport (last {t.resolved_markets} resolved mkts, "
                      f"${sport_total:+,.0f} of them): {'  '.join(parts)}")
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
