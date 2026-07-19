# masterportal-admin

A secure backend and admin console for [Masterportal](https://www.masterportal.org/).
Instead of hand-editing static `config.json` / `services.json` files per portal,
you manage everything through a database and a web console: portals, layers,
the layer tree, branding, modules, and who is allowed to see what — all served
to a **stock, unforked** Masterportal over HTTP.

**What it does**

- **Serves the config dynamically & per-role.** The four config files
  (`config.json`, `services.json`, `rest-services.json`, `style.json`) come
  from the DB, filtered to what the caller is allowed to see.
- **Hides & guards upstream services.** All layer traffic goes through a
  `/geo/<service-key>` reverse proxy: real upstream URLs never reach the
  browser (rewritten in GetCapabilities and legends too), upstream credentials
  are injected server-side, and it's not an open proxy (pinned upstreams, SSRF
  guard, size/timeout caps, mutations opt-in).
- **OIDC + deny-by-default RBAC.** Secured layers need a verified OIDC token
  (JWKS signature, issuer, expiry, dedicated API audience, asymmetric algs)
  **plus** a role grant; `admin` always passes. Works with any OIDC IdP.
- **Admin console at `/admin/`.** Create/configure portals (branding, map view,
  controls, modules), a drag-drop **layer-tree** editor, a **vector-style**
  editor (color pickers + live preview, with a raw-JSON mode for full rule
  power), a **grants** matrix, **WMS capabilities import**,
  **draft → publish → rollback** snapshots, an advanced raw-config editor, and
  an append-only audit log. Admin identity is
  fully separate (own OIDC client + audience) with a BFF login (HttpOnly
  session cookie — no admin token in JS), so a portal-side XSS can't reach it.
- **Production-shaped.** Alembic migrations, SQLite **or** PostgreSQL, ETag +
  gzip on config, per-caller rate limiting, structured JSON access logs.

**Architecture** — one stock Masterportal (static) + this backend + an OIDC IdP,
behind one origin:

```
browser ─┬─ Masterportal (static)      ── / ──────────────┐
         ├─ Admin console (/admin)      ── /admin ─────────┤ nginx / vite proxy
         │                              ── /api  /geo  ────┤   (one https origin)
         └─ OIDC login (PKCE)           ── /auth ──────────┘
                                              │
                              masterportal-admin (this repo) ──► upstream WMS/WFS/OAF…
                                              │                   (real URLs hidden here)
                                          Postgres/SQLite
```

**Docs:** operator/integration guide → [INTEGRATION.md](INTEGRATION.md) ·
design decisions & roadmap → [PLAN.md](PLAN.md).

## Run it

```bash
cd masterportal-admin
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

docker compose up -d          # dev Keycloak with pre-imported realm (see below)

# The schema is managed by Alembic and applied automatically on startup
# (a fresh SQLite admin.db is built from the migrations; existing DBs are
# upgraded in place — no dropping the DB on schema changes). To run it
# manually: `alembic upgrade head`.

# seed a services CATALOG from a Masterportal example (reads its config.js,
# resolves local or remote catalogs — master pulls Hamburg's ~6,500 services)
python import_portal.py ../masterportal/portal/master master

# start the API (runs `alembic upgrade head`, then serves)
uvicorn app.main:app --reload

# end-to-end security checks (25 checks: authz, URL-hiding, live proxy,
# snapshots, rate limits, migrations, gzip, logging…)
python test_e2e.py
```

Then open the **admin console** at <https://localhost:9001/admin/> (or
`http://localhost:8000/admin/` standalone) and use **＋ New** to create a
portal, or edit an imported one. API docs: `<host>/docs`.

## Wire a Masterportal to it

A portal is a **stock** Masterportal whose `config.js` points its
`portalConf` / `layerConf` / `restConf` / `styleConf` at this backend and adds
an OIDC `login` block; portal, backend and IdP must sit behind **one https
origin** (dev: the vite proxy `masterportal/devtools/proxyconf.json`; prod:
nginx). A worked example lives in the Masterportal repo at
`portal/backend-demo/` (its `config.js` points at the `demo` portal). Full
step-by-step for any portal — including creating a fresh one on a new project
— is in **[INTEGRATION.md](INTEGRATION.md)**.

