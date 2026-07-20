# Masterportal Admin Backend — Plan

## 0. What the codebase research showed (the seams we build on)

Masterportal v3 (Vue 3, Vite) is driven entirely by 5 config files, all fetchable from **URLs**, not just static files:

| File | Set via | Controls | Loaded at |
|---|---|---|---|
| `config.js` | next to `index.html` | paths to everything else, projections, `login` (OIDC), `proxyHost` | `src/masterportal.js` (script tag) |
| `config.json` | `Config.portalConf` | portal UI (`portalConfig`: menus, modules/plugins) + layer tree (`layerConfig`) | `src/app-store/actions.js` → `loadConfigJson` (plain `axios.get`) |
| `services.json` | `Config.layerConf` | flat catalog of all layers (WMS/WFS/WMTS/OAF/VectorTile/STA/GeoTiff…), keyed by `id` | `loadServicesJson` |
| `rest-services.json` | `Config.restConf` | print, CSW, geocoder, WPS, email services | `loadRestServicesJson` |
| `style_v3.json` | `Config.styleConf` | vector styles keyed by `styleId` | `initializeVectorStyle` |

Critical facts for us:

- **All four JSON configs can point at backend URLs.** Docs even show remote URLs as examples. So the backend can *serve* configs dynamically — no file generation/deployment step required.
- **Login already exists**: `src/modules/login/` implements OIDC Auth Code + PKCE (built for Keycloak). It stores tokens in cookies and patches `axios`/`XHR`/`fetch` to attach `Authorization: Bearer <token>` to any URL matching `Config.login.interceptorUrlRegex`. The docs explicitly say the backend is expected to use this token to "deliver user-specific data (e.g. layers)". The frontend `parseJwt` does **not** validate signatures — the backend must.
- **`isSecured: true`** on a services.json entry makes all layer requests go out with credentials/bearer token. Enforcement is expected server-side (proxy), not in the frontend.
- **There is no role/permission model in Masterportal at all.** Everything is static config. Per-user behavior = serve different config JSON per token. That's exactly the product gap this app fills.
- Masterportal's own proxy support (`useProxy`/`proxyHost`, `src/app-store/js/getProxyUrl.js`) is **deprecated** — we don't use it. Instead we simply put *our* proxy URLs in the `url` fields of the services.json we serve.
- Referential integrity rules to validate: every `layerConfig` id (incl. each id in a `typ:"GROUP"` id-array) must exist in services.json; every `styleId` must exist in style_v3.json; `printServiceId`/`cswId` etc. must exist in rest-services.json.
- No JSON Schemas exist, but `docs/User/**` markdown tables (config.json.md is 7454 lines) are structured enough to derive schemas from. The v2→v3 migrator (`devtools/tasks/migrator/`) is a structural reference.

## 1. Architecture

One backend service + one admin SPA + Keycloak + Postgres. Masterportal itself stays a stock, unforked static app.

```
                    ┌─────────────────────────────────────────────┐
Browser             │                 nginx / traefik             │
  │  ┌──────────┐   │                                             │
  ├──│Masterportal│──┤  /                → Masterportal static     │
  │  └──────────┘   │  /admin           → Admin SPA (static)      │
  │  ┌──────────┐   │  /api/*           → backend (admin CRUD)    │
  ├──│ Admin UI  │──┤  /portals/*/…json → backend (config serving)│
  │  └──────────┘   │  /geo/*           → backend (layer proxy)   │
  │                 │  /auth/*          → Keycloak                │
  │                 └───────────────┬─────────────────────────────┘
  └── OIDC (PKCE) ──► Keycloak      │
                                    ▼
                          backend ──► Postgres
                             │
                             └──► upstream WMS/WFS/… (real URLs, hidden)
```

**Identity: Keycloak, not custom auth.** Masterportal's login module is literally built for it, and building our own IdP would be the single biggest security mistake available. Both Masterportal and the admin UI are public OIDC clients (PKCE); the backend validates JWTs (signature via JWKS, `iss`, `aud`, `exp`) on every request. Roles live in Keycloak (realm/client roles), permissions mapping lives in our DB.

