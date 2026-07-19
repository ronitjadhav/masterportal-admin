# Wiring any Masterportal to masterportal-admin

Step-by-step guide to connect a **stock, unforked** Masterportal (v3.x) to this
backend — for a brand-new portal or an existing one. Nothing in Masterportal's
source is modified; the whole integration is configuration.

## How the integration works

Masterportal loads all of its configuration over HTTP and attaches an OIDC
bearer token to requests matching a regex. This backend exploits exactly
those two seams:

```
Masterportal (static files)          masterportal-admin           upstreams
┌───────────────────────┐   /api/portals/<slug>/*.json   ┌─────┐
│ config.js  ──────────────► per-user filtered configs   │     │   real URLs,
│ (login: OIDC + PKCE)  │                                │ DB  │   credentials
│                       │   /geo/<service-id>/...        └─────┘   only here
│ map requests ────────────► reverse proxy ──────────────────────► WMS/WFS/OAF/…
└───────────────────────┘   (authz + URL hiding + creds injection)
```

**Golden rule: one https origin.** The portal, the backend (`/api`, `/geo`,
`/legends`) and the IdP (`/auth`) must be reachable under the *same* https
origin (dev: the vite proxy; prod: nginx). Browsers block or break everything
else: an https page calling `http://…` is mixed content (the login popup dies
on the token exchange — symptom: blank popup, no login), and cross-origin
setups need CORS + cookie exceptions you simply don't want to manage.

## 1. Import the portal

```bash
python import_portal.py <path-to-portal-folder> <slug>
```

