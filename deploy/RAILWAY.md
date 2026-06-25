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