**Stack recommendation:** Python **FastAPI + SQLAlchemy + Postgres** (async httpx for the proxy), admin UI in **Vue 3** (same ecosystem as Masterportal, skills transfer). Pydantic models double as the JSON validation layer for configs. Alternative: NestJS if the team is TS-first — everything else in this plan is stack-agnostic.

## 2. Data model (Postgres)

- `service` — the layer catalog. Columns: `id`, `slug`, `typ` (WMS/WFS/WMTS/OAF/VectorTile/STA/GeoTiff/GeoJSON), `name`, **`internal_url`** (real upstream, never leaves the server), `upstream_auth` (none | basic | header; secret ref), `attrs JSONB` (the full services.json entry: `layers`, `featureType`, `version`, `gfiAttributes`, …), `is_public`.
- `style` — `style_id`, `rules JSONB` (a style_v3.json entry).
- `rest_service` — rest-services.json entries (`attrs JSONB`).
- `portal` — one Masterportal instance: `slug`, `title`, `portal_config JSONB` (the `portalConfig` half of config.json: menus, controls, module list), `config_js_overrides JSONB` (projections, language…).
- `tree_node` — the layer tree per portal: `portal_id`, `parent_id`, `position`, `kind` (folder | layer | group), `service_id` / `service_ids[]` (for GROUP), `overrides JSONB` (visibility, name, styleId…), `category` (baselayer | subjectlayer). This is the editable form of `layerConfig`.
- `role` — mirrors Keycloak role names (synced or just referenced by name).
- Permission tables (plain join tables, allow-list semantics):
  - `role_service` — which roles may see/use a layer (absent + `is_public=false` → filtered out AND proxy returns 403).
  - `role_portal` — which roles may open a portal at all.
  - `role_module` — which portalConfig modules/plugins (print, draw, wfst, measure…) each role gets.
- `audit_log` — who changed what, when, old/new value (JSONB diff). Append-only.
- `config_snapshot` — versioned published configs per portal (rollback = re-point to old snapshot).

Users are **not** stored locally (Keycloak owns them); we only store role→resource mappings.

## 3. Backend surface

### 3a. Config delivery (consumed by Masterportal)

The portal's `config.js` (generated once per portal by us) points at:

```js
portalConf: "/api/portals/<slug>/config.json",
layerConf:  "/api/portals/<slug>/services.json",
restConf:   "/api/portals/<slug>/rest-services.json",
styleConf:  "/api/portals/<slug>/style.json",
login: { oidc… , interceptorUrlRegex: "^/(api|geo)/" }
```

Each endpoint:
1. Validates the bearer token if present (anonymous allowed → public subset only).
2. Resolves the user's roles.
3. Builds the JSON from DB, **filtered**: only permitted layers in `layerConfig` + services.json, only permitted modules in `portalConfig`, secured layers get `isSecured: true`.
4. All service `url` fields are rewritten to `/geo/<service-slug>` — **upstream URLs never appear in any response**.
5. `Cache-Control: private, no-store` (responses are per-user); ETag on the published snapshot for cheap revalidation.

### 3b. Reverse proxy `/geo/<service-slug>/*`

- Looks up the service; if not public, requires valid JWT + `role_service` grant → else 403.
- Forwards to `internal_url`, injecting upstream credentials server-side (from env/secret store — the browser never sees them).
- Allow-list of forwarded query params/methods per service type (GET for WMS/WMTS; GET/POST with body-size limit for WFS/WFS-T).
- Rewrites URLs inside GetCapabilities responses so upstream hosts don't leak there either.
- Streams responses; per-user rate limit; request/response size caps; timeouts.
- Sets correct CORS: `Access-Control-Allow-Origin: <portal origin>` + `Allow-Credentials: true` (exactly what `docs/User/Global-Config/services.json.md` demands for secured services).

### 3c. Admin API `/api/admin/*` (requires `admin` or `editor` role)

