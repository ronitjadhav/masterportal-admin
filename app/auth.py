"""OIDC bearer-token validation against any spec-compliant IdP.

The Masterportal frontend decodes JWTs without checking signatures (its own
code says so) — so this backend is the only place tokens are actually
verified. Verification here is full: signature via the IdP's JWKS, issuer,
expiry, and audience (Keycloak puts the client id in `azp` and often only
"account" in `aud`, so either satisfying `aud` or `azp` is accepted).
"""
import threading

import httpx
import jwt
from fastapi import HTTPException, Request

from .settings import OIDC_AUDIENCE, OIDC_DISCOVERY_URL, OIDC_ISSUER, OIDC_JWKS_URL

_jwks_client: jwt.PyJWKClient | None = None
_lock = threading.Lock()


def _get_jwks_client() -> jwt.PyJWKClient:
    """Lazily resolve the JWKS URI via OIDC discovery; PyJWKClient caches keys.

    Discovery is fetched from OIDC_DISCOVERY_URL (an internal URL when the IdP
    sits behind the public reverse proxy), but the advertised issuer must
    still be exactly OIDC_ISSUER — that is what tokens are validated against.
    """
    global _jwks_client
    with _lock:
        if _jwks_client is None:
            discovery = httpx.get(OIDC_DISCOVERY_URL, timeout=10)
            discovery.raise_for_status()
            meta = discovery.json()
            if meta.get("issuer") != OIDC_ISSUER:
                raise RuntimeError(
                    f"issuer mismatch: discovery says {meta.get('issuer')!r}, "
                    f"OIDC_ISSUER is {OIDC_ISSUER!r}"
                )
            _jwks_client = jwt.PyJWKClient(OIDC_JWKS_URL or meta["jwks_uri"], cache_keys=True)
        return _jwks_client


def validate_token(token: str, audience: str = OIDC_AUDIENCE) -> dict:
    """Return verified claims or raise jwt.InvalidTokenError.

    Strict per RFC 8725: the given audience must be present in `aud`
    (no `azp` fallback — that only proves which client the token was issued
    to, not that it was minted FOR this API), and ID tokens are rejected
    (Keycloak marks access tokens `typ: Bearer`, ID tokens `typ: ID`).
    """
    signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],  # asymmetric only — never accept HS*
        issuer=OIDC_ISSUER,
        audience=audience,
        options={"require": ["exp", "iss", "sub", "aud"]},
    )
    if claims.get("typ") not in (None, "Bearer"):  # None: IdPs without the claim
        raise jwt.InvalidTokenError(f"not an access token (typ={claims.get('typ')})")
    return claims


def roles(claims: dict) -> set[str]:
    """Keycloak realm roles; falls back to a flat `roles` claim (other IdPs)."""
    realm = claims.get("realm_access", {}).get("roles", [])
    flat = claims.get("roles", [])
    return set(realm) | set(flat)


def current_user(request: Request) -> dict | None:
    """FastAPI dependency: verified claims, None if anonymous, 401 if bad token.

    A *present but invalid* token is always a 401 — never downgraded to
    anonymous, so expired sessions surface instead of silently losing layers.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        return validate_token(header[len("Bearer "):])
    except (jwt.PyJWTError, httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(
            401, f"invalid token: {exc}", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
