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


def alert_tracked(cfg: Config, a: dict) -> None:
    """Raw-mirror alert for a manually-tracked wallet (any market, buy/sell)."""
    url = _market_url(a.get("event_slug", ""))
    side = a.get("side", "")
    emoji = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "🔵"
    title = f"👁 {a.get('label')}: {side} ${a.get('usd', 0):,.0f}"
    msg = f"{a.get('title')} — {a.get('outcome')} @ {a.get('price')}"
    embed = {
        "title": f"{emoji} {title}",
        "color": GREEN if side == "BUY" else RED if side == "SELL" else BLUE,
        "description": f"**{a.get('title')}**" + (f"\n[View on Polymarket]({url})" if url else ""),
        "fields": [
            {"name": "Side", "value": side or "—", "inline": True},
            {"name": "Outcome", "value": str(a.get("outcome", "—")), "inline": True},
            {"name": "Price", "value": str(a.get("price", "—")), "inline": True},
            {"name": "Size", "value": f"${a.get('usd', 0):,.0f}", "inline": True},
            {"name": "Wallet", "value": str(a.get("label", "—")), "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_discord(cfg, embed)
    print(f"\n{'=' * 70}\n{emoji} {title}\n{msg}\n{url}\n{'=' * 70}")
    _log_alert(cfg, f"TRACK| {a.get('label')} | {side} {msg} | {url}")


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
    return n_buy, n_sell, len(tracked_alerts)


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