- CRUD: services, styles, rest-services, portals, tree (move/nest/reorder), role mappings.
- **GetCapabilities import**: paste a WMS/WFS/WMTS URL → backend fetches capabilities, lists layers, prefills `service.attrs` (biggest day-to-day time-saver; the field soup in services.json is exactly what admins get wrong by hand).
- **Validate + publish**: validation endpoint enforces the referential-integrity rules above via Pydantic schemas derived from the docs; publishing writes a `config_snapshot`. Draft vs published separation so editing never breaks a live portal.
- Preview: "open portal as role X" (backend mints nothing — just a query the admin UI uses to render the filtered config diff).
- Audit log read endpoint.

### 3d. Admin UI (Vue SPA)

Pages: Portals list · Layer catalog (with capabilities-import wizard) · Tree editor (drag-drop folders/layers/groups per portal) · Module/plugin toggles per portal per role · Role permissions matrix · Audit log · Publish/rollback. Login via Keycloak redirect.

## 4. Security design (non-negotiables)

- **AuthN**: OIDC only, PKCE public clients, short-lived access tokens + refresh. Backend validates signature against Keycloak JWKS on every request — never trusts the frontend's unvalidated `parseJwt`.
- **AuthZ**: deny-by-default allow-lists (`role_*` tables); enforced at *both* config-serving and proxy layers (filtering the tree is UX, the proxy check is the actual control).
- **Secrets**: upstream service credentials only in env vars / Docker secrets / Vault, encrypted at rest if in DB; never serialized into any config response.
- **Proxy hardening**: strict URL allow-list (only registered services — this is *not* an open proxy), param allow-lists, no redirects followed cross-host, SSRF guard (upstream hosts pinned at registration, deny link-local/metadata IPs), body/response caps, timeouts.
- **Transport & headers**: TLS everywhere (terminate at nginx), HSTS, CSP on admin UI, `SameSite` cookies, CSRF not applicable to bearer-token API but enabled if any cookie auth is added.
- **Input validation**: every config JSONB validated against Pydantic schemas before persist *and* before serve.
- **Audit**: append-only log of all mutations with actor (`sub` claim), IP, diff.
- **Ops**: rate limiting (per-IP anonymous, per-sub authenticated), structured logs, `/healthz`, dependency scanning + lockfiles in CI, DB user with least privilege, no default creds anywhere.
- **Isolation**: admin API and config/proxy endpoints can be split into two deployments of the same image later if exposure profiles must differ — don't do it on day one.

## 5. Deployment

`docker-compose.yml` (dev = prod shape): `nginx` (Masterportal static build + admin SPA + routing), `backend`, `postgres`, `keycloak` (+ its own PG or shared). Prod: same images under compose or k8s; Keycloak realm exported as JSON for reproducible setup; DB migrations via Alembic; backups = pg_dump + snapshots table already gives config history.

Masterportal deployment stays stock: `npm run build`, drop the portal folder in nginx, its `config.js` points at our endpoints. **No fork, no addon needed** — the existing login module + URL-based config loading is the whole integration.

## 6. Phased roadmap (revised after the 2026-07-18 security audit)

**Done — Phase 1+2 (+ audit fixes):** configs served from DB with **catalog
scoping** (portals share or own catalogs; internal keys are
`<catalog>:<external-id>`, so imports can never collide and portals only see
their own services); generic `/geo` proxy (any OGC type, path suffixes,
best-effort URL rewriting); strict OIDC validation (JWKS signature, issuer,
expiry, **dedicated `masterportal-api` audience** via Keycloak audience
mapper, ID tokens rejected per RFC 8725); SSRF guard (private/loopback/
link-local upstreams refused unless `PROXY_ALLOW_PRIVATE_UPSTREAMS=1` —
intranet GIS is a legit use; pair with an egress firewall, which is the real
control against DNS rebinding); **mutations opt-in** (WFS-T `<Transaction>`
and OGC-API writes 403 unless `allow_transactions`); secret env vars
restricted to the `UPSTREAM_SECRET_` prefix; capped importer fetches; pinned
requirements; `/healthz`; `no-store` on config responses. Covered by the
12-check `test_e2e.py`.

