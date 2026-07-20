"""Reverse proxy for geo services: /geo/<service-key>[/<subpath>]?<params>.

<service-key> is the internal catalog-scoped key ("<catalog>:<external-id>")
— external ids are only unique per catalog, never globally.

Works for any OGC-style upstream, any version, custom endpoints included:
- KVP services (WMS/WMTS/WFS GetMap/GetFeature/GetCapabilities...): query
  params are forwarded verbatim — no per-version parameter list to maintain.
- Path-based services (OGC API Features `/collections/.../items`, 3D
  tilesets `/tileset.json`, SensorThings `/Things`, plain file downloads):
  the request path after /geo/<key>/ is appended to the pinned upstream URL.
- Textual responses (XML capabilities, OAF JSON with absolute `links[].href`,
  DescribeFeatureType, HTML GFI) are buffered and every occurrence of the
  upstream URL/origin is rewritten to the proxy URL. This hides hosts from
  well-behaved clients; the actual security boundary is the 401/403 below,
  never the rewriting.

Security properties:
- NOT an open proxy: the upstream base URL is pinned server-side per
  registered service; the client controls only a sub-path (traversal-checked,
  cannot escape the pinned base) and the query string.
- SSRF guard: upstream hosts resolving to private/loopback/link-local
  addresses are refused unless PROXY_ALLOW_PRIVATE_UPSTREAMS=1 (deliberate
  intranet deployments; pair with an egress firewall — that, not this check,
  is the robust control against DNS rebinding).
- Mutations are opt-in: POST bodies containing a WFS <Transaction> and POSTs
  to sub-paths (OGC API create/update) require allow_transactions on the
  service. Plain POST GetFeature (used by Masterportal's filter) still works.
- Upstream credentials are injected from UPSTREAM_SECRET_* env vars only;
  the client's Authorization header and cookies are never forwarded upstream.
- Redirects are not followed, timeouts and request/response size caps are
  enforced, only http(s) upstreams are accepted, methods limited to GET/POST.
- Non-public services require a verified OIDC token (per-role grants: Phase 3).
"""
import base64
import ipaddress
import os
import re
import socket
import time
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from . import access, configsrc, ratelimit, settings
from .auth import current_user
from .db import Service, ServiceRole, SessionLocal

router = APIRouter()
_rate = ratelimit.limiter(settings.PROXY_RATE_PER_MIN)

# Response headers safe to relay to the browser. Everything else (cookies,
# CORS headers, server banners, hop-by-hop) is dropped. Content-Length is
# deliberately absent: httpx transparently decompresses gzip, so the upstream
# value can be wrong — the ASGI server recomputes it.
RELAY_HEADERS = {"content-type", "content-disposition", "cache-control"}
TEXTUAL_TYPES = ("xml", "json", "text", "html")
TRANSACTION_RE = re.compile(rb"<\s*(\w+:)?Transaction[\s>]", re.IGNORECASE)


def _is_transaction(body: bytes) -> bool:
    """True if a POST body is a WFS-T <Transaction>. Robust to the encoding
    trick a raw-bytes scan misses: UTF-16/UTF-32 interleave ASCII with NUL
    bytes, so the plain regex never matches the tag. We test the raw bytes AND
    a NUL-stripped copy (which collapses UTF-16/32-encoded ASCII back to ASCII),
    so a `charset=utf-16` transaction can't slip past a read-only service."""
    if TRANSACTION_RE.search(body):
        return True
    return b"\x00" in body and bool(TRANSACTION_RE.search(body.replace(b"\x00", b"")))

client = httpx.AsyncClient(
    timeout=httpx.Timeout(settings.PROXY_TIMEOUT_S, connect=10),
    follow_redirects=False,
    limits=httpx.Limits(max_connections=100),
)

# CGNAT / shared address space (RFC 6598): not flagged by ipaddress.is_private,
# but used to front cloud metadata (e.g. Alibaba 100.100.100.200) and carrier
# internal networks — deny it explicitly alongside the standard bad ranges.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