The importer reads the portal's `config.js`, resolves `layerConf` /
`restConf` / `styleConf` (relative paths **or** remote `https://` catalog
URLs — both work, e.g. `portal/master` pulls Hamburg's 6,500-service catalog),
validates referential integrity (unknown layer ids, unknown `styleId`s —
suffixed ids like `8712.1` resolve to service `8712`), and upserts everything
into the DB.

Add the login button + mark services login-only:

```bash
python enable_login.py <slug> [<service-id> ...]
```

## 2. Point the portal's config.js at the backend

Replace the config paths and add the `login` block (all same-origin):

```js
const Config = {
    portalConf: "/api/portals/<slug>/config.json",
    layerConf: "/api/portals/<slug>/services.json",
    restConf: "/api/portals/<slug>/rest-services.json",
    styleConf: "/api/portals/<slug>/style.json",
    login: {
        oidcAuthorizationEndpoint: "https://<origin>/auth/realms/masterportal/protocol/openid-connect/auth",
        oidcTokenEndpoint: "https://<origin>/auth/realms/masterportal/protocol/openid-connect/token",
        oidcRevocationEndpoint: "https://<origin>/auth/realms/masterportal/protocol/openid-connect/revoke",
        oidcClientId: "masterportal",
        oidcScope: "profile email openid",
        oidcRedirectUri: "https://<origin>/portal/<portal-folder>/",
        interceptorUrlRegex: "^(https://<origin>)?/(api|geo|legends)/",
        includeCredentials: false
    },
    // ... keep namedProjections, portalLanguage, etc. as before
};
```

`portal/backend-demo/` in the Masterportal repo is a working example.

## 3. Route one origin to the three services

**Dev (vite):** `devtools/proxyconf.json` in the Masterportal repo (vite
strips the prefix, so repeat it on the target; use `127.0.0.1`, not
`localhost` — Node may try IPv6 first and stall for seconds):

```json
{
  "/api": {"target": "http://127.0.0.1:8000/api"},
  "/geo": {"target": "http://127.0.0.1:8000/geo"},
  "/legends": {"target": "http://127.0.0.1:8000/legends"},
  "/admin": {"target": "http://127.0.0.1:8000/admin"},
  "/auth": {"target": "http://127.0.0.1:8080/auth"}
}
```

**Prod (nginx):** same shape — `location /api|/geo|/legends|/admin → backend`,
`location /auth → keycloak`, everything else → the built portal's static files.
Note: hosting the admin UI behind the *same* origin is fine because it uses an
HttpOnly session cookie and a separate audience; for the strongest isolation
give `/admin` its own hostname (an OIDC `redirect_uri` + `PUBLIC_BASE_URL`
change) so admin and portal never share a document origin at all.

## 4. Identity provider

Any OIDC IdP works (the backend only consumes discovery + JWKS; the frontend
only needs auth-code + PKCE on a public client). The dev Keycloak
(`docker compose up -d`) is pre-configured via `keycloak/realm-masterportal.json`.

Keycloak must present PUBLIC urls even though it's proxied
(`docker-compose.yml` shows the full set): `KC_HTTP_RELATIVE_PATH=/auth`,
`KC_HOSTNAME=https://<origin>/auth`, `KC_PROXY_HEADERS=xforwarded`,
`KC_HTTP_ENABLED=true`. Since Keycloak ≥24, keep `basic` in the client's
`defaultClientScopes`, or access tokens lose the `sub` claim and the backend
rejects them.

Backend env (see `app/settings.py` for defaults):

| Var | Meaning |
|---|---|
| `OIDC_ISSUER` | Public issuer, exactly as in tokens' `iss` (`https://<origin>/auth/realms/masterportal`) |
| `OIDC_DISCOVERY_URL` | Where the *backend* fetches discovery (internal, e.g. `http://keycloak:8080/auth/...`) |
| `OIDC_JWKS_URL` | Internal JWKS URL ("" → use the discovery document's) |
| `OIDC_AUDIENCE` | API audience required in `aud` (default `masterportal-api`). Your IdP must add it to access tokens — the dev realm does this with a Keycloak audience mapper on the `masterportal` client. ID tokens are rejected. |
| `OIDC_ADMIN_CLIENT_ID` | OIDC client for the admin UI (default `masterportal-admin`) — separate from the portal client. |
| `OIDC_ADMIN_AUDIENCE` | Audience `/api/admin` requires (default `masterportal-admin-api`). A portal token can never satisfy it, and vice versa. |
| `OIDC_TOKEN_URL` | Internal token endpoint the backend uses for the admin BFF code/refresh exchange. |
| `PUBLIC_BASE_URL` | The public origin, used to build `/geo` URLs and the admin OIDC `redirect_uri` |
| `PORTAL_ORIGINS` | CORS allow-list (only relevant if you ever go cross-origin) |
| `PROXY_ALLOW_PRIVATE_UPSTREAMS` | `1` allows upstreams on private/loopback networks (intranet GIS). Off by default (SSRF guard); pair with an egress firewall. |
| `UPSTREAM_SECRET_*` | Per-service basic-auth secrets (`user:password`). Only env vars with this prefix can be referenced by a service row. |
| `PROXY_RATE_PER_MIN` | Per-caller `/geo` request budget/minute (default 1200 — a map load fires many tiles). |
| `CONFIG_RATE_PER_MIN` | Per-caller config-serving budget/minute (default 300). |
| `DATABASE_URL` | SQLAlchemy URL (SQLite default; Postgres for prod) |

Rate limiting is per-caller (first `X-Forwarded-For` IP, else socket peer),
sliding-window, returning `429` + `Retry-After`. It is in-process (single
worker); a production nginx should also rate-limit at the edge, and a
multi-worker deployment should move the window to Redis. Upstream load is
additionally bounded by the proxy's global connection cap
(`max_connections=100`) and the request/response size caps; a per-service
concurrency limit is a documented follow-up.

## Portals, catalogs, and the admin console

Two distinct concepts:

- A **catalog** is a shared pool of services/styles/rest-services (imported
  from a `services.json` or built via WMS import). Many portals can draw from
  one catalog.
- A **portal** is one Masterportal instance = its `config.json`
  (`portalConfig` + `layerConfig`), bound to a catalog. Portals are the
  first-class thing you create and edit; multiple portals per deployment is
  supported, which is why the console has a portal switcher.

In the admin console (`/admin/`) each portal is edited across tabs, all
writing the **draft** (publish to go live):

- **Settings** — branding (`mainMenu.title` text/logo/link/toolTip,
  `portalFooter.urls`) and the **map view** (`startingMapMode` 2D/3D,
  `mapView` startCenter / startZoomLevel / extent / epsg / backgroundImage)
  and **controls** (zoom, orientation, fullScreen, totalView, rotation,
  backForward, button3d). Also **create** (＋ New), **clone**, and **delete**.
- **Layer tree** — the `layerConfig` editor (folders, ordering, add/remove).
- **Modules** — enable/disable the menu tools/plugins (about, legend, measure,
  draw, print, …); "needs config" modules render with defaults and are
  fine-tuned later.
- **Styles** — vector styling (`style.json`, catalog-scoped). A structured
  editor covers the common single-symbol case (circle / icon / line / polygon)
  with colour pickers, opacity, sizes, and a live SVG preview; an **Edit as
  JSON** mode exposes the full rule/condition/hatch/cluster/label power so
  nothing is unreachable. Create / duplicate / delete, with a "used by N
  layers" indicator. Layers reference a style by its `styleId`. Edits are
  catalog-wide (shared like services) and follow the same draft → publish flow.
- **Advanced config** — a validated raw-JSON editor for the whole
  `portalConfig`. This is the escape hatch so *nothing* is unreachable: any
  field the structured forms don't cover yet (per-module config,
  `searchBar.searchInterfaces`, `tree` auto-mode, GFI/mouseHover styling, 3D
  params) is editable here. Malformed structure is rejected; a bad save is
  recoverable via Publish/rollback.
- **Services / Import WMS** — the catalog.
- **Access & roles / Publish / Audit** — as before.

`basic` and `master` in `portal/` are Masterportal's own **example** portals;
`import_portal.py` loads them for reference/testing. A real deployment creates
its own portals with **＋ New** (from a minimal starter) — nothing is called
"basic". Note: `import_portal.py` is an authoritative reset (see below).

**Structured forms vs. raw tier:** the Settings/Modules/Tree forms cover the
common fields; everything else (per-module config like print service id /
contact email / routing params, `searchBar.searchInterfaces`, `tree`
auto-mode, GFI/mouseHover styling, 3D scene params) is reachable *today* via
the **Advanced config** tab (validated raw JSON). Structured forms for the
high-traffic modules are a progressive enhancement on top. There is **no
visual theme/skin system** in Masterportal — branding is
title/logo/footer/background only.

## Catalogs and portal scoping

Every import creates (or extends) a **catalog** (third CLI argument, default:
the portal slug). Portals only serve services/styles/rest-services from their
own catalog, and internal service keys are `<catalog>:<external-id>` — so two
catalogs may reuse the same external ids without ever colliding, and proxy
URLs look like `/geo/master:19173`. Give several portals the same catalog
name to share one service set.

## Admin UI (`/admin/`)

Open `https://<origin>/admin/`; it redirects through the IdP (the
`masterportal-admin` client) and lands you back with an **HttpOnly session
cookie** — no admin token is ever exposed to JavaScript, so a portal-side XSS
cannot steal admin credentials or call `/api/admin`. Requires the `admin`
role. Tabs: browse/search services and toggle `is_public` / WFS-T, manage
grants, edit the **layer tree** (folders/ordering/add-remove layers via
drag-drop + a catalog picker), **publish/roll back** portals, import WMS
capabilities, and read the audit log. Everything the UI does is also a plain
JSON API under `/api/admin/*` (Bearer token with the `masterportal-admin-api`
audience) for scripting/CI.

## Editing the layer tree

The **Tree** tab edits a portal's `layerConfig` (the `baselayer` /
`subjectlayer` groups): drag a row onto a folder or group to move it in, ↑↓ to
reorder, rename folders inline, `＋folder` / `＋layer` (catalog picker) to add,
`✕` to remove. **Save draft** writes to the draft only — reload the portal to
preview, then **Publish**. The API is `GET/PUT /api/admin/portals/{slug}/tree`;
PUT rejects malformed structure (422) and returns soft `warnings` for layer
ids not found in the catalog (legitimate for inline GeoJSON/StaticImage
layers, which carry their own definition in the tree).