**Done — Phase 3 (permissions):** grant tables `service_roles`/`portal_roles`/
`module_roles`, decision + filtering logic in `app/access.py`, deny-by-default
enforcement at *both* config serving (role-filtered trees, menus, services.json)
and the proxy (401 anon / 403 wrong role; `admin` always passes). `grant.py`
CLI. Two users demonstrably see different trees and plugin sets, and the proxy
403s per role.

**Done — Phase 4 core (admin API + UI):** identity fully separated — `/api/admin`
validates the `masterportal-admin-api` audience AND the `admin` role; a portal
token is rejected there and an admin token is rejected on `/geo` (both proven
in `test_e2e.py`). BFF login: the backend runs the OIDC code/refresh exchange
and issues an HttpOnly/Secure/SameSite=Strict session cookie, so no admin token
ever reaches JavaScript (verified in-browser: `document.cookie` cannot see the
session, and the admin origin can't pull a secured portal layer). Single-file
admin UI at `/admin/` (`app/static/admin.html`): service browse/search + toggle
`is_public`/WFS-T, grants management, WMS capabilities import (SSRF/size/entity
guarded, dry-run default), audit log. Every mutation is written to an
append-only `audit_log`.

**Done — draft/publish snapshots.** `portal_config`/`layer_config` + the
catalog rows are the editable **draft**; **publish** freezes a full
render-input (portal/layer config, services with attrs+is_public, styles,
rest-services) into an immutable versioned `Snapshot` and points the portal
at it; **activate** rolls to any version or back to the live draft. Serving
reads the active snapshot (or live draft if none) through one shared
materialize+filter path (`app/configsrc.py`), with role filtering and the
proxy's authz always applied LIVE on top — so a rollback survives later
catalog edits (proven: a live edit doesn't touch the active snapshot;
`draft_dirty` flags divergence), while security changes take effect
immediately. Publish/rollback are in the admin UI's Publish tab.

**Done — tree editor.** `GET/PUT /api/admin/portals/{slug}/tree` edits the
draft `layerConfig`: PUT validates structure strictly (top-level groups,
folder names, layer ids → 422 on malformed), warns softly on ids absent from
the catalog (inline GeoJSON/StaticImage layers stay valid), strips editor
`_uid`s, and audits. The admin UI's **Tree** tab renders both groups with
resolved layer names, native drag-drop to move rows into folders/groups (a
uid-based model with a descendant guard so a folder can't be dropped into
itself), ↑↓ reorder, inline folder rename, add-folder, delete, and an inline
catalog **picker** to add layers. Edits save to the draft → reload to preview
→ Publish to go live. This completes the original "add/remove layers and
layer groups" vision.

**Done — vector-style editor.** `style.json` is now managed in the console
(catalog-scoped): list with live SVG swatches + usage, a structured editor for
the common single-symbol styles (circle/icon/line/polygon — colour pickers,
opacity, sizes, live preview), an advanced raw-JSON mode for the full
rule/condition/hatch/cluster/label power, and create/duplicate/delete.
`GET/POST/PUT/DELETE /api/admin/styles`; `styleId` immutable; edits are
draft → publish like everything else.

**Phase 4 is complete.** Every config surface — portalConfig (settings +
modules + advanced), layerConfig (tree), and style.json (styles) — is now
editable in the console. What remains is Phase 5 ops-hardening.

**Phase 5 — Ops (in progress):**
- **Done — rate limiting.** Per-caller sliding-window limiter
  (`app/ratelimit.py`) on `/geo` (1200/min) and config serving (300/min),
  `429` + `Retry-After`. In-process (single worker); edge nginx + Redis are
  the multi-worker path. Per-upstream concurrency **deferred** (correct
  release across streaming responses is error-prone; the global httpx
  `max_connections=100` + size caps already bound upstream load).
- **Done — Alembic migrations.** Schema is now migration-managed
  (`alembic/`, two revisions: initial + add `portals.description`);
  `init_db()` runs `alembic upgrade head` on startup (batch mode → SQLite
  ALTER support), so fresh DBs build from migrations and existing data is
  upgraded in place — the destructive `rm admin.db` reseed is gone. Proven on
  the real data DB (2 portals + 6558 services survived the column-add) and
  idempotent. Multi-worker deploys should run the upgrade as a separate step.
- **Done — PostgreSQL.** `psycopg` driver added; a `postgres` service ships in
  compose. The **full 25-check e2e suite passes against Postgres** (parity with
  SQLite), migrations apply cleanly (portable `sa.JSON`/`String`/`Integer`).
  Three documented paths (README "Database"): SQLite default (zero setup),
  bundled Postgres, and **external/managed Postgres** (just set `DATABASE_URL`
  + `alembic upgrade head`). The running demo stays on SQLite for a
  frictionless dev loop; Postgres is one env var away.
- **Done — config caching.** Published portals serve config files with a
  content-derived `ETag` + `private, must-revalidate` → `304` on unchanged
  payloads (big win: services.json is multi-MB). ETag = hash of the actual
  role-filtered body, so it's correct under live grant changes too; never
  shared-cacheable. Live draft keeps `no-store`. This retired the
  no-store/ETag tension cleanly (gate on published vs draft).
- **Done — gzip compression.** `GZipMiddleware` (minimum_size 1024) — the
  framework's streaming-safe impl. services.json ~16× smaller (7.5 MB → 0.47
  MB). Weak config ETags compose correctly with it. Production edge (nginx/CDN)
  content-type-aware gzip is preferable; app-level keeps it working standalone.