# host → (allowed?, checked_at).  Short TTL so a hostname that flips to a
# private/metadata IP (DNS rebinding) is re-evaluated rather than trusted for
# the process lifetime. Hosts come from the admin catalog, so cardinality is
# small. The robust control against rebinding remains an egress firewall — this
# is name-based and httpx re-resolves at connect time (documented residual).
_HOST_CACHE_TTL = 300
_host_check_cache: dict[str, tuple[bool, float]] = {}


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # unresolvable → refuse
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                or ip in _SHARED_ADDRESS_SPACE
                or (ip.version == 6 and ip.ipv4_mapped is not None
                    and ip.ipv4_mapped in _SHARED_ADDRESS_SPACE)):
            return False
    return bool(infos)


def _assert_allowed_upstream(url: str):
    if settings.PROXY_ALLOW_PRIVATE_UPSTREAMS:
        return
    host = urlsplit(url).hostname or ""
    cached = _host_check_cache.get(host)
    now = time.monotonic()
    if cached is None or now - cached[1] > _HOST_CACHE_TTL:
        cached = (_host_is_public(host), now)
        _host_check_cache[host] = cached
    if not cached[0]:
        raise HTTPException(502, "upstream address not allowed")


def _load_service(service_key: str, user: dict | None) -> Service:
    """Fetch + authorize a service in a SHORT-LIVED session.

    Deliberately not a Depends(db_session): request-scoped sessions stay
    checked out until a StreamingResponse finishes, so slow/hung clients
    would pin DB connections and eventually wedge the whole pool.

    Deny-by-default: secured services need a role grant (or the admin role).
    Anonymous → 401 so the client can log in; wrong role → 403.
    """
    with SessionLocal() as db:
        service = db.get(Service, service_key)
        if service is None:
            raise HTTPException(404, "unknown service")
        if not service.is_public:
            grants = {r.role for r in db.query(ServiceRole)
                      .filter(ServiceRole.service_key == service_key)}
            uroles = access.user_roles(user)
            if not access.service_allowed(False, grants, uroles):
                if uroles is None:
                    raise HTTPException(401, "login required",
                                        headers={"WWW-Authenticate": "Bearer"})
                raise HTTPException(403, "not authorized for this service")
        return service


def _upstream_auth_header(service: Service) -> dict:
    if not service.upstream_auth_env:
        return {}
    if not service.upstream_auth_env.startswith(settings.UPSTREAM_SECRET_PREFIX):
        # A DB row must never be able to exfiltrate arbitrary env vars.
        raise HTTPException(503, "credentials env var must start with "
                                 + settings.UPSTREAM_SECRET_PREFIX)
    secret = os.environ.get(service.upstream_auth_env)
    if not secret:
        # Fail closed: a secured upstream without its secret must not be
        # called unauthenticated.
        raise HTTPException(503, f"credentials env var {service.upstream_auth_env} not set")
    return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}


def _upstream_base(service: Service) -> str:
    url = service.attrs.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise HTTPException(502, "service has no proxyable url")
    return url.rstrip("/")


def _join_subpath(base: str, subpath: str) -> str:
    """Append a client-supplied sub-path to the pinned base, traversal-safe."""
    segments = [s for s in subpath.split("/") if s not in ("", ".")]
    if ".." in segments or "\\" in subpath:
        raise HTTPException(400, "invalid path")
    if not segments:
        return base
    return base + "/" + "/".join(quote(s, safe="~!$&'()*+,;=:@-._") for s in segments)


async def _read_capped_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.PROXY_MAX_REQUEST_BYTES:
            raise HTTPException(413, "request body too large")
    return bytes(body)


