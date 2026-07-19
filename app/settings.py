"""All runtime configuration, from environment variables only (no secrets in code/DB)."""
import os

# OIDC provider — any spec-compliant IdP (Keycloak, Authentik, Zitadel, ...).
# OIDC_ISSUER is the PUBLIC issuer exactly as it appears in tokens' `iss`.
# Behind a reverse proxy the backend often can't (or shouldn't) reach the
# public URL, so discovery/JWKS may be fetched from internal URLs instead.
OIDC_ISSUER = os.environ.get(
    "OIDC_ISSUER", "https://localhost:9001/auth/realms/masterportal")
OIDC_DISCOVERY_URL = os.environ.get(
    "OIDC_DISCOVERY_URL", "http://localhost:8080/auth/realms/masterportal/.well-known/openid-configuration")
OIDC_JWKS_URL = os.environ.get(  # set "" to use the discovery document's jwks_uri
    "OIDC_JWKS_URL", "http://localhost:8080/auth/realms/masterportal/protocol/openid-connect/certs")
# Audience this API requires in access tokens' `aud` (RFC 8725). Keycloak adds
# it via the audience mapper in the realm import — it is the API's identity,
# NOT the frontend client id.
OIDC_AUDIENCE = os.environ.get("OIDC_AUDIENCE", "masterportal-api")

# Admin identity is fully separate: its own OIDC client and its own audience.
# A portal token is never valid on /api/admin, and vice versa.
OIDC_ADMIN_CLIENT_ID = os.environ.get("OIDC_ADMIN_CLIENT_ID", "masterportal-admin")
OIDC_ADMIN_AUDIENCE = os.environ.get("OIDC_ADMIN_AUDIENCE", "masterportal-admin-api")
# Token endpoint the BACKEND uses for the admin BFF code exchange (internal).
OIDC_TOKEN_URL = os.environ.get(
    "OIDC_TOKEN_URL", "http://localhost:8080/auth/realms/masterportal/protocol/openid-connect/token")

# Where browsers reach this backend (used to build proxy URLs in services.json).
# Same origin as the portal — dev: the vite proxy, prod: nginx.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://localhost:9001").rstrip("/")

# Origins allowed to call the API (the portal(s) and the future admin UI).
PORTAL_ORIGINS = os.environ.get(
    "PORTAL_ORIGINS", "https://localhost:9001,http://localhost:9001"
).split(",")

# Proxy limits.
PROXY_TIMEOUT_S = float(os.environ.get("PROXY_TIMEOUT_S", "30"))
PROXY_MAX_REQUEST_BYTES = int(os.environ.get("PROXY_MAX_REQUEST_BYTES", str(1 * 2**20)))
PROXY_MAX_RESPONSE_BYTES = int(os.environ.get("PROXY_MAX_RESPONSE_BYTES", str(100 * 2**20)))
# Textual responses up to this size are buffered so upstream URLs can be
# rewritten to proxy URLs; larger ones stream through unrewritten. Clamped to
# the response cap so misconfiguration can't bypass it.
PROXY_REWRITE_MAX_BYTES = min(
    int(os.environ.get("PROXY_REWRITE_MAX_BYTES", str(8 * 2**20))),
    PROXY_MAX_RESPONSE_BYTES,
)

# SSRF guard: upstream hosts resolving to private/loopback/link-local/metadata
# addresses are refused unless explicitly allowed (intranet GIS servers are a
# legitimate use of this proxy — enable deliberately, ideally combined with an
# egress firewall).
PROXY_ALLOW_PRIVATE_UPSTREAMS = os.environ.get("PROXY_ALLOW_PRIVATE_UPSTREAMS", "") == "1"

# upstream_auth_env values must carry this prefix, so a compromised DB row
# can only ever name dedicated secret variables — never PATH, AWS_*, etc.
UPSTREAM_SECRET_PREFIX = "UPSTREAM_SECRET_"

# Per-caller rate limits (requests/minute). Proxy is generous — one map load
# fires many tile/feature requests; config serving is lower.
PROXY_RATE_PER_MIN = int(os.environ.get("PROXY_RATE_PER_MIN", "1200"))
CONFIG_RATE_PER_MIN = int(os.environ.get("CONFIG_RATE_PER_MIN", "300"))
