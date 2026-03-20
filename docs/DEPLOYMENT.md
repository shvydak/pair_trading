# Deployment & Infrastructure Guide

Production deployment on Raspberry Pi with automated CI/CD via GitHub Actions.

---

## Infrastructure Overview

| Component           | Specification                       |
| ------------------- | ----------------------------------- |
| **Host**            | Raspberry Pi 5 8GB (ARM64)          |
| **Runtime**         | Python 3.x (.venv)                  |
| **Process Manager** | PM2                                 |
| **CI/CD**           | GitHub Actions (self-hosted runner) |
| **Routing**         | Cloudflare Tunnels                  |

### Production URL

- **App:** `https://pair-trading.shvydak.com` (port 8080) — FastAPI serves both API and frontend

---

## Architecture

FastAPI serves both the backend API and `frontend/index.html` from a single process on port 8080:

```
pair-trading.shvydak.com → localhost:8080 → uvicorn (FastAPI)
                                               ├── GET /          → frontend/index.html
                                               └── GET /api/...   → API handlers
                                               └── WS  /ws/...    → WebSocket handlers
```

One port → one Cloudflare Tunnel (no separate static file server needed).

---

## CI/CD Pipeline

**Trigger:** Push to `main` branch

**Workflow:** `.github/workflows/deploy.yml`

### Key Details

1. **No build step** — Python needs no compilation; `index.html` is static
2. **`clean: false` on checkout** — preserves `.env` file between deployments
3. **`.venv` on server** — created once manually; `pip install` updates it on each deploy

### Pipeline Steps

1. Checkout (preserves `.env`)
2. `.venv/bin/pip install -r backend/requirements.txt`
3. `pm2 reload` (zero-downtime restart)

---

## Environment Variables

Located at: `~/actions-runner/_work/pair_trading/pair_trading/backend/.env`

```env
BINANCE_API_KEY=...
BINANCE_SECRET=...
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_NOTIFY_OPENS=true
TELEGRAM_ALERT_RESET_Z=0.5
```

---

## PM2 Configuration

Defined in `ecosystem.config.js` (project root):

| Process       | Port | Script              |
| ------------- | ---- | ------------------- |
| `pair-trading` | 8080 | uvicorn (FastAPI)   |

`cwd` is set to `./backend` so relative paths (`.env`, SQLite DB) resolve correctly.

---

## Initial Setup on Raspberry Pi (one time)

### 1. Setup GitHub Actions Runner

Go to repository Settings → Actions → Runners → New self-hosted runner, follow Linux ARM64 instructions.

### 2. Create virtualenv and install dependencies

```bash
cd ~/actions-runner/_work/pair_trading/pair_trading
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

### 3. Create `.env` file

```bash
nano backend/.env
# Add production API keys (see Environment Variables above)
```

### 4. Start PM2

```bash
pm2 start ecosystem.config.js
pm2 save
pm2 startup
# Follow the printed command to enable autostart on reboot
```

### 5. Configure Cloudflare Tunnel

In Cloudflare dashboard: `pair-trading.shvydak.com` → `localhost:8080`

---

## Manual Operations

```bash
# Status
pm2 status

# Logs
pm2 logs pair-trading --lines 50

# Restart
pm2 restart pair-trading

# Update dependencies manually
.venv/bin/pip install -r backend/requirements.txt && pm2 restart pair-trading
```

---

## Troubleshooting

### Process keeps crashing

```bash
pm2 logs pair-trading --err --lines 100
# Common causes: missing .env, wrong DB path, missing Python package
```

### `.env` disappears after deployment

**Cause:** `actions/checkout` with `clean: true`
**Fix:** Already configured with `clean: false` in workflow

### GitHub Action stuck

```bash
sudo systemctl status actions.runner.*
sudo systemctl restart actions.runner.*
```

### WebSocket connections dropping

Cloudflare Tunnels support WebSocket natively — no extra config needed.
If issues persist, check `pm2 logs pair-trading` for connection errors.