Note: `python import_portal.py` is an **authoritative reset** from the config
file — it restores services to public and transactions-off, so re-run
`enable_login.py` / `grant.py` afterwards to re-apply access policy.

## Observability

Two complementary trails:
- **Access log** — one JSON line per request on stderr (request id, method,
  path, status, latency ms, claimed actor, client IP). 4xx/5xx are `WARNING`,
  so auth failures (401/403) and rate limits (429) are easy to alert on. Every
  response carries an `X-Request-ID` (echoed if the client sends one) for
  correlation. Ship stderr to your log aggregator as-is (it's already JSON).
- **Audit log** — the `audit_log` table (visible in the console's Audit tab)
  records every *mutation* with the authoritative verified actor. The access
  log's actor is best-effort (unverified token subject) and is for traffic
  analysis, not attribution.

## Config caching

A **published** portal serves its config files with a content-derived `ETag`
and `Cache-Control: private, must-revalidate`: the browser keeps its copy but
revalidates on every load via `If-None-Match`, and an unchanged payload comes
back as `304 Not Modified` with no body — important because `services.json`
can be multiple MB. The ETag hashes the actual per-role filtered response, so
it stays correct even when a live grant change (not a new snapshot version)
alters what a user sees. Responses are never shared-cacheable (`private`), so
role-specific output can't leak between users. The **live draft**
(unpublished) uses `Cache-Control: no-store` — it changes freely, so it is
never cached.

Responses are also **gzip-compressed** for clients that accept it (services.json
shrinks ~16×, e.g. 7.5 MB → 0.47 MB). The config ETags are weak validators, so
gzip and 304 revalidation compose correctly. In production an nginx/CDN edge
with content-type-aware gzip is preferable (it skips already-compressed image
tiles); the app-level gzip keeps compression working when running standalone.

## Draft vs published (staging & rollback)

Admin edits change the **draft** (the live DB rows). Portal users don't see
them until you **publish**: that freezes a full snapshot (portal + layer
config, services with their attrs and `is_public`, styles, rest-services) as
an immutable version and serves it. **Activate** an older version to roll
back, or activate "live draft" to serve edits immediately (dev default when a
portal has never been published). Rollback is safe against later catalog edits
because the snapshot froze the service definitions it needs.

Two things stay **live**, never frozen, so they take effect immediately: role
grants (who may see a layer/module) and the proxy's authz. So publishing
controls *what exists and how it's arranged*; security policy is always
current. A stale snapshot vs live divergence always fails safe (the proxy is
the boundary).

API: `POST /api/admin/portals/{slug}/publish`, `.../activate {"version": N|null}`,
`GET .../snapshots` (lists versions + whether the draft has unpublished changes).

## Roles and grants (deny-by-default)

Roles come from the IdP token (`realm_access.roles` or a flat `roles` claim);
the `admin` role always passes. Manage grants in the admin UI's **Grants**
tab, or with `grant.py`:

```bash
python grant.py service <catalog> <external_id> <role>...   # who may use a secured service
python grant.py module <portal> <module_type> <role>...     # restrict a menu module
python grant.py portal <slug> <role>...                     # restrict a whole portal
```

- A secured service (`is_public=false`) with **no grants** is admin-only.
- Enforcement happens at the proxy (401 anonymous / 403 wrong role); config
  filtering (tree entries, menu modules) is the matching UX so users never
  see what they can't load.
- **UX note:** Masterportal loads its configs once at boot. After logging in,
  the user must reload the page to receive their role-specific tree/modules —
  the login module does not refetch configs by itself.

## Mutations are opt-in

POST requests whose body contains a WFS `<Transaction>`, and any POST to a
sub-path (OGC API Features create/update), return 403 unless the service row
has `allow_transactions = true`. Plain POST `GetFeature` (used by
Masterportal's filter module) is unaffected. Enable transactions only for
WFS-T editing services, and (from Phase 3) only for the roles that may edit.

## What the proxy supports, per service type

| Type | Status | How |
|---|---|---|
| WMS / WMS-T (any version) | ✅ | KVP query forwarded verbatim; GetCapabilities URLs rewritten |
| WFS 1.x/2.x, WFS-T | ✅ | GET + POST (body size-capped); DescribeFeatureType rewritten |
| OGC API Features (OAF) | ✅ | Path suffix forwarded (`/geo/<id>/collections/...`); absolute `links[].href` in JSON rewritten to proxy URLs |
| WMTS (KVP) | ✅ | Same as WMS |
| WMTS (REST / `capabilitiesUrl`) | ⚠️ | `capabilitiesUrl`/`urls` are served unproxied (direct) for now |
| GeoJSON / static files | ✅ | Plain GET through the pinned base |
| SensorThings (STA) | ⚠️ | HTTP queries proxied; **MQTT/WebSocket push is not** — those go direct |
| 3D Tiles / Terrain | ✅ | Path suffix forwarding covers `tileset.json` + tiles |
| Legends (`legendURL` / `legend[]`) | ✅ | Served as `/legends/<id>/<n>`, pinned server-side (legends often live on a *different* upstream — they leak hosts otherwise) |
| Inline layers (defined directly in config.json, not services.json) | ⚠️ | Served as-is — their `url` bypasses the proxy |

Textual responses (XML/JSON/HTML) up to `PROXY_REWRITE_MAX_BYTES` (32 MB
default) get every occurrence of the upstream URL/origin rewritten to the
proxy URL; larger ones stream through unrewritten.

## Security model recap

- Tokens are verified **only** in the backend (signature via JWKS, `iss`,
  `exp`, audience; asymmetric algorithms only). Masterportal's own
  `parseJwt` never checks signatures — do not trust anything client-side.
- `/geo` is not an open proxy: upstream base pinned per registered service,
  sub-paths traversal-checked, methods limited to GET/POST, sizes and
  timeouts capped, redirects not followed.
- Client cookies/Authorization are never forwarded upstream; upstream
  basic-auth is injected from env vars (`upstream_auth_env` stores the env
  var *name*, never the secret).
- `is_public=false` → 401 without a valid token (per-role grants: Phase 3).

## Known dev-mode quirks (not bugs in the integration)

- **Cold vite is slow.** The dev server transforms thousands of modules on
  first load; with the 6,500-service master catalog the first page load can
  take 30–60 s (blank/grey page meanwhile) and may self-reload (Masterportal's
  dev-mode double-mount protection). Production builds don't do this.
- The master portal's `addons` (bildungsatlas, dipas, ...) live in a separate
  repo; without them the related menu entries render empty and log Vue
  warnings. Harmless; remove them from `Config.addons` to silence.
