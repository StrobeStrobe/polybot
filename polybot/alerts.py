"""Manual copy-trade alerting: poll watched wallets and notify the human
instead of trading. No LLM calls, no API key, no money at risk."""

import json
import logging
import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Config
from .copytrade import Watchlist, run_copytrade_scout

log = logging.getLogger("polybot.alerts")


def notify(title: str, message: str, sound: str = "Glass") -> None:
    """Fire a native desktop notification on macOS or Windows.

    The terminal print + alerts.log in alert_buy/alert_sell are the durable
    record; this is the best-effort pop-up. A failure here is logged, never
    fatal — the watcher keeps running.
    """
    system = platform.system()
    if system == "Darwin":
        _notify_macos(title, message, sound)
    elif system == "Windows":
        _notify_windows(title, message)
    else:
        log.debug("no desktop notifier for %s; terminal + log only", system)


def _notify_macos(title: str, message: str, sound: str) -> None:
    """json.dumps handles AppleScript string escaping."""
    script = (f"display notification {json.dumps(message)} "
              f"with title {json.dumps(title)} sound name {json.dumps(sound)}")
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("macOS notification failed: %s", e)


def _notify_windows(title: str, message: str) -> None:
    """Windows 11 toast via winotify (pip install winotify)."""
    try:
        from winotify import Notification, audio
    except ImportError:
        log.warning("winotify not installed — run: pip install winotify "
                    "(falling back to terminal + log)")
        return
    try:
        toast = Notification(app_id="Polybot", title=title, msg=message)
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception as e:  # noqa: BLE001 — notifier must never crash the watcher
        log.warning("Windows notification failed: %s", e)


# Backwards-compatible alias (older call sites / scripts).
notify_mac = notify


