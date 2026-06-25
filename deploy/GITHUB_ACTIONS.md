# Running Polybot free on GitHub Actions (no server)

GitHub runs the watcher on a timer in the cloud — no VPS, nothing to keep on.
It checks every ~5 minutes (GitHub's scheduler is best-effort, so figure
5-15 min), posts new trades to Discord, and remembers where it left off by
committing the small state files back to the repo.

## Cost / privacy: use a PUBLIC repo

- **Public repo → GitHub Actions is free and unlimited.** Recommended.
- A private repo only gets 2,000 free Action-minutes/month, which a 5-minute
  timer burns through. (If you need private, raise the cron interval to ~30 min.)
- What's in the repo: the bot code + which wallet addresses you track. Those
  addresses are already public on-chain. **Your Discord webhook is NOT in the
  repo** — it's stored as an encrypted GitHub secret (step 3).

## Setup (about 5 minutes)

### 1. Put the project on GitHub

Easiest is **GitHub Desktop** (desktop.github.com — works on Mac & Windows):
1. Install it, sign in (create a free github.com account if needed).
2. File → Add Local Repository → pick the `polybot` folder → "create a
   repository" when prompted.
3. Click **Publish repository**. Leave "Keep this code private" **unchecked**
   (public — see above). Publish.

(CLI alternative, from the polybot folder:)
```bash
git init && git add -A && git commit -m "polybot"
gh repo create polybot --public --source=. --push     # needs the gh CLI
```

### 2. Add your tracked wallets (before or after publishing)

```bash
.venv/bin/python run.py track add <wallet-or-profile-url> --label "name"
```
Commit + push the change (GitHub Desktop: it shows the changed
`state/tracked_wallets.json` — commit, then Push).

### 3. Add the Discord webhook as a secret

On github.com, in your repo:
**Settings → Secrets and variables → Actions → New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Value: your webhook URL
- Add secret.

### 4. Enable Actions

Open the **Actions** tab. If prompted, click "I understand my workflows,
enable them." You'll see the **polybot-watch** workflow.

### 5. Test it

On the **Actions** tab → polybot-watch → **Run workflow** (manual trigger).
Watch the run; it should finish green. If those wallets have traded recently,
alerts land in Discord. After this, it runs automatically every ~5 min.

## Day-to-day

- **Add/remove wallets:** edit via `run.py track ...` locally and push, OR edit
  `state/tracked_wallets.json` directly on github.com (pencil icon) and commit.
- **See it running:** Actions tab shows every run; `state/last_check.txt` in the
  repo updates each cycle.
- **Pause:** Actions tab → polybot-watch → ⋯ → Disable workflow.

## Notes / limits

- GitHub may delay or skip scheduled runs when its infrastructure is busy —
  occasional gaps are normal. The 90-min freshness window absorbs this.
- The repo will accrue frequent "state: poll update" commits. That's expected
  and harmless.
- If you ever want tighter, guaranteed timing, the VPS path (deploy/VPS.md)
  polls every 3 min on a box you control.