- **Done — structured access logging.** Pure-ASGI middleware
  (`app/logging_setup.py`) emits one JSON line per request to stderr
  (request id, method, path, status, latency, claimed actor, client);
  4xx/5xx at WARNING so auth failures/429s stand out. `X-Request-ID` echoed
  or generated for client correlation. Writes JSON directly (not via
  `logging`) because uvicorn's `disable_existing_loggers` kept silencing a
  named logger. Doesn't buffer streamed proxy responses. The DB `AuditLog`
  remains the authoritative actor trail; this is the traffic/latency stream.
- **Deployment (done 2026-07-20):** `Dockerfile` (backend, single worker —
  migrations + `basic` auto-seed on boot) and `docker-compose.prod.yml` bring
  up the whole stack **behind one https origin** via nginx (`deploy/nginx.conf`):
  portal static (mounted at `deploy/portal/`), admin console, config API + `/geo`
  proxy, and bundled Keycloak + Postgres. `deploy/gen-certs.sh` makes a
  self-signed cert for the demo. Verified end-to-end: fresh Postgres volume →
  `basic` auto-seeded, all routes reachable via `https://localhost:9001`. Going
  to real production changes only configuration (own IdP, managed Postgres, real
  certs, origin env) — not code.
- **DB-backed admin sessions (done 2026-07-20):** `_sessions`/`_pending_logins`
  moved from process dicts to the `admin_sessions` / `admin_pending_logins`
  tables (migration `a1b2c3d4e5f6`). Admins now stay logged in across restarts,
  and the app is safe to run with multiple workers/replicas. Chose the existing
  DB over adding Redis (YAGNI — a handful of admin sessions is trivial; note the
  refresh tokens are now at rest in the DB, which must be protected regardless).
  `test_sessions.py` proves persistence + expiry-cleanup + GC without Keycloak;
  wired into CI. Only in-process state left: the rate-limit window (per-worker,
  approximate — fine until it needs a shared store under heavy multi-worker use).
- **Next:** metrics, backup/restore runbook, pen-test checklist.

## 8. Portal model & full config coverage (revised 2026-07-19)

Reframe after inventorying the config surface (see the two research reports).
Driver: `basic`/`master` were only Masterportal *example* fixtures; shipping
them as the product is confusing, and the admin should **create and fully
configure** portals, not toggle pre-baked ones.

### Concepts (now explicit)
- **Catalog** — a shared pool of services/styles/rest-services (from a
  `services.json`). Many portals can draw from one catalog.