def _log_alert(cfg: Config, line: str) -> None:
    path = Path(cfg.alerts_log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with path.open("a") as f:
        f.write(f"[{stamp}] {line}\n")


def _market_url(event_slug: str) -> str:
    return f"https://polymarket.com/event/{event_slug}" if event_slug else ""


GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB   # tracked-wallet activity (raw mirror)


def post_discord(cfg: Config, embed: dict) -> None:
    """Post a rich embed to the configured Discord webhook. No-op (debug log)
    if no webhook is set, so the watcher runs fine without Discord."""
    url = cfg.discord_webhook_url
    if not cfg.discord_enabled:
        log.debug("discord_enabled is false — skipping Discord post")
        return
    if not url:
        log.debug("no DISCORD_WEBHOOK_URL set — skipping Discord post")
        return
    try:
        resp = requests.post(url, json={"embeds": [embed]}, timeout=10)
        if resp.status_code not in (200, 204):
            log.warning("Discord webhook returned %s: %s", resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("Discord webhook post failed: %s", e)


def alert_buy(cfg: Config, r: dict) -> None:
    traders = r.get("traders", [])
    lead = max(traders, key=lambda t: t.get("bet_usd", 0)) if traders else {}
    who = lead.get("name", "?")
    wr = lead.get("win_rate", 0)
    n_more = r.get("num_watched_traders", 1) - 1
    who_str = f"{who} ({wr:.0%} win rate)" + (f" +{n_more} more" if n_more > 0 else "")
    url = _market_url(r.get("event_slug", ""))
    title = f"🟢 COPY SIGNAL: ${r.get('total_smart_money_usd', 0):,.0f} bet"
    msg = (f"{r.get('title')} — {r.get('outcome')} @ {r.get('smart_money_avg_entry')} "
           f"(ask now {r.get('current_best_ask')}) by {who_str}")

    embed = {
        "title": title,
        "color": GREEN,
        "description": f"**{r.get('title')}**" + (f"\n[View on Polymarket]({url})" if url else ""),
        "fields": [
            {"name": "Outcome", "value": str(r.get("outcome", "—")), "inline": True},
            {"name": "Their entry", "value": str(r.get("smart_money_avg_entry", "—")), "inline": True},
            {"name": "Ask now", "value": str(r.get("current_best_ask", "—")), "inline": True},
            {"name": "Smart money", "value": f"${r.get('total_smart_money_usd', 0):,.0f}", "inline": True},
            {"name": "Bettor(s)", "value": who_str, "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_discord(cfg, embed)
    if cfg.desktop_notifications:
        notify(title, msg)
    print(f"\n{'=' * 70}\n{title}\n{msg}\n{url}\n{'=' * 70}")
    _log_alert(cfg, f"BUY  | {msg} | {url}")


def alert_sell(cfg: Config, s: dict) -> None:
    url = _market_url(s.get("event_slug", ""))
    title = f"🔴 EXIT SIGNAL: {s.get('trader')} sold ${s.get('their_usd', 0):,.0f}"
    msg = (f"{s.get('title')} — {s.get('outcome')} @ {s.get('their_price')} "
           f"(if you copied this market, consider exiting)")

    embed = {
        "title": title,
        "color": RED,
        "description": f"**{s.get('title')}**" + (f"\n[View on Polymarket]({url})" if url else ""),
        "fields": [
            {"name": "Outcome", "value": str(s.get("outcome", "—")), "inline": True},
            {"name": "They sold at", "value": str(s.get("their_price", "—")), "inline": True},
            {"name": "Size", "value": f"${s.get('their_usd', 0):,.0f}", "inline": True},
            {"name": "Trader", "value": str(s.get("trader", "—")), "inline": True},
        ],
        "footer": {"text": "If you copied this market, consider exiting."},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_discord(cfg, embed)
    if cfg.desktop_notifications:
        notify(title, msg, sound="Basso")
    print(f"\n{'=' * 70}\n{title}\n{msg}\n{url}\n{'=' * 70}")
    _log_alert(cfg, f"SELL | {s.get('trader')} | {msg} | {url}")


def send_test_alert(cfg: Config) -> None:
    """Fire one sample buy + sell down every configured channel, to verify
    Discord / desktop / log wiring before relying on it."""
    print("Sending a test buy + sell alert down all configured channels...")
    alert_buy(cfg, {
        "title": "TEST — Houston Astros vs. Los Angeles Angels: O/U 8.5",
        "outcome": "Under", "event_slug": "",
        "smart_money_avg_entry": 0.48, "current_best_ask": 0.49,
        "total_smart_money_usd": 3360.0, "num_watched_traders": 2,
        "traders": [{"name": "afghj2421", "win_rate": 0.65, "bet_usd": 2360.0}],
    })
    alert_sell(cfg, {
        "trader": "bananawoin", "their_usd": 1850.0,
        "title": "TEST — Yankees vs. Red Sox", "outcome": "Yankees",
        "their_price": 0.61, "event_slug": "",
    })
    where = "Discord + " if cfg.discord_webhook_url else ""
    print(f"\nSent. Check {where}your desktop + {cfg.alerts_log_file}")
    if not cfg.discord_webhook_url:
        print("(No DISCORD_WEBHOOK_URL set — add it to .env to enable Discord.)")


def _sport_tag(a: dict) -> str:
    """Human-readable profitability tag for the bet's sport, from the trader's
    cached per-sport record (with the date it was computed, so you know how
    fresh the ✅/❌ is)."""
    sport = a.get("sport") or "Other"
    rec = a.get("sport_record")
    asof = f" (as of {a['sport_asof']})" if a.get("sport_asof") else ""
    if rec and rec.get("markets"):
        pnl, wr, n = rec.get("pnl", 0), rec.get("win_rate", 0), rec["markets"]
        thin = " ⚠️thin" if n < 10 else ""
        if pnl > 0:
            return f"✅ profitable at {sport}: {wr:.0%} W, ${pnl:+,.0f} / {n} bets{thin}{asof}"
        return f"❌ UNprofitable at {sport}: {wr:.0%} W, ${pnl:+,.0f} / {n} bets{thin}{asof}"
    return f"❔ no track record at {sport}"


def alert_tracked(cfg: Config, a: dict) -> None:
    """Position-change alert for a manually-tracked wallet (fills coalesced:
    `usd` is the new money since the last alert, `position_usd` the total)."""
    url = _market_url(a.get("event_slug", ""))
    side = a.get("side", "")
    emoji = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "🔵"
    fills = int(a.get("fills") or 1)
    pos = float(a.get("position_usd") or a.get("usd") or 0)
    usd = float(a.get("usd") or 0)
    title = f"👁 {a.get('label')}: {side} ${usd:,.0f}"
    if fills > 1:
        title += f" ({fills} fills)"
    msg = f"{a.get('title')} — {a.get('outcome')} @ {a.get('price')}"
    if pos > usd + 0.5:  # a re-alert on growth: show the whole position
        msg += f" (position now ${pos:,.0f})"
    # Price drift: is the copy still there, or has the market already moved?
    now_p, their_p = a.get("now_price"), float(a.get("price") or 0)
    drift = ""
    if now_p is not None and their_p > 0:
        drift = f"{now_p:.2f} ({now_p - their_p:+.2f} vs their entry)"
        msg += f" | now {drift}"
    tag = _sport_tag(a)
    wallet = a.get("wallet", "")
    profile = f"https://polymarket.com/profile/{wallet}" if wallet else ""
    links = " · ".join(filter(None, [
        f"[View market]({url})" if url else "",
        f"[Trader profile]({profile})" if profile else "",
    ]))
    embed = {
        "title": f"{emoji} {title}",
        "color": GREEN if side == "BUY" else RED if side == "SELL" else BLUE,
        "description": f"**{a.get('title')}**\n{tag}" + (f"\n{links}" if links else ""),
        "fields": [
            {"name": "Side", "value": side or "—", "inline": True},
            {"name": "Outcome", "value": str(a.get("outcome", "—")), "inline": True},
            {"name": "Avg price", "value": str(a.get("price", "—")), "inline": True},
            {"name": "Price now", "value": drift or "—", "inline": True},
            {"name": "New money", "value": f"${usd:,.0f}"
             + (f" ({fills} fills)" if fills > 1 else ""), "inline": True},
            {"name": "Position total", "value": f"${pos:,.0f}", "inline": True},
            {"name": f"{a.get('label', '—')} at {a.get('sport', 'Other')}",
             "value": tag, "inline": False},
            {"name": "Wallet", "value": wallet or "—", "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_discord(cfg, embed)
    print(f"\n{'=' * 70}\n{emoji} {title}\n{msg}\n{tag}\n{profile}\n{url}\n{'=' * 70}")
    _log_alert(cfg, f"TRACK| {a.get('label')} | {wallet} | {side} {msg} | {tag} | {url}")


def alert_consensus(cfg: Config, c: dict) -> None:
    """2+ tracked wallets just landed on the same market & outcome — the
    strongest copy signal the tracker produces. Fired loud and separate."""
    url = _market_url(c.get("event_slug", ""))
    who = ", ".join(c.get("labels", []))
    title = f"🔥 CONSENSUS: {len(c.get('labels', []))} tracked wallets on {c.get('outcome')}"
    msg = f"{c.get('title')} — {who} | combined ${c.get('total_usd', 0):,.0f} @ ~{c.get('avg_price')}"
    embed = {
        "title": title,
        "color": 0xF39C12,  # orange — stands out from buy/sell green/red
        "description": f"**{c.get('title')}**\n{who}"
                       + (f"\n[View market]({url})" if url else ""),
        "fields": [
            {"name": "Outcome", "value": str(c.get("outcome", "—")), "inline": True},
            {"name": "Combined size", "value": f"${c.get('total_usd', 0):,.0f}", "inline": True},
            {"name": "Avg entry", "value": str(c.get("avg_price", "—")), "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_discord(cfg, embed)
    if cfg.desktop_notifications:
        notify(title, msg, sound="Hero")
    print(f"\n{'=' * 70}\n{title}\n{msg}\n{url}\n{'=' * 70}")
    _log_alert(cfg, f"CONS | {msg} | {url}")


def _maybe_self_check(cfg: Config, tracked) -> None:
    """Once a day, reconcile each tracked wallet's open positions against its
    trade feed. The takerOnly bug hid maker fills exactly this way (position
    visible, trades silent) for months — this turns that failure mode into a
    Discord warning instead of silent bad data. Warns once per position."""
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    marker = Path(cfg.alerts_log_file).parent / "last_selfcheck.txt"
    try:
        if marker.exists() and marker.read_text().strip() == day_key:
            return
    except OSError:
        pass
    from .copytrade import positions_trades_gap
    warned_path = Path(cfg.alerts_log_file).parent / "selfcheck_warned.json"
    try:
        warned = set(json.loads(warned_path.read_text()))
    except (OSError, ValueError):
        warned = set()
    log.info("running daily positions-vs-trades self-check")
    new_gaps = []
    for w in tracked.wallets:
        try:
            for gap in positions_trades_gap(w.wallet):
                key = f"{w.wallet}|{gap['conditionId']}"
                if key in warned:
                    continue
                warned.add(key)
                new_gaps.append(f"**{w.label or w.wallet[:10]}**: "
                                f"${gap['usd']:,.0f} on “{gap['title']}” "
                                f"— in /positions but not in the trade feed")
        except Exception as e:  # noqa: BLE001 — self-check must never break polling
            log.warning("self-check failed for %s: %s", w.label or w.wallet[:10], e)
    if new_gaps:
        post_discord(cfg, {
            "title": "⚠️ Data self-check: positions without matching trades",
            "color": 0xF1C40F,
            "description": ("These open positions never showed in the trades API "
                            "(21-day lookback). Either the feed is dropping fills "
                            "again (takerOnly-style) or the position is older than "
                            "the lookback — worth an eyeball:\n\n"
                            + "\n".join(new_gaps))[:3900],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        _log_alert(cfg, f"CHECK| {len(new_gaps)} position/trade gaps flagged")
    try:
        marker.write_text(day_key)
        warned_path.write_text(json.dumps(sorted(warned)))
    except OSError:
        pass


def _run_cycle(cfg: Config, watchlist, tracked) -> tuple:
    """One poll: copy scout (if enabled) + tracked-wallet mirror. Fires alerts,
    writes the heartbeat, returns (n_buy, n_sell, n_tracked). Shared by the
    continuous watcher and the run-once (GitHub Actions / cron) path."""
    from .tracked import scan_tracked
    scout_on = cfg.copytrade.enabled
    n_buy = n_sell = 0
    if scout_on:
        report = run_copytrade_scout(cfg, watchlist, held_market_ids=[],
                                     report_all_sells=True)
        for r in report.get("reports", []):
            alert_buy(cfg, r)
        for s in report.get("exit_signals", []):
            alert_sell(cfg, s)
        n_buy = len(report.get("reports", []))
        n_sell = len(report.get("exit_signals", []))
    tracked_alerts = scan_tracked(cfg, tracked)
    for a in tracked_alerts:
        alert_tracked(cfg, a)
    # Ledger + consensus: record every alert for would-be-PnL scoring, and
    # fire a 🔥 when 2+ tracked wallets converge on the same market & outcome.
    try:
        from . import ledger
        for c in ledger.record_alerts(cfg, tracked_alerts):
            alert_consensus(cfg, c)
    except Exception as e:  # noqa: BLE001 — bookkeeping must never break polling
        log.error("ledger/consensus failed: %s", e)
    _maybe_self_check(cfg, tracked)
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] checked — {n_buy} buy, {n_sell} exit, {len(tracked_alerts)} tracked")
    try:
        hb = Path(cfg.alerts_log_file).parent / "last_check.txt"
        hb.write_text(
            f"last checked {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} "
            f"— {len(watchlist.traders) if scout_on else 0} vetted, "
            f"{len(tracked.wallets)} tracked, "
            f"{n_buy} buy / {n_sell} exit / {len(tracked_alerts)} tracked alerts\n"
        )
    except OSError:
        pass
    _maybe_weekly_report(cfg)
    return n_buy, n_sell, len(tracked_alerts)


def _maybe_weekly_report(cfg: Config) -> None:
    """Fire the weekly PnL report once per week — Sunday at/after 8 AM Eastern.
    Runs inside the watcher (which polls every few min), so no separate cron or
    GitHub Action is needed. DST-correct via zoneinfo."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — no tz data: approximate with UTC
        now = datetime.now(timezone.utc)
    # Fire window: Sunday 8-11 AM ET only. The done-marker lives on ephemeral
    # disk (Railway), so a redeploy later in the day would otherwise re-post.
    if now.weekday() != 6 or not (8 <= now.hour < 12):  # 6 = Sunday
        return
    week_key = now.strftime("%G-W%V")
    marker = Path(cfg.alerts_log_file).parent / "last_weekly.txt"
    try:
        if marker.exists() and marker.read_text().strip() == week_key:
            return  # already sent this week
    except OSError:
        pass
    log.info("posting weekly PnL report (%s)", week_key)
    try:
        from .copytrade import weekly_wallet_pnl
        from . import ledger
        reports, window = weekly_wallet_pnl(cfg, 7)
        post_weekly_report(cfg, reports, window, ledger.settle(cfg))
        marker.write_text(week_key)
    except Exception as e:  # noqa: BLE001 — a report failure shouldn't kill the watcher
        log.error("weekly report failed: %s", e)


def run_once(cfg: Config) -> None:
    """Single poll then exit — for GitHub Actions / cron. State (last_seen_ts)
    persists in the JSON files, which the caller commits back between runs."""
    from .tracked import TrackedList
    watchlist = Watchlist(cfg.watchlist_file)
    tracked = TrackedList(cfg.tracked_wallets_file)
    try:
        _run_cycle(cfg, watchlist, tracked)
    except Exception as e:  # noqa: BLE001 — surface but don't fail the whole job
        log.error("run-once cycle failed: %s", e, exc_info=True)


def post_weekly_report(cfg: Config, reports: list, window: str,
                       ledger_summary: dict = None) -> None:
    """Post a per-wallet weekly PnL scorecard (total + by sport) to Discord,
    with a would-be copy-PnL line from the alert ledger and a ⚠️ cold-streak
    flag on wallets deeply negative over 30 days."""
    combined = sum(r["net_official"] for r in reports)
    fields = []
    for r in reports:
        sports = sorted(r["by_sport"].items(), key=lambda kv: kv[1]["pnl"], reverse=True)
        lines = [f"{s}: ${x['pnl']:+,.0f} ({x['win_rate']:.0%}/{x['markets']})" for s, x in sports]
        pend = f"  ·  {r['pending']} open" if r["pending"] else ""
        cold = ""
        if r.get("net_30d", 0) < -2000:
            cold = f"\n⚠️ **cold streak: ${r['net_30d']:+,.0f} over 30d** — copy with care"
        body = (f"**net {window}: ${r['net_official']:+,.0f}**{pend}{cold}\n"
                + ("\n".join(lines) if lines else "_no resolved bets this week_"))
        fields.append({"name": r["label"], "value": body[:1024], "inline": False})
    desc = (f"Combined net: **${combined:+,.0f}** across {len(reports)} wallets\n"
            f"_'net' = Polymarket's realized {window} figure; sport rows = bets "
            f"placed this week that resolved (a different slice — won't sum to net)._")
    ls = ledger_summary
    if ls and (ls.get("settled") or ls.get("open")):
        desc += (f"\n\n**Copy scorecard** (${ls['stake']:.0f}/alert): "
                 f"{ls['wins']}/{ls['settled']} won, "
                 f"would-be **${ls['copy_pnl']:+,.0f}** this week "
                 f"({ls['open']} alerts still open)")
    embed = {
        "title": f"📊 Tracked-wallet PnL — last {window}",
        "description": desc,
        "color": GREEN if combined >= 0 else RED,
        "fields": fields[:25],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_discord(cfg, embed)
    print(f"\n=== Weekly PnL ({window}) — combined ${combined:+,.0f} ===")
    for r in reports:
        print(f"\n{r['label']} ({r['wallet']}): net {window} ${r['net_official']:+,.0f}"
              f"  ({r['pending']} open)")
        for s, x in sorted(r["by_sport"].items(), key=lambda kv: kv[1]["pnl"], reverse=True):
            print(f"   {s:8} ${x['pnl']:>+10,.0f}  ({x['win_rate']:.0%} / {x['markets']} bets)")


def watch_loop(cfg: Config, interval_minutes: float) -> None:
    from .tracked import TrackedList
    watchlist = Watchlist(cfg.watchlist_file)
    tracked = TrackedList(cfg.tracked_wallets_file)
    # On a managed host (Railway/Render) the filesystem is ephemeral and the
    # committed last_seen_ts can be stale, so a redeploy would re-alert the
    # whole backlog. Reseed to "now" on startup to alert only going forward.
    if os.environ.get("TRACKED_RESEED_ON_START", "").strip().lower() in ("1", "true", "yes"):
        now = int(time.time())
        for w in tracked.wallets:
            w.last_seen_ts = now
        tracked.save()
        log.info("reseeded %d tracked wallets to now (TRACKED_RESEED_ON_START)",
                 len(tracked.wallets))
    # One-time deploy self-test: fire a sample alert so you can confirm Discord
    # works on this host without waiting for a real trade. Set the env var,
    # confirm the alert lands, then remove it.
    if os.environ.get("SEND_TEST_ALERT_ON_START", "").strip().lower() in ("1", "true", "yes"):
        log.info("SEND_TEST_ALERT_ON_START set — firing one sample alert")
        send_test_alert(cfg)
    channels = []
    if cfg.discord_webhook_url and cfg.discord_enabled:
        channels.append("Discord")
    elif cfg.discord_webhook_url:
        channels.append("Discord(PAUSED)")
    if cfg.desktop_notifications:
        channels.append("desktop")
    channels.append(cfg.alerts_log_file)
    scout_on = cfg.copytrade.enabled
    print(f"Polling every {interval_minutes:g} min. Alerts → {', '.join(channels)}\n"
          f"  • copy scout: {len(watchlist.traders) if scout_on else 0} vetted traders"
          f"{'' if scout_on else ' (disabled)'}\n"
          f"  • tracked wallets: {len(tracked.wallets)}\n"
          f"Ctrl-C to stop.\n")
    for w in tracked.wallets:
        print(f"  👁 {w.label or w.wallet[:10]}  {w.wallet}")
    while True:
        try:
            _run_cycle(cfg, watchlist, tracked)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 — a blip shouldn't kill the watcher
            log.error("watch cycle failed: %s", e)
        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            print("\nstopped.")
            return
