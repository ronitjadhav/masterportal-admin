# Security posture

What this backend defends against, how, and what it deliberately leaves to the
operator. Written to be **auditable** — each control points at the code that
implements it. It reflects an adversarial review of the token, proxy, RBAC, and
admin-BFF paths (2026-07-20).

## Trust boundaries

- **The browser is untrusted.** The Masterportal frontend decodes JWTs without
  verifying signatures (by its own admission), so **this backend is the only
  place tokens are verified**. Nothing the client sends — token, `X-Forwarded-For`,
  sub-path, query, body — is trusted for an authorization decision.
- **The database is trusted** and is the whole system's config + secrets-adjacent
  store. It holds admin **refresh tokens** at rest (`admin_sessions`) and the
  *names* of upstream-credential env vars (never their values). Protect it as
  you would any secrets store; back it up (see [OPERATIONS.md](OPERATIONS.md)).
- **Upstream services are semi-trusted.** They're admin-registered, but the
  proxy still guards against a malicious/compromised one (SSRF, size, redirects).
- **One HTTPS origin is mandatory.** Portal, backend, and IdP must share it
  (dev: vite proxy; prod: nginx). This isn't cosmetic — the OIDC login breaks as
  mixed content otherwise, and the admin cookie's `Secure`/`SameSite` depend on it.

## Enforced controls

**OIDC token validation** (`app/auth.py`)
- Signature verified against the IdP's JWKS; algorithms restricted to `RS256`/`ES256`
  — `alg=none` and HS256 key-confusion are rejected.
- `iss` checked against `OIDC_ISSUER`; the discovery document's issuer is
  independently asserted before its `jwks_uri` is trusted (internal-discovery /
  public-issuer split can't be abused).
- `aud` required in the token's `aud` claim — **no `azp` fallback**, so a
  frontend-client token can't satisfy an API audience.
- `exp` enforced; ID tokens rejected (`typ` must be Bearer/absent + resource
  audience). Malformed/expired/wrong-audience tokens fail closed to 401.

**Deny-by-default RBAC** (`app/access.py`, `app/configsrc.py`)
- A secured service with no grants is admin-only; anonymous → 401, wrong role → 403.
- Roles come only from a fully-verified JWT — they can't be injected.
- Filtering is applied to every surface: `services.json`, the `config.json` layer
  tree (including nested folders/groups — a group is kept only if *all* members
  are allowed), and menu modules.
- **Security is always live.** `is_public` and grants are read from the live DB
  even when serving a published snapshot (`configsrc.with_live_access`), so
  securing a layer takes effect immediately — a snapshot never re-exposes it.

**Reverse proxy** (`app/proxy.py`) — the real confidentiality/integrity boundary
- Not an open proxy: the upstream base URL is pinned per service; the client
  controls only a traversal-checked sub-path (can't escape the base) and the query.
- Only `GET`/`POST` are routable; upstream credentials are injected server-side
  and never leak into relayed bodies; client `Authorization`/cookies are never
  forwarded upstream; `Set-Cookie`/CORS/server headers are stripped from responses.
- **Mutations are opt-in** per service (`allow_transactions`): WFS-T `<Transaction>`
  and OGC-API path writes are refused — the check is robust to UTF-16/UTF-32
  encoding tricks, not a naïve ASCII byte-scan.
- **SSRF guard**: upstream hosts resolving to private/loopback/link-local/reserved
  /multicast/CGNAT (`100.64.0.0/10`) addresses are refused unless
  `PROXY_ALLOW_PRIVATE_UPSTREAMS=1`; upstream redirects are not followed.
- Size caps (1 MB request, 100 MB response), timeouts, and a global connection
  cap bound resource use; DB access is short-lived (never pins the pool across a
  streamed response).

**Admin identity isolation + BFF** (`app/admin.py`)
- `/api/admin/*` requires the **separate admin audience AND the `admin` role** on
  every request (bearer or cookie); a portal token 401s here and an admin token
  401s on `/geo`. Isolation is proven both directions in the e2e.
- The admin UI never sees a token: the backend runs the OIDC code+PKCE exchange
  and issues an opaque, `HttpOnly`/`Secure`/`SameSite=Strict` session cookie
  (256-bit id, rotated per login, server-side row deleted on logout).
- The OAuth callback is bound to the initiating browser by a `state` cookie
  (not just the query `state`), closing a login-session-swap CSRF; PKCE is real
  S256; the pending row is one-shot; `redirect_uri` is fixed (no open redirect).
- Cookie-authenticated mutations additionally require a same-origin `Origin`
  header (fail-closed: a missing `Origin` is refused).
- Capabilities import is SSRF-guarded, streamed under a size cap, and parsed with
  `defusedxml` (no XXE, no entity-expansion / billion-laughs).
- Service PATCH is field-restricted (only `is_public`/`allow_transactions`/
  `upstream_auth_env`; the last must carry the `UPSTREAM_SECRET_` prefix) — no
  mass-assignment. Every mutation is written to an append-only audit log whose
  actor comes from the verified token, not the request body.

**Secret handling**
- Upstream credentials live in the environment; the DB only names the env var,
  and that name must start with `UPSTREAM_SECRET_`, so a tampered row can't
  exfiltrate `PATH`/`AWS_*`/etc.

**Availability**
- Per-caller sliding-window rate limits on `/geo` and config endpoints (429 +
  `Retry-After`); the caller table is swept so a rotating `X-Forwarded-For`
  can't grow it unbounded.

## Residual risks (operator's responsibility)

- **DNS rebinding / SSRF TOCTOU.** The SSRF guard checks a resolved name, but
  httpx re-resolves at connect time; the allow-decision is cached for 5 min. The
  robust control is an **egress firewall** on the backend — deploy one,
  especially if you enable `PROXY_ALLOW_PRIVATE_UPSTREAMS`.
- **Protect the database.** It holds admin refresh tokens at rest and all config.
  Use least-privilege DB creds, encryption at rest, and restricted network access.
- **`rest-services.json`, `style.json`, and URLs embedded in `portalConfig`** are
  served verbatim — not proxied, credential-injected, or role-filtered. Put only
  **public** endpoints there; anything secret belongs in a `service` row behind
  the proxy.
- **Host-hiding is best-effort, not a boundary.** Textual responses over the
  rewrite cap (8 MB) stream through unrewritten and may show the upstream host.
  The security boundary is the proxy's 401/403, never URL rewriting.
- **Rate limits are per-worker.** Running multiple workers multiplies the
  effective limit; put nginx/edge rate-limiting in front for a hard global cap.
- **The bundled Keycloak + Postgres are dev-grade.** `start-dev`, trivial
  passwords, and the imported realm are for demos. Production points `OIDC_*` at
  your own IdP and `DATABASE_URL` at managed Postgres, with real TLS certs.

## Reporting

Found something? Open a private security advisory on the repository (or email the
maintainer) rather than a public issue.
