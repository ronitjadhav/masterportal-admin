"""Masterportal admin backend — serves the four config files + the /geo proxy.

Point a portal's config.js at these endpoints:
    portalConf: "<PUBLIC_BASE_URL>/api/portals/<slug>/config.json"
    layerConf:  "<PUBLIC_BASE_URL>/api/portals/<slug>/services.json"
    restConf:   "<PUBLIC_BASE_URL>/api/portals/<slug>/rest-services.json"
    styleConf:  "<PUBLIC_BASE_URL>/api/portals/<slug>/style.json"
"""
import hashlib
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session

from . import (access, admin, configsrc, logging_setup, proxy, ratelimit, seed,
               settings)
from .metrics import metrics
from .auth import current_user
from .db import Portal, PortalRole, db_session, init_db

_config_rate = ratelimit.limiter(settings.CONFIG_RATE_PER_MIN)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()          # runs Alembic migrations
    seed.seed_if_empty()   # first-run: bundled 'basic' portal on an empty DB
    yield
    await proxy.client.aclose()


app = FastAPI(title="masterportal-admin", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.PORTAL_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
# Compress large text payloads (services.json is multi-MB) for clients that
# accept it; adds `Vary: Accept-Encoding`. Composes with the weak config ETags
# (weak validators are encoding-independent per RFC 7232) — 304s carry no body
# so aren't compressed. ponytail: in production an nginx/CDN edge with
# content-type-aware gzip is preferable (skips already-compressed image tiles);
# this app-level gzip keeps compression working standalone.
app.add_middleware(GZipMiddleware, minimum_size=1024)
# Outermost (added last): structured access log wrapping CORS/GZip so it sees
# the final status and can stamp X-Request-ID on the response.
app.add_middleware(logging_setup.AccessLogMiddleware)

app.include_router(proxy.router)
app.include_router(admin.router)

def serve_config(data, request: Request, portal: Portal) -> Response:
    """Serve a role-filtered config payload with correct caching.

    Published portal → a content-derived ETag + `private, must-revalidate`:
    the browser keeps its copy but revalidates every load, and an unchanged
    payload returns 304 (no body) — the win, since services.json can be
    multi-MB. The ETag is a hash of the actual filtered response, so it is
    correct even when a live grant change (not a new snapshot) alters output.
    Never shared-cacheable (`private`), so per-role responses can't leak.

    Live draft (unpublished) → `no-store`: the draft changes freely; don't
    cache it at all.
    """
    if portal.active_snapshot_id is None:
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    etag = f'W/"{digest[:32]}"'
    headers = {"ETag": etag, "Cache-Control": "private, must-revalidate"}
    inm = [t.strip() for t in request.headers.get("if-none-match", "").split(",")]
    if etag in inm:
        return Response(status_code=304, headers=headers)
    return JSONResponse(data, headers=headers)


def get_portal(slug: str, db: Session, uroles: set[str] | None) -> Portal:
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, f"unknown portal: {slug}")
    required = {r.role for r in db.query(PortalRole).filter(PortalRole.portal_slug == slug)}
    if not access.roles_satisfy(required, uroles):
        if uroles is None:
            raise HTTPException(401, "login required", headers={"WWW-Authenticate": "Bearer"})
        raise HTTPException(403, "not authorized for this portal")
    return portal


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def prometheus_metrics():
    # Prometheus text exposition. Unauthenticated (scrapers don't send creds);
    # expose only on an internal network / firewall it — it reveals traffic
    # volume + latency, nothing sensitive. See SECURITY.md.
    return PlainTextResponse(metrics.render(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/portals")
def list_portals(db: Session = Depends(db_session)):
    return [p.slug for p in db.query(Portal).all()]


@app.get("/api/portals/{slug}/config.json")
def config_json(slug: str, request: Request,
                user: dict | None = Depends(current_user),
                db: Session = Depends(db_session), _rl: None = Depends(_config_rate)):
    uroles = access.user_roles(user)
    portal = get_portal(slug, db, uroles)
    source = configsrc.with_live_access(configsrc.active_source(portal, db), db, portal.catalog)
    data = configsrc.build_config(source, configsrc.service_grants(db, portal.catalog),
                                  configsrc.module_restrictions(db, slug), uroles)
    return serve_config(data, request, portal)


@app.get("/api/portals/{slug}/services.json")
def services_json(slug: str, request: Request,
                  user: dict | None = Depends(current_user),
                  db: Session = Depends(db_session), _rl: None = Depends(_config_rate)):
    uroles = access.user_roles(user)
    portal = get_portal(slug, db, uroles)
    source = configsrc.with_live_access(configsrc.active_source(portal, db), db, portal.catalog)
    data = configsrc.build_services(source, configsrc.service_grants(db, portal.catalog), uroles)
    return serve_config(data, request, portal)


@app.get("/api/portals/{slug}/rest-services.json")
def rest_services_json(slug: str, request: Request,
                       user: dict | None = Depends(current_user),
                       db: Session = Depends(db_session), _rl: None = Depends(_config_rate)):
    portal = get_portal(slug, db, access.user_roles(user))
    return serve_config(configsrc.active_source(portal, db)["rest_services"], request, portal)


@app.get("/api/portals/{slug}/style.json")
def style_json(slug: str, request: Request,
               user: dict | None = Depends(current_user),
               db: Session = Depends(db_session), _rl: None = Depends(_config_rate)):
    portal = get_portal(slug, db, access.user_roles(user))
    return serve_config(configsrc.active_source(portal, db)["styles"], request, portal)
