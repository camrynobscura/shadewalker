# Deploying Shade Walker

The whole deploy story for `shadewalker.nyc`. Three files do the work:

- `Caddyfile` — TLS + reverse proxy + security headers + scrubbed logs
- `shadewalker.service` — the systemd unit that runs uvicorn
- `deploy.sh` — rsync the finished app up + restart

**Model:** one DigitalOcean box (2GB / 1 vCPU / 50GB, NYC, ~$12/mo). The
box runs Python + uv only and **never builds** — the laptop builds the
frontend and the export, and `deploy.sh` ships the artifacts. Rate
limiting is app-level (slowapi), so Caddy is a stock install.

---

## 0. Prerequisites

- Droplet created: Ubuntu 24.04 LTS, 2GB Basic, NYC region, your SSH key.
- DNS (Namecheap): an **A record** `shadewalker.nyc` → the droplet IP, and
  an A record `www` → the same IP (Caddy redirects www → apex). TLS won't
  issue until these resolve.

## 1. One-time box setup

SSH in as root (or a sudo admin user). Then:

```bash
# Service user that owns the app dir and runs uvicorn (no login shell)
sudo adduser --system --group --home /opt/shadewalker shadewalker

# uv — install system-wide so both the service and deploys see it
#   (official installer: https://docs.astral.sh/uv/getting-started/installation/)
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

# Caddy — from its official apt repo (commands: https://caddyserver.com/docs/install)
#   installs a systemd-managed `caddy` service and /etc/caddy/Caddyfile

# Firewall: SSH + web only
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw --force enable

# Harden SSH — a public box gets brute-forced within minutes of going live.
#   In /etc/ssh/sshd_config set (key-only auth, no root login):
#     PasswordAuthentication no
#     PermitRootLogin no
#   then: sudo systemctl restart ssh   (confirm your key still logs you in!)
sudo apt install -y fail2ban              # auto-bans IPs after repeated failures
sudo apt install -y unattended-upgrades  # automatic OS security patches
sudo dpkg-reconfigure -plow unattended-upgrades
```

Put the config in place:

```bash
# App unit
sudo cp /opt/shadewalker/deploy/shadewalker.service /etc/systemd/system/
sudo systemctl daemon-reload

# Caddy config (replaces the default)
sudo cp /opt/shadewalker/deploy/Caddyfile /etc/caddy/Caddyfile
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
```

Let the deploy restart the app without a password (one narrow sudoers
rule for whichever user `deploy.sh` connects as):

```bash
echo '<deploy-user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart shadewalker' \
  | sudo tee /etc/sudoers.d/shadewalker-deploy
```

## 2. First deploy (from the laptop)

```bash
# Build the frontend WITH the CARTO key (it bakes into the bundle)
(cd web && VITE_CARTO_KEY=<key> npm run build)

# Ship code + dist + export, sync the slim venv, restart
HOST=<deploy-user>@<droplet-ip> ./deploy/deploy.sh
```

`deploy.sh` runs `uv sync --no-default-groups` on the box — the 7 runtime
packages only. Then start everything:

```bash
sudo systemctl enable --now shadewalker      # the app
sudo systemctl reload caddy                   # picks up the Caddyfile; TLS auto-issues
```

## 3. Verify (don't skip — the CSP fails silently)

- `curl -sS https://shadewalker.nyc/health` → `{"status":"ok"}`
- Load the site in a browser. **Open the console** and confirm no CSP
  violations — specifically that the **map tiles render** and **address
  autocomplete works** (those are what a wrong CSP would break quietly).
- Run a real route; check the About page loads.
- Headers: `curl -sSI https://shadewalker.nyc | grep -iE 'strict-transport|content-security|referrer|x-content'`
- Rate limiting: hammer `/route` past the limit and confirm a `429`.
- Concurrency: fire two cross-borough routes at once; confirm they queue
  politely (one worker, by design) rather than erroring.

## 4. Monitoring

Two UptimeRobot monitors:
- **Alive:** HTTP(s) on `https://shadewalker.nyc/health`, keyword `ok`.
- **Latency:** a fixed short `/route?...` URL with response-time alerting
  (the live counterpart to the local latency canary).

## 5. Ongoing — monthly refresh → redeploy

Run the refresh ritual on the laptop (REFETCH.md: OSM re-pin + tree bust +
build), then just:

```bash
(cd web && VITE_CARTO_KEY=<key> npm run build)   # only if the frontend changed
HOST=<deploy-user>@<droplet-ip> ./deploy/deploy.sh
```

Data refresh = restart, by design — there is no live reload (see
`server/app.py`'s module docstring).