async def _capped_stream(upstream: httpx.Response, already: int = 0):
    received = already
    async for chunk in upstream.aiter_bytes():
        received += len(chunk)
        if received > settings.PROXY_MAX_RESPONSE_BYTES:
            await upstream.aclose()
            raise HTTPException(502, "upstream response too large")
        yield chunk


async def _relay(upstream_request: httpx.Request, rewrites: list[tuple[bytes, bytes]] | None = None):
    """Send the request; stream the response back, rewriting textual bodies."""
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unreachable: {type(exc).__name__}") from exc

    relay_headers = {k: v for k, v in upstream.headers.items() if k.lower() in RELAY_HEADERS}
    content_type = upstream.headers.get("content-type", "")

    if rewrites and any(t in content_type for t in TEXTUAL_TYPES):
        chunks, size, overflow = [], 0, False
        async for chunk in upstream.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > settings.PROXY_REWRITE_MAX_BYTES:
                overflow = True
                break
        if not overflow:
            await upstream.aclose()
            body = b"".join(chunks)
            for old, new in rewrites:
                body = body.replace(old, new)
            return Response(body, status_code=upstream.status_code, headers=relay_headers)

        # Textual but huge (big GML dumps): stream unrewritten rather than
        # buffer unbounded. ponytail: streaming rewrite if this ever matters.
        async def passthrough():
            for done in chunks:
                yield done
            async for rest in _capped_stream(upstream, already=size):
                yield rest

        return StreamingResponse(passthrough(), status_code=upstream.status_code,
                                 headers=relay_headers, background=BackgroundTask(upstream.aclose))

    return StreamingResponse(_capped_stream(upstream), status_code=upstream.status_code,
                             headers=relay_headers, background=BackgroundTask(upstream.aclose))


@router.get("/legends/{service_key}/{idx}")
async def legend(
    service_key: str,
    idx: int,
    user: dict | None = Depends(current_user),
    _rl: None = Depends(_rate),
):
    service = await run_in_threadpool(_load_service, service_key, user)
    urls = configsrc.legend_urls_of(service.attrs)
    if not 0 <= idx < len(urls):
        raise HTTPException(404, "no such legend")
    # Target is entirely server-side; the client only picks the index.
    await run_in_threadpool(_assert_allowed_upstream, urls[idx])
    return await _relay(client.build_request(
        "GET", urls[idx], headers={"Accept": "*/*", **_upstream_auth_header(service)},
    ))


@router.api_route("/geo/{service_key}", methods=["GET", "POST"])
@router.api_route("/geo/{service_key}/{subpath:path}", methods=["GET", "POST"])
async def proxy(
    service_key: str,
    request: Request,
    subpath: str = "",
    user: dict | None = Depends(current_user),
    _rl: None = Depends(_rate),
):
    service = await run_in_threadpool(_load_service, service_key, user)
    base = _upstream_base(service)
    await run_in_threadpool(_assert_allowed_upstream, base)
    target = _join_subpath(base, subpath)

    headers = {
        "Accept": request.headers.get("Accept", "*/*"),
        **_upstream_auth_header(service),
    }
    body = None
    if request.method == "POST":
        body = await _read_capped_body(request)
        if not service.allow_transactions and (subpath or _is_transaction(body)):
            # WFS-T <Transaction> or OGC API create/update — mutations are opt-in.
            raise HTTPException(403, "mutating requests are disabled for this service")
        headers["Content-Type"] = request.headers.get("Content-Type", "application/xml")

    proxy_base = f"{settings.PUBLIC_BASE_URL}/geo/{service.key}".encode()
    origin = urlsplit(base)
    rewrites = [(base.encode(), proxy_base)]
    # Scrub any leftover mention of the upstream origin (schemaLocation etc.).
    if f"{origin.scheme}://{origin.netloc}" != base:
        rewrites.append((f"{origin.scheme}://{origin.netloc}".encode(), proxy_base))

    return await _relay(
        client.build_request(request.method, target, params=request.query_params,
                             headers=headers, content=body),
        rewrites=rewrites,
    )
