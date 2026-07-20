# Operations runbook

Day-2 operations for a running `masterportal-admin`: **what to back up, how to
restore, and where to look when something's off**. For first-time setup see the
[README](README.md) ("Run it" / "Deploy") and [INTEGRATION.md](INTEGRATION.md).

## What state exists

Everything the app serves lives in **one database** — there is no other
persistent state to coordinate. Back up the DB and you've backed up the whole
system's configuration.

| Table | Holds | Back up? |
|---|---|---|
| `portals` | each portal's `portalConfig` + `layerConfig`, active snapshot pointer | **yes** |
| `services`, `rest_services`, `styles` | the service catalog + vector styles | **yes** |
| `snapshots` | published config versions (the rollback history) | **yes** |
| `service_roles`, `portal_roles`, `module_roles` | who may see what (RBAC grants) | **yes** |
| `audit_log` | append-only record of admin mutations | **yes** (compliance) |
| `admin_sessions`, `admin_pending_logins` | live BFF login state | no — ephemeral; losing them just re-prompts admins to log in |

Not in the DB, back up separately:

- **Environment / secrets** — `OIDC_*`, `DATABASE_URL`, `PUBLIC_BASE_URL`,
  `PORTAL_ORIGINS`, and any `UPSTREAM_SECRET_*` upstream credentials. Store
  these in your secret manager / `.env` vault, not in the DB (by design — the
  DB only ever names a secret's env var, never its value).
- **The IdP realm** — `keycloak/realm-masterportal.json` is the dev realm;
  production uses your own IdP, backed up by that IdP's own process.
- **TLS certs** — `deploy/certs/` (production certs; the self-signed demo cert
  is regenerated with `deploy/gen-certs.sh`).

The bundled `basic` seed and all code live in Git, so they need no backup.

## Back up

### PostgreSQL (production)

Logical dump — portable across versions and machines. Custom format (`-Fc`)
restores selectively and compresses:

```bash
# Bundled compose Postgres:
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U mpadmin -Fc masterportal_admin > backup-$(date +%F).dump

# External / managed Postgres — pg_dump wants a plain postgresql:// URL, so drop
# the "+psycopg" driver suffix that SQLAlchemy uses in DATABASE_URL:
pg_dump -Fc "${DATABASE_URL/+psycopg/}" > backup-$(date +%F).dump
```

Schedule it (cron/systemd-timer) and ship the dumps off-box. A daily dump plus
your provider's storage snapshots is plenty for this workload — the data is
config, not high-churn transactional records.

### SQLite (dev / small single-node)

The whole DB is one file. Use the online backup API so you can copy a live DB
safely (a plain `cp` of a DB being written can tear):

```bash
sqlite3 admin.db ".backup 'backup-$(date +%F).db'"
```

## Restore

The dump carries the full schema, so a restore stands up a working DB on its
own. On next start the app runs `alembic upgrade head`, which is a no-op if the
dump is current and transparently upgrades it if it came from an older release.

### PostgreSQL

```bash
# into a fresh database (bundled compose):
docker compose -f docker-compose.prod.yml exec -T postgres \
  createdb -U mpadmin masterportal_admin_restored
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore -U mpadmin -d masterportal_admin_restored --clean --if-exists < backup-YYYY-MM-DD.dump
# then point DATABASE_URL at masterportal_admin_restored and restart the backend.

# external / managed:
pg_restore -d "${DATABASE_URL/+psycopg/}" --clean --if-exists < backup-YYYY-MM-DD.dump
```

### SQLite

Stop the backend, drop the file in place, start again:

```bash
cp backup-YYYY-MM-DD.db admin.db
```

### Verify a restore

```bash
curl -sk https://<origin>/healthz                 # {"status":"ok"}
curl -sk https://<origin>/api/portals             # your portals are listed
```

Then load a portal and open `/admin/` → **Audit** to confirm history is intact.

## Config history vs. backups (not the same thing)

Publishing a portal writes a **snapshot**; the admin console's **Publish** tab
rolls a portal back to any earlier published version. That's in-app,
per-portal, and instant — the right tool for "undo a bad config change." It is
**not** a substitute for DB backups: it lives in the same database, so it can't
help you recover from losing that database. Use snapshots for config mistakes,
backups for data loss.

## Disaster recovery (start to finish)

1. Bring up a fresh stack (`docker compose -f docker-compose.prod.yml up -d`, or
   redeploy the image) pointed at an empty DB. It auto-seeds `basic` — that's
   fine, the restore replaces it.
2. Restore the latest dump (above) and point `DATABASE_URL` at it.
3. Restore environment/secrets and TLS certs; confirm the IdP is reachable.
4. Restart the backend and run the verify steps.

## At a glance

- **Health:** `GET /healthz` → `{"status":"ok"}` (use it as the container/LB
  healthcheck).
- **Logs:** one structured JSON line per request on **stderr** (`request_id`,
  method, path, status, `duration_ms`, actor); 4xx/5xx at WARNING.
  `docker compose -f docker-compose.prod.yml logs -f backend`.
- **Metrics:** Prometheus exposition at **`GET /metrics`** (`http_requests_total`
  by method/status, `http_request_duration_seconds` histogram). Unauthenticated
  and deliberately **not** routed through the public nginx origin — scrape it
  from the backend on the internal network / behind your firewall.
- **Scaling:** admin sessions are DB-backed, so the backend runs multi-worker /
  multi-replica safely — add `--workers N` to the `uvicorn` command or scale the
  service. The rate-limit window is per-worker (approximate) until it moves to a
  shared store.
- **Migrations:** applied automatically on startup. For multi-worker rollouts,
  run `alembic upgrade head` once as a deploy step instead of relying on
  per-process startup.