- **Portal** — one Masterportal instance = its `config.json`
  (`portalConfig` + `layerConfig`), bound to a catalog. This is the
  first-class thing admins create and edit. Multiple portals per deployment is
  a real Masterportal capability, so the portal selector stays — but gains
  create / clone / rename / delete.
- **No "theme" system.** Masterportal's "Themenconfig" == the layer tree
  (already editable). Branding is only `mainMenu.title` (text/logo/link),
  `portalFooter.urls`, and `mapView.backgroundImage`; there is no runtime
  skin/color engine. "Create a new theme" therefore means "create a new
  portal."

### Seeding (revised — shipped)
Ship **one clean starting-point portal, `basic`**, bundled self-contained in
`seed/basic/` (config + services + styles, ~80 KB, adapted from the Masterportal
example) and **auto-seeded on an empty DB** (`app/seed.py`, idempotent) so a
fresh clone works with zero setup and no dependency on the Masterportal repo. All
layers start public; the operator secures/grows from there or creates new portals
with **＋ New** and can delete `basic`. `master` (and other examples) remain
*optional* imports via `import_portal.py`. The e2e suite self-seeds `basic` from
the same bundle, so tests are green from a clean checkout.

### Config coverage — what the admin exposes
`portalConfig` is large (full field inventory in the research reports). We
cover it in tiers, structured forms for the common 90% and a raw-JSON escape
hatch for the long tail (honest + extensible):

- **Branding** (done this phase): `mainMenu.title` text/logo/link/toolTip,
  `portalFooter.urls`.
- **Map** (done this phase): `startingMapMode` (2D/3D), `mapView`
  (startCenter, startZoomLevel, extent, epsg, backgroundImage),
  `controls` (toggle zoom / orientation / fullScreen / totalView /
  rotation / backForward / button3d).
- **Modules / plugins** (done this phase): enable/disable the common menu
  modules per menu (about, legend, contact, measure, draw, coordToolkit,
  scaleSwitcher, fileImport, openConfig, selectFeatures, shareView, language,
  news, shadow, styleVT, compareMaps, layerClusterToggler, login). Reorder and
  per-module config are the next tier.
- **Layer tree** (already done): `layerConfig` editor.
- **Later tiers**: per-module config (print/contact/routing/wfsSearch/filter
  need service ids + params), `searchBar.searchInterfaces` config,
  `tree` auto-mode knobs, GFI/mouseHover styling, 3D params. Each is a
  structured form added incrementally; until then a **raw-JSON advanced
  editor** (validated on save) exposes everything.

### UX principles (dashboard)
- Portal is chosen once (header switcher) and every view acts on it; the
  switcher offers **＋ New portal** and per-portal manage (rename/clone/delete).
- Structured forms with sensible defaults for common fields; an "Advanced
  (raw JSON)" panel for the uncovered long tail so nothing is unreachable.
- Everything edits the **draft**; Publish/rollback already govern going live.
- Every field/section documented in INTEGRATION.md with its config.json path.

## 6a. Explicit non-goals / accepted trade-offs (audit-reviewed)

- **URL-hiding is a feature, not a security boundary.** Byte-rewriting of
  textual responses hides hosts from well-behaved clients; the enforced
  boundary is the proxy's 401/403. WMTS `capabilitiesUrl`, STA MQTT push and
  >8 MB textual bodies stay direct/unrewritten until a concrete need arises.
- **Import warnings don't fail imports.** Real catalogs (Hamburg's included)
  ship broken references; a `--strict` flag can come with the admin API.
- **The dev docker-compose is dev-only** (start-dev, password grant, trivial
  credentials) and clearly labeled; the prod compose is separate Phase 5 work.

## 7. Deliberately skipped (add when actually needed)

- Visual style editor (serve/validate style_v3.json as raw JSON with schema check first).
- Multi-tenancy beyond multiple portals, config i18n editing, 3D/oblique specifics.
- Custom login addon for Masterportal — the built-in module suffices.
- Kubernetes/Helm before compose stops being enough.
- Syncing Keycloak users into our DB — role names are the only contract.
