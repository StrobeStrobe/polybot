# Reliable 24/7 on Railway (continuous loop, no server to manage)

GitHub Actions' scheduler is best-effort and skips/delays runs. Railway runs
the watcher as a **continuous process** that polls every 3 minutes reliably —
no SSH, no Linux, just a web UI connected to your GitHub repo.

Cost: a small trial credit, then ~$5/month (this process is tiny).

## Steps

### 1. Push the deploy files
In GitHub Desktop you'll see new files (`Procfile`, updated `alerts.py`).
Commit them ("add railway deploy") and **Push origin**.

### 2. Create the Railway service
1. Go to **railway.com** → sign up with your GitHub account.
2. **New Project → Deploy from GitHub repo → polybot**.
3. Railway auto-detects Python (via `requirements.txt`) and uses the `Procfile`
   start command `python run.py watch`. If it asks for a start command, use:
   ```
   python run.py watch
   ```

### 3. Add environment variables
In the service → **Variables** tab, add:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | your webhook URL |
| `DISCORD_ENABLED` | `true` |
| `TRACKED_RESEED_ON_START` | `true` |

(`TRACKED_RESEED_ON_START` makes a restart pick up only *new* trades instead of
re-alerting the backlog.)

### 4. Deploy
Railway builds and starts it. Open the **Deploy logs** — you should see the
startup banner and a `checked — …` line every 3 minutes. Alerts now flow to
Discord continuously.

### 5. Turn OFF the GitHub Actions workflow
So you don't get duplicate alerts from both:
GitHub repo → **Actions** tab → **polybot-watch** → **⋯ → Disable workflow**.

## Day-to-day
- **Add/remove wallets**: `run.py track ...` locally → commit + push. Railway
  auto-redeploys on push and picks up the new wallet.
- **See it running**: Railway dashboard → Deploy logs (live), or
  `state/last_check.txt` heartbeat.
- **Pause**: Railway → service → Settings → remove, or just disable.

## Why this and not the others
- **GitHub Actions** (free): scheduler unreliable — fine for loose timing, not
  for catching big bets within minutes.
- **Railway/Render** (~$5/mo): continuous process, reliable 3-min polling, web
  UI. ← you are here.
- **VPS** (~$4/mo): also reliable, cheapest, but needs SSH/Linux (deploy/VPS.md).

---

## Persistent state (IMPORTANT — do this once)

Railway containers have an **ephemeral filesystem**. Every deploy starts a
fresh container, so anything the watcher computed at runtime is thrown away
and reset to whatever is committed in git. That wipes:

- each tracked wallet's **sport and size records** (alerts revert to ❔)
- `last_seen_ts` (risking a re-alert of the backlog, or missed trades)
- the **resolution cache** (~450KB — has to be refetched market by market)
- the **copy-performance ledger**, which can never accumulate enough
  history to tell you whether copying is actually profitable

### Fix: mount a volume

1. Railway project → your service → **Variables** → add:
   ```
   POLYBOT_STATE_DIR=/data
   ```
2. Service → **Settings → Volumes → Add Volume**, mount path `/data`.
3. Redeploy.

On the first boot with an empty volume, the committed `state/` files are
copied in automatically. From then on the volume is authoritative for
runtime state.

### How the tracked wallet list stays in sync

The roster (who you track, labels, per-wallet `min_usd`) always comes from
**git** — edit it locally, push, and the change takes effect. The computed
fields (`by_sport`, `by_size`, `last_seen_ts`, `open_alerts`) are preserved
from the volume for any wallet that's still on the list. Add a wallet and it
appears with no records yet; remove one and its state is dropped.

Startup logs the merge, e.g.:
`tracked wallets: 15 from git, kept computed state for 14, dropped 1 no longer tracked`
