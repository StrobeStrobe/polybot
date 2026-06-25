# Running Polybot on a VPS (24/7)

A VPS keeps the watcher running constantly, independent of your desktop. Alerts
go to Discord, so you never log into the VPS day-to-day.

## 1. Get a VPS

Any cheap Linux box works — the watcher is tiny. Good options:

- **Hetzner Cloud** (CX22, ~$4/mo) — best value
- **DigitalOcean** ($4–6/mo droplet) — simplest UI
- **Vultr / Linode / AWS Lightsail** — all fine

Pick **Ubuntu 24.04 LTS**. The smallest instance is plenty. Use SSH-key auth.

## 2. Copy the project over

From your machine (where `polybot.zip` is):

```bash
scp polybot.zip <user>@<vps-ip>:~/
```

Then SSH in and unzip:

```bash
ssh <user>@<vps-ip>
sudo apt-get install -y unzip      # if needed
unzip polybot.zip && cd polybot
```

## 3. Add your webhook

```bash
echo 'DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...' > .env
```

## 4. Add wallets to track (optional, can also do later)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # or just run step 5 first
.venv/bin/python run.py track add <wallet-or-profile-url> --label "name"
```

## 5. Install as a service (one command)

```bash
bash deploy/setup-vps.sh
```

This builds the environment, installs a systemd service, and starts it. The
watcher now runs 24/7 and auto-restarts on crash or reboot.

## Day-to-day

```bash
journalctl -u polybot -f                    # watch it live (heartbeat each cycle)
.venv/bin/python run.py track add <wallet>  # add a wallet...
sudo systemctl restart polybot              # ...then restart to pick it up
sudo systemctl stop polybot                 # pause everything
```

`state/last_check.txt` also shows the last poll time. Alerts always land in
Discord regardless.

## Notes

- **Discord-only**: desktop toasts are off and not needed on a headless box.
  Make sure `discord_enabled` is `true` in `config.json`.
- **Security**: the watcher only makes outbound calls (public Polymarket APIs +
  your Discord webhook). No inbound ports. Keep `.env` private; use SSH keys.
- **Resources**: idles near zero CPU/RAM. A refresh of the vetted list every 3
  days is the only heavy moment, and it's still light.
