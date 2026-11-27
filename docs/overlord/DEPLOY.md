# Deploying Overlord (runs with your computer and phone off)

## What you need (one time)
1. **A small Linux server** with a persistent disk: 4 vCPU / 8 GB RAM / 80 GB disk is enough for CPU transcription
   (`small` model ≈ 0.3× real time; `large-v3` ≈ 1×). Any VPS works (Hetzner CX32, DigitalOcean, AWS Lightsail,
   Railway/Fly with a volume). GPU is not needed unless you process many hours per day.
   *Vercel is not suitable*: functions time out and have no persistent disk for multi-hour video work.
2. **Composio API key** (dashboard → API keys). The YouTube channel is already connected in Composio.
3. **Optional Anthropic API key** for semantic scoring and metadata.

## Steps
```bash
git clone https://github.com/officialyashkeshri-ux/ulton && cd ulton/overlord
cp .env.example .env   # fill in OVERLORD_ADMIN_TOKEN, COMPOSIO_API_KEY, COMPOSIO_USER_ID, optional ANTHROPIC_API_KEY
docker compose up -d --build
curl -s localhost:8080/api/health
```
Open `http://SERVER:8080`, enter the admin token, create a client, paste a link. Put the API behind HTTPS (Caddy or
the host's proxy) before exposing it.

## Turning on autonomous publishing (owner decision)
Client settings → publishing policy → `"autonomous": true`, `"max_per_day"`, `"windows"`, `"timezone"`.
Until then every clip waits for Approve on the dashboard.

## Operations
- Health: `GET /api/health`; Docker restarts unhealthy containers.
- Logs: JSON lines on stdout (`docker compose logs -f worker`).
- Audit trail: `GET /api/audit`. Costs: dashboard Overview (render/transcribe seconds per job in `usage`).
- Backups: snapshot the `overlord-data` volume (SQLite DB + media). `sqlite3 /data/overlord.db ".backup x.db"` is safe while running.