```bash
cd ../masterportal && npm start   # then open https://localhost:9001/portal/backend-demo/
```

The portal renders entirely from the DB and all layer traffic goes through
`/geo`. First dev-mode load is slow (vite cold start, 30–60s) — that's the dev
server, not the backend.

## Login in the browser

With backend + Keycloak up, open the demo portal — the menu has a **Login**
entry (Keycloak `demo` / `demo`, or `admin-demo` / `admin-demo`). Layers marked
login-only show a lock and load only once you're signed in (Masterportal then
attaches the bearer token, which the proxy verifies). In the shipped `demo`
portal, **Krankenhäuser** is such a layer, granted to role `user`. Reload the
page after logging in — Masterportal only reads config at startup.

## Database

The schema is managed by Alembic (`alembic upgrade head` runs automatically on
startup), so switching databases is just a `DATABASE_URL` change — the tables
and any future schema changes are applied for you. Supported: **SQLite**
(default, zero setup) and **PostgreSQL** (recommended for production). Any
other SQLAlchemy-supported engine is best-effort.

**1. SQLite (default)** — nothing to configure; a local `admin.db` is created
and migrated on first start. Great for dev and small single-node use.

**2. Bundled PostgreSQL** — a `postgres` service ships in `docker-compose.yml`.
Bring it up and point the backend at it:

```bash
docker compose up -d postgres
export DATABASE_URL="postgresql+psycopg://mpadmin:mpadmin@localhost:5432/masterportal_admin"
uvicorn app.main:app          # migrations run on startup; then import_portal.py etc.
```

**3. You already have PostgreSQL (managed/RDS/existing cluster) — or similar.**
Do **not** run the bundled `postgres` service. Just point `DATABASE_URL` at
your instance; the app (or a one-off `alembic upgrade head`) creates its schema
in whatever database/schema the URL names:

```bash
export DATABASE_URL="postgresql+psycopg://USER:PASS@your-host:5432/YOUR_DB"
alembic upgrade head          # or just start the app — it does this itself
python import_portal.py ../masterportal/portal/basic default   # seed as usual
```

Notes: the driver is `psycopg` (v3) → use the `postgresql+psycopg://` prefix.
The app owns only its own tables (all prefixed conceptually by this project),
so pointing at a shared database is safe; give it its own schema/database if
you prefer isolation. For a **multi-worker** deployment run `alembic upgrade
head` as a deploy step rather than relying on per-process startup migration.
JSON config is stored in `JSON` columns (portable across SQLite/Postgres).

## OIDC is provider-agnostic

The backend only uses standard OIDC discovery + JWKS, so Keycloak, Authentik,
Zitadel, or a customer's existing IdP all work. Configuration is env vars
(see `app/settings.py` for the full list and defaults):

```bash
OIDC_ISSUER=https://<origin>/auth/realms/masterportal     # public issuer (matches tokens' iss)
OIDC_AUDIENCE=masterportal-api                            # audience the portal API requires
OIDC_ADMIN_AUDIENCE=masterportal-admin-api               # separate audience for /api/admin
PUBLIC_BASE_URL=https://<origin>                          # how browsers reach this app (for /geo URLs)
PORTAL_ORIGINS=https://<origin>                           # CORS allow-list
DATABASE_URL=sqlite:///admin.db                           # or postgresql+psycopg://…
```

See `app/settings.py` for the full list (discovery/JWKS URLs, rate limits,
proxy caps, SSRF toggle). INTEGRATION.md explains each with its purpose.

Masterportal's side is the `login` block in the portal's `config.js` (see
`docs/User/Portal-Config/config.js.md` in the Masterportal repo) — also plain
OIDC with PKCE, so the same provider freedom applies.

The dev realm (`keycloak/realm-masterportal.json`) is imported automatically
by docker compose: realm `masterportal`, public PKCE client `masterportal`,
roles `admin`/`user`, demo users above. **Dev only** — it enables the password
grant for curl-based tests and uses trivial passwords; never reuse it in
production.

## Securing an upstream service

Store only the env var *name* in the DB; the secret stays in the environment:

```bash
# service row: upstream_auth_env = "SVC_123_CREDS"
export SVC_123_CREDS="user:password"   # injected as Basic auth by the proxy
```
