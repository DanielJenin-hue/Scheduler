# Production deploy — Lab Staffing Scheduler (self-serve SaaS)

## Overview

The app is a Streamlit multi-tenant workspace backed by SQLite locally or Postgres in production.
Billing uses Stripe Checkout + webhooks; a separate FastAPI process handles webhook events.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `LAB_SCHEDULER_DB_PATH` | Prod | Path to SQLite file **or** use Postgres migration below |
| `DATABASE_URL` | Postgres | `postgresql://user:pass@host:5432/dbname` (see migration script) |
| `STRIPE_SECRET_KEY` | Live billing | Stripe secret key |
| `STRIPE_PRICE_ID` | Live billing | Recurring price ID ($299 CAD/mo) |
| `STRIPE_WEBHOOK_SECRET` | Webhook service | Signing secret from Stripe dashboard |
| `APP_BASE_URL` | Live billing | Public app URL, e.g. `https://schedule.example.com` |
| `USE_MOCK_STRIPE` | Local dev | Set `1` to keep mock checkout (default when Stripe keys absent) |
| `LAB_ALLOW_DEMO_ACCOUNTS` | **Never in prod** | Set `1` only for local/demo Streamlit; seeds bundled demo logins |
| `LAB_DEMO_NORTHSTAR_PASSWORD` | Dev only | Override northstar demo password when demo accounts enabled |
| `LAB_DEMO_SOUTHBRIDGE_PASSWORD` | Dev only | Override southbridge demo password when demo accounts enabled |
| `LAB_SCHEDULER_ENV` | Prod | Set `production` to block demo account seeding in `scripts/app.py` |
| `LAB_INBOUND_IMAP_HOST` | Inbox sync | e.g. `imap.gmail.com`, `outlook.office365.com` |
| `LAB_INBOUND_IMAP_USER` | Inbox sync | Monitored inbox address for Business → Inbox |
| `LAB_INBOUND_IMAP_PASSWORD` | Inbox sync | App password (never commit) |
| `LAB_INBOUND_IMAP_FOLDER` | Inbox sync | Default `INBOX` |
| `LAB_INBOUND_REPLY_TO` | Outbound | Optional Reply-To for mailto links; defaults to IMAP user |

## Streamlit hosting

1. Deploy `lab_staffing_scheduler` to Streamlit Community Cloud, Railway, or Fly.io.
2. Entry point: `streamlit run scripts/app.py`
3. Mount persistent volume for `LAB_SCHEDULER_DB_PATH` when using SQLite.
4. Set secrets for Stripe and `APP_BASE_URL`.

## Stripe webhook service

```bash
cd lab_staffing_scheduler
pip install -e ".[billing]"
export LAB_SCHEDULER_DB_PATH=/data/demo.sqlite3
export STRIPE_SECRET_KEY=sk_live_...
export STRIPE_WEBHOOK_SECRET=whsec_...
uvicorn scripts.stripe_webhook:app --host 0.0.0.0 --port 8080
```

Register endpoint `https://your-api.example.com/stripe/webhook` for:

- `checkout.session.completed`
- `customer.subscription.deleted` (optional downgrade)

## Postgres migration

For multi-tenant production, migrate off SQLite:

1. Apply `deploy/postgres/001_schema.sql` to a fresh Postgres database.
2. Export SQLite tenants with `scripts/migrate_sqlite_to_postgres.py` (one-time).
3. Point app connections at Postgres via `DATABASE_URL` once the adapter is enabled in your deployment fork.

Until the app uses a Postgres driver natively, run SQLite on a persistent volume for early pilots, then migrate.

## Landing page

Serve `deploy/landing.html` on your marketing domain (or reverse-proxy `/` to static HTML).
Link **Start free trial** to `https://app.example.com/?signup=1`.

## Custom domain & HTTPS

- Streamlit Cloud: configure custom domain in workspace settings.
- Railway/Fly: attach domain + TLS certificate; set `APP_BASE_URL` to the HTTPS origin.

## Security checklist

- Set `LAB_SCHEDULER_ENV=production` and **do not** set `LAB_ALLOW_DEMO_ACCOUNTS` on public hosts.
- Override demo passwords via `LAB_DEMO_*` env vars only in local dev; never commit real secrets.
- Use Stripe live keys only in production secrets.
- Run webhook service on a private URL reachable by Stripe only.
- Enable WAL + persistent disk for SQLite until Postgres cutover.

## Human-only deploy checklist (operator)

These steps cannot be closed by code alone — track in `docs/FINISH_APP_ITERATIONS.md`:

1. **Host** — deploy `scripts/app.py` to Streamlit Cloud / Railway / Fly with persistent `LAB_SCHEDULER_DB_PATH`.
2. **Domain + TLS** — attach custom domain; set `APP_BASE_URL` to the HTTPS origin (required for Stripe return URLs).
3. **Secrets** — Stripe live keys, webhook signing secret, inbound IMAP (`LAB_INBOUND_IMAP_*`) for Business Inbox sync.
4. **Landing** — serve `deploy/landing.html` on marketing domain; point trial CTA to `APP_BASE_URL/?signup=1`.
5. **Smoke** — sign in, Distribute→Fill→Save, RSI gate, Business Gather → Preview → mailto (no auto-send).
6. **First outbound** — human sends 5 Manitoba first-touch mailtos from Email Preview.
