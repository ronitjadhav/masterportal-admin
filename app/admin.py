"""Admin API + BFF login (Phase 4).

Identity isolation (audit P0):
- /api/admin/* only accepts tokens whose `aud` contains OIDC_ADMIN_AUDIENCE
  AND whose roles include `admin`. Portal tokens are never valid here.
- The admin UI never sees a token: the backend runs the OIDC code exchange
  itself (BFF) and hands the browser an HttpOnly, Secure, SameSite=Strict
  session cookie. Masterportal keeps ITS tokens in JS-readable cookies, so a
  portal/addon XSS could read those — but not the admin session.
- Cookie-authenticated mutations additionally require a same-origin Origin
  header (belt on top of SameSite=Strict). Bearer-token calls (scripts, CI)
  skip cookies entirely.

ponytail: sessions in a process dict — single worker; move to Redis/DB rows
when running more than one process.
"""
import base64
import copy
import hashlib
import re
import secrets
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import access, configsrc, portalcfg, settings
from .auth import roles as token_roles
from .auth import validate_token
from .db import (AuditLog, ModuleRole, Portal, PortalRole, Service,
                 ServiceRole, Snapshot, Style, db_session, scoped_key)
from .proxy import _assert_allowed_upstream

router = APIRouter()

_sessions: dict[str, dict] = {}          # sid -> {access, refresh, exp}
_pending_logins: dict[str, tuple] = {}   # state -> (verifier, created)
SESSION_COOKIE = "admin_session"
CAPS_MAX_BYTES = 20 * 2**20


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- BFF login

@router.get("/admin/")
def admin_ui():
    return FileResponse(__file__.rsplit("/", 1)[0] + "/static/admin.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/admin/login")
def admin_login(request: Request):
    for state in [s for s, (_, t) in _pending_logins.items() if _now() - t > 600]:
        del _pending_logins[state]
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    _pending_logins[state] = (verifier, _now())
    params = {
        "response_type": "code",
        "client_id": settings.OIDC_ADMIN_CLIENT_ID,
        "redirect_uri": f"{settings.PUBLIC_BASE_URL}/admin/callback",
        "scope": "openid profile email",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if request.query_params.get("prompt") == "login":
        params["prompt"] = "login"   # force a fresh IdP prompt (switch account)
    return RedirectResponse(f"{settings.OIDC_ISSUER}/protocol/openid-connect/auth?{urlencode(params)}")


def _denied_page(message: str) -> HTMLResponse:
    return HTMLResponse(status_code=403, content=f"""<!doctype html><meta charset=utf-8>
<title>Access denied</title><style>body{{font-family:system-ui,sans-serif;background:#f5f6f8;
color:#101828;display:grid;place-items:center;height:100vh;margin:0}}.box{{background:#fff;border:1px solid #e7e9ee;
border-radius:14px;padding:2rem 2.2rem;max-width:420px;text-align:center;box-shadow:0 6px 20px rgba(16,24,40,.08)}}
h1{{font-size:18px;margin:.2rem 0 .4rem}}p{{color:#667085;font-size:14px}}a{{display:inline-block;margin-top:1rem;
background:#101828;color:#fff;padding:.55rem 1rem;border-radius:9px;text-decoration:none;font-size:13px}}</style>
<div class=box><h1>Access denied</h1><p>{esc_html(message)}</p>
<a href="/admin/login?prompt=login">Sign in as a different user</a></div>""")


def esc_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


@router.get("/admin/callback")
def admin_callback(code: str, state: str):
    pending = _pending_logins.pop(state, None)
    if pending is None:
        raise HTTPException(400, "unknown or expired login state")
    tokens = httpx.post(settings.OIDC_TOKEN_URL, timeout=10, data={
        "grant_type": "authorization_code",
        "client_id": settings.OIDC_ADMIN_CLIENT_ID,
        "redirect_uri": f"{settings.PUBLIC_BASE_URL}/admin/callback",
        "code": code,
        "code_verifier": pending[0],
    })
    if tokens.status_code != 200:
        raise HTTPException(401, f"token exchange failed: {tokens.text[:200]}")
    tokens = tokens.json()
    try:
        claims = _validate_admin_access_token(tokens["access_token"])
    except HTTPException as exc:
        # wrong user / missing admin role: friendly page, not raw JSON
        return _denied_page(exc.detail if isinstance(exc.detail, str) else "Admin access required.")

    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {
        "access": tokens["access_token"],
        "refresh": tokens.get("refresh_token"),
        "exp": claims["exp"],
    }
    response = RedirectResponse("/admin/")
    response.set_cookie(SESSION_COOKIE, sid, httponly=True, secure=True,
                        samesite="strict", path="/")
    return response


@router.post("/admin/logout")
def admin_logout(request: Request):
    _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
    response = RedirectResponse("/admin/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _validate_admin_access_token(token: str) -> dict:
    try:
        claims = validate_token(token, audience=settings.OIDC_ADMIN_AUDIENCE)
    except Exception as exc:
        raise HTTPException(401, f"invalid admin token: {exc}",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    if access.ADMIN_ROLE not in token_roles(claims):
        raise HTTPException(403, "admin role required")
    return claims


def _refresh_session(session: dict) -> bool:
    if not session.get("refresh"):
        return False
    tokens = httpx.post(settings.OIDC_TOKEN_URL, timeout=10, data={
        "grant_type": "refresh_token",
        "client_id": settings.OIDC_ADMIN_CLIENT_ID,
        "refresh_token": session["refresh"],
    })
    if tokens.status_code != 200:
        return False
    tokens = tokens.json()
    session["access"] = tokens["access_token"]
    session["refresh"] = tokens.get("refresh_token", session["refresh"])
    session["exp"] = _now() + int(tokens.get("expires_in", 300))
    return True


def require_admin(request: Request) -> dict:
    """Verified admin claims, from a bearer token or the BFF session cookie."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return _validate_admin_access_token(header[len("Bearer "):])

    session = _sessions.get(request.cookies.get(SESSION_COOKIE, ""))
    if session is None:
        raise HTTPException(401, "admin login required")
    if session["exp"] - 30 < _now() and not _refresh_session(session):
        raise HTTPException(401, "admin session expired")
    if request.method != "GET":
        # CSRF belt on top of SameSite=Strict for cookie-authenticated writes.
        origin = request.headers.get("Origin", "")
        if origin and origin not in settings.PORTAL_ORIGINS:
            raise HTTPException(403, "cross-origin admin mutation refused")
    return _validate_admin_access_token(session["access"])


# ---------------------------------------------------------------- helpers

def _audit(db: Session, claims: dict, action: str, target: str, detail: dict):
    db.add(AuditLog(ts=datetime.now(timezone.utc).isoformat(),
                    actor=claims.get("preferred_username") or claims["sub"],
                    action=action, target=target, detail=detail))


def _service_row(s: Service) -> dict:
    return {"key": s.key, "catalog": s.catalog, "id": s.external_id,
            "name": s.attrs.get("name"), "typ": s.attrs.get("typ"),
            "url": s.attrs.get("url"), "is_public": s.is_public,
            "allow_transactions": s.allow_transactions,
            "upstream_auth_env": s.upstream_auth_env}


# ---------------------------------------------------------------- read APIs

@router.get("/api/admin/me")
def me(claims: dict = Depends(require_admin)):
    return {"username": claims.get("preferred_username") or claims["sub"],
            "roles": sorted(token_roles(claims))}


def _portal_title(p: Portal) -> str:
    return ((p.portal_config.get("mainMenu", {}) or {}).get("title", {}) or {}).get("text", "") or p.slug


@router.get("/api/admin/portals")
def portals(claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    return [{"slug": p.slug, "catalog": p.catalog, "title": _portal_title(p),
             "description": p.description,
             "services": db.query(Service).filter(Service.catalog == p.catalog).count()}
            for p in db.query(Portal).all()]


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


@router.post("/api/admin/portals")
def create_portal(payload: dict = Body(...), claims: dict = Depends(require_admin),
                  db: Session = Depends(db_session)):
    slug = (payload.get("slug") or "").strip()
    title = (payload.get("title") or slug).strip()
    catalog = (payload.get("catalog") or slug).strip()
    if not SLUG_RE.match(slug):
        raise HTTPException(422, "slug must be lowercase letters/digits/-/_ (max 41 chars)")
    if db.get(Portal, slug) is not None:
        raise HTTPException(409, f"portal {slug!r} already exists")
    cfg = portalcfg.starter(title)
    db.add(Portal(slug=slug, catalog=catalog,
                  description=(payload.get("description") or "").strip() or None,
                  portal_config=cfg["portalConfig"], layer_config=cfg["layerConfig"],
                  active_snapshot_id=None))
    _audit(db, claims, "portal.create", slug, {"catalog": catalog, "title": title})
    db.commit()
    return {"slug": slug, "catalog": catalog, "title": title}


@router.post("/api/admin/portals/{slug}/clone")
def clone_portal(slug: str, payload: dict = Body(...), claims: dict = Depends(require_admin),
                 db: Session = Depends(db_session)):
    src = db.get(Portal, slug)
    if src is None:
        raise HTTPException(404, "unknown portal")
    new_slug = (payload.get("slug") or "").strip()
    if not SLUG_RE.match(new_slug):
        raise HTTPException(422, "invalid slug")
    if db.get(Portal, new_slug) is not None:
        raise HTTPException(409, f"portal {new_slug!r} already exists")
    pc = copy.deepcopy(src.portal_config)
    if payload.get("title"):
        pc.setdefault("mainMenu", {}).setdefault("title", {})["text"] = payload["title"].strip()
    db.add(Portal(slug=new_slug, catalog=src.catalog, portal_config=pc,
                  layer_config=copy.deepcopy(src.layer_config), active_snapshot_id=None))
    _audit(db, claims, "portal.clone", new_slug, {"from": slug})
    db.commit()
    return {"slug": new_slug, "catalog": src.catalog}


@router.delete("/api/admin/portals/{slug}")
def delete_portal(slug: str, claims: dict = Depends(require_admin),
                  db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    if db.query(Portal).count() <= 1:
        raise HTTPException(409, "cannot delete the last portal")
    for row in db.query(Snapshot).filter(Snapshot.portal_slug == slug):
        db.delete(row)
    for row in db.query(PortalRole).filter(PortalRole.portal_slug == slug):
        db.delete(row)
    for row in db.query(ModuleRole).filter(ModuleRole.portal_slug == slug):
        db.delete(row)
    db.delete(portal)
    _audit(db, claims, "portal.delete", slug, {})
    db.commit()
    return {"deleted": slug}


@router.get("/api/admin/portals/{slug}/settings")
def get_settings(slug: str, claims: dict = Depends(require_admin),
                 db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    return {"slug": slug, "catalog": portal.catalog, "description": portal.description or "",
            "settings": portalcfg.extract_settings(portal.portal_config),
            "moduleCatalog": portalcfg.module_catalog(),
            "controls": portalcfg.CONTROLS}


@router.patch("/api/admin/portals/{slug}/settings")
def patch_settings(slug: str, payload: dict = Body(...), claims: dict = Depends(require_admin),
                   db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    if payload.get("map", {}).get("startingMapMode") not in (None, "2D", "3D"):
        raise HTTPException(422, "startingMapMode must be 2D or 3D")
    if "description" in payload:      # a Portal column, not part of portalConfig
        portal.description = (payload["description"] or "").strip() or None
    portal.portal_config = portalcfg.apply_settings(portal.portal_config, payload)
    _audit(db, claims, "portal.settings", slug, {"keys": sorted(payload)})
    db.commit()
    return portalcfg.extract_settings(portal.portal_config)


@router.get("/api/admin/portals/{slug}/portal-config")
def get_portal_config(slug: str, claims: dict = Depends(require_admin),
                      db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    return {"portal_config": portal.portal_config}


def _validate_portal_config(pc) -> list[str]:
    """Guardrail validation for the raw portalConfig editor: reject the few
    structural mistakes that would break the portal (422); warn on the rest.
    Not a full schema — the draft + publish/rollback net covers the long tail."""
    if not isinstance(pc, dict):
        raise HTTPException(422, "portalConfig must be a JSON object")
    for key in ("map", "mainMenu", "secondaryMenu", "tree", "portalFooter"):
        if key in pc and not isinstance(pc[key], dict):
            raise HTTPException(422, f"portalConfig.{key} must be an object")
    for menu in ("mainMenu", "secondaryMenu"):
        m = pc.get(menu)
        if isinstance(m, dict) and "sections" in m and not isinstance(m["sections"], list):
            raise HTTPException(422, f"{menu}.sections must be a list")
    warnings = []
    if not ((pc.get("mainMenu", {}) or {}).get("title", {}) or {}).get("text"):
        warnings.append("no portal title set (portalConfig.mainMenu.title.text)")
    return warnings


@router.put("/api/admin/portals/{slug}/portal-config")
def put_portal_config(slug: str, payload: dict = Body(...),
                      claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    pc = payload.get("portal_config")
    warnings = _validate_portal_config(pc)
    portal.portal_config = pc          # reassignment → SQLAlchemy marks dirty
    _audit(db, claims, "portal.rawconfig", slug, {"warnings": len(warnings)})
    db.commit()
    return {"ok": True, "warnings": warnings}


@router.post("/api/admin/portals/{slug}/modules")
def toggle_module(slug: str, payload: dict = Body(...), claims: dict = Depends(require_admin),
                  db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    try:
        portal.portal_config = portalcfg.set_module(
            portal.portal_config, payload["menu"], payload["type"], bool(payload["enabled"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    _audit(db, claims, "portal.module", slug,
           {"menu": payload["menu"], "type": payload["type"], "enabled": bool(payload["enabled"])})
    db.commit()
    return portalcfg.extract_settings(portal.portal_config)


@router.get("/api/admin/services")
def services(catalog: str | None = None, q: str | None = None,
             offset: int = 0, limit: int = 50, portal: str | None = None, in_portal: bool = False,
             claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    query = db.query(Service)
    if catalog:
        query = query.filter(Service.catalog == catalog)
    rows = query.order_by(Service.position).all()
    if q:
        needle = q.lower()
        rows = [s for s in rows if needle in s.external_id.lower()
                or needle in str(s.attrs.get("name", "")).lower()]
    # which of these are actually used in the given portal's layer tree
    used: set[str] = set()
    if portal:
        p = db.get(Portal, portal)
        if p:
            for group in p.layer_config.values():
                if isinstance(group, dict):
                    _collect_ids(group.get("elements"), used)
    if in_portal:
        rows = [s for s in rows if s.external_id in used]
    return {"total": len(rows), "used_in_portal": len(used) if portal else None,
            "items": [{**_service_row(s), "in_portal": s.external_id in used}
                      for s in rows[offset:offset + min(limit, 200)]]}


@router.get("/api/admin/grants")
def grants(claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    return {
        "services": [{"key": r.service_key, "role": r.role} for r in db.query(ServiceRole)],
        "modules": [{"portal": r.portal_slug, "module": r.module_type, "role": r.role}
                    for r in db.query(ModuleRole)],
        "portals": [{"portal": r.portal_slug, "role": r.role} for r in db.query(PortalRole)],
    }


@router.get("/api/admin/audit")
def audit(limit: int = 100, claims: dict = Depends(require_admin),
          db: Session = Depends(db_session)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500)).all()
    return [{"ts": r.ts, "actor": r.actor, "action": r.action,
             "target": r.target, "detail": r.detail} for r in rows]


# ---------------------------------------------------------------- mutations

PATCHABLE = {"is_public", "allow_transactions", "upstream_auth_env"}


@router.patch("/api/admin/services/{key}")
def patch_service(key: str, payload: dict = Body(...),
                  claims: dict = Depends(require_admin),
                  db: Session = Depends(db_session)):
    service = db.get(Service, key)
    if service is None:
        raise HTTPException(404, "unknown service")
    unknown = set(payload) - PATCHABLE
    if unknown:
        raise HTTPException(422, f"not patchable: {sorted(unknown)}")
    env = payload.get("upstream_auth_env")
    if env and not env.startswith(settings.UPSTREAM_SECRET_PREFIX):
        raise HTTPException(422, f"upstream_auth_env must start with "
                                 f"{settings.UPSTREAM_SECRET_PREFIX}")
    changes = {}
    for field, value in payload.items():
        old = getattr(service, field)
        if old != value:
            setattr(service, field, value)
            changes[field] = {"old": old, "new": value}
    if changes:
        _audit(db, claims, "service.patch", key, changes)
        db.commit()
    return _service_row(service)


@router.post("/api/admin/grants/{kind}")
def add_grant(kind: str, payload: dict = Body(...),
              claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    row = _grant_row(kind, payload, db)
    db.merge(row)
    _audit(db, claims, f"grant.{kind}.add", str(payload), payload)
    db.commit()
    return {"ok": True}


@router.post("/api/admin/grants/{kind}/delete")
def delete_grant(kind: str, payload: dict = Body(...),
                 claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    row = db.get(type(_grant_row(kind, payload, db)),
                 _grant_pk(kind, payload))
    if row is None:
        raise HTTPException(404, "no such grant")
    db.delete(row)
    _audit(db, claims, f"grant.{kind}.delete", str(payload), payload)
    db.commit()
    return {"ok": True}


def _grant_row(kind: str, p: dict, db: Session):
    try:
        if kind == "service":
            key = p.get("key") or scoped_key(p["catalog"], p["id"])
            if db.get(Service, key) is None:
                raise HTTPException(404, f"unknown service {key}")
            return ServiceRole(service_key=key, role=p["role"])
        if kind == "module":
            return ModuleRole(portal_slug=p["portal"], module_type=p["module"], role=p["role"])
        if kind == "portal":
            return PortalRole(portal_slug=p["portal"], role=p["role"])
    except KeyError as exc:
        raise HTTPException(422, f"missing field {exc}") from exc
    raise HTTPException(404, "kind must be service|module|portal")


def _grant_pk(kind: str, p: dict):
    if kind == "service":
        return (p.get("key") or scoped_key(p["catalog"], p["id"]), p["role"])
    if kind == "module":
        return (p["portal"], p["module"], p["role"])
    return (p["portal"], p["role"])


# ---------------------------------------------------------------- publish

@router.get("/api/admin/portals/{slug}/snapshots")
def list_snapshots(slug: str, claims: dict = Depends(require_admin),
                   db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    snaps = (db.query(Snapshot).filter(Snapshot.portal_slug == slug)
             .order_by(Snapshot.version.desc()).all())
    live = configsrc.live_source(portal, db)
    active = next((s for s in snaps if s.id == portal.active_snapshot_id), None)
    return {
        "active_version": active.version if active else None,
        # draft differs from what's live-served → a publish would change output
        "draft_dirty": active is None or active.data != live,
        "snapshots": [{"version": s.version, "created_ts": s.created_ts,
                       "created_by": s.created_by, "active": s.id == portal.active_snapshot_id}
                      for s in snaps],
    }


@router.post("/api/admin/portals/{slug}/publish")
def publish(slug: str, claims: dict = Depends(require_admin),
            db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    version = (db.query(Snapshot).filter(Snapshot.portal_slug == slug).count()) + 1
    snap = Snapshot(portal_slug=slug, version=version,
                    created_ts=datetime.now(timezone.utc).isoformat(),
                    created_by=claims.get("preferred_username") or claims["sub"],
                    data=configsrc.live_source(portal, db))
    db.add(snap)
    db.flush()
    portal.active_snapshot_id = snap.id
    _audit(db, claims, "portal.publish", slug, {"version": version})
    db.commit()
    return {"version": version, "active": True}


@router.post("/api/admin/portals/{slug}/activate")
def activate(slug: str, payload: dict = Body(...),
             claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    """Roll back (or forward) to an existing snapshot version, or unpublish
    (version=null → serve the live draft, dev-style)."""
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    version = payload.get("version")
    if version is None:
        portal.active_snapshot_id = None
    else:
        snap = (db.query(Snapshot)
                .filter(Snapshot.portal_slug == slug, Snapshot.version == version).first())
        if snap is None:
            raise HTTPException(404, f"no snapshot version {version}")
        portal.active_snapshot_id = snap.id
    _audit(db, claims, "portal.activate", slug, {"version": version})
    db.commit()
    return {"active_version": version}


# ---------------------------------------------------------------- tree editor

LAYER_CONFIG_GROUPS = {"baselayer", "subjectlayer"}
MAX_TREE_DEPTH = 12


def _collect_ids(elements, out):
    for el in elements or []:
        if isinstance(el, dict) and el.get("type") == "folder":
            _collect_ids(el.get("elements"), out)
        elif isinstance(el, dict):
            el_id = el.get("id")
            for x in (el_id if isinstance(el_id, list) else [el_id]):
                if x is not None:
                    out.add(str(x).split(".")[0])


def _validate_tree(lc: dict, known_ids: set[str]) -> list[str]:
    """Reject structurally broken trees (422); return soft warnings for ids
    not in the catalog (legitimate for inline GeoJSON/StaticImage layers)."""
    if not isinstance(lc, dict):
        raise HTTPException(422, "layerConfig must be an object")
    extra = set(lc) - LAYER_CONFIG_GROUPS
    if extra:
        raise HTTPException(422, f"unknown top-level groups: {sorted(extra)}")
    warnings: list[str] = []

    def walk(elements, depth, path):
        if depth > MAX_TREE_DEPTH:
            raise HTTPException(422, f"tree nested too deep at {path}")
        if not isinstance(elements, list):
            raise HTTPException(422, f"{path}.elements must be a list")
        for i, el in enumerate(elements):
            here = f"{path}[{i}]"
            if not isinstance(el, dict):
                raise HTTPException(422, f"{here} must be an object")
            if el.get("type") == "folder":
                if not isinstance(el.get("name"), str) or not el["name"].strip():
                    raise HTTPException(422, f"{here} folder needs a name")
                walk(el.get("elements", []), depth + 1, here)
            else:
                el_id = el.get("id")
                if el_id is None:
                    raise HTTPException(422, f"{here} layer needs an id")
                ids = el_id if isinstance(el_id, list) else [el_id]
                if not ids or not all(isinstance(x, (str, int)) for x in ids):
                    raise HTTPException(422, f"{here} invalid id {el_id!r}")
                for x in ids:
                    if str(x).split(".")[0] not in known_ids:
                        warnings.append(f"{here}: id {x!r} not in catalog (inline layer?)")

    for group, val in lc.items():
        if not isinstance(val, dict) or not isinstance(val.get("elements"), list):
            raise HTTPException(422, f"{group} must have an elements list")
        walk(val["elements"], 1, group)
    return warnings


def _strip_uids(node):
    """Remove editor-only _uid keys the UI attaches, recursively."""
    if isinstance(node, dict):
        return {k: _strip_uids(v) for k, v in node.items() if k != "_uid"}
    if isinstance(node, list):
        return [_strip_uids(x) for x in node]
    return node


@router.get("/api/admin/portals/{slug}/tree")
def get_tree(slug: str, claims: dict = Depends(require_admin),
             db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    ids: set[str] = set()
    for group in portal.layer_config.values():
        if isinstance(group, dict):
            _collect_ids(group.get("elements"), ids)
    layers: dict[str, dict] = {}
    if ids:
        for s in (db.query(Service)
                  .filter(Service.catalog == portal.catalog, Service.external_id.in_(ids))):
            layers[s.external_id] = {"name": s.attrs.get("name"), "typ": s.attrs.get("typ"),
                                     "is_public": s.is_public, "styleId": s.attrs.get("styleId")}

    # effective styleId per tree layer (element override wins over the service's)
    style_ids: set[str] = set()

    def walk_styles(elements):
        for el in elements or []:
            if el.get("type") == "folder":
                walk_styles(el.get("elements"))
            else:
                sid = el.get("styleId")
                if not sid:
                    el_id = el.get("id")
                    base = str(el_id).split(".")[0] if not isinstance(el_id, list) else None
                    sid = layers.get(base, {}).get("styleId") if base else None
                if sid:
                    style_ids.add(str(sid))
    for group in portal.layer_config.values():
        if isinstance(group, dict):
            walk_styles(group.get("elements"))

    swatches: dict[str, dict] = {}
    if style_ids:
        keys = [scoped_key(portal.catalog, s) for s in style_ids]
        for st in db.query(Style).filter(Style.catalog == portal.catalog, Style.key.in_(keys)):
            rules = st.attrs.get("rules") or []
            swatches[_sid(st)] = rules[0].get("style", {}) if rules else {}

    return {"catalog": portal.catalog, "layer_config": portal.layer_config,
            "names": {k: v["name"] for k, v in layers.items()},   # kept for compatibility
            "layers": layers, "styleSwatches": swatches}


@router.put("/api/admin/portals/{slug}/tree")
def put_tree(slug: str, payload: dict = Body(...),
             claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    portal = db.get(Portal, slug)
    if portal is None:
        raise HTTPException(404, "unknown portal")
    lc = _strip_uids(payload.get("layer_config"))
    known = {r.external_id for r in
             db.query(Service.external_id).filter(Service.catalog == portal.catalog)}
    warnings = _validate_tree(lc, known)
    portal.layer_config = lc          # reassignment → SQLAlchemy marks dirty
    _audit(db, claims, "tree.save", slug, {"warnings": len(warnings)})
    db.commit()
    return {"ok": True, "warnings": warnings}


# ---------------------------------------------------------------- vector styles

def _style_template(style_id: str) -> dict:
    return {"styleId": style_id, "rules": [{"style": {
        "type": "circle", "circleRadius": 8,
        "circleFillColor": [0, 153, 255, 0.8],
        "circleStrokeColor": [0, 0, 0, 1], "circleStrokeWidth": 1}}]}


def _validate_style(attrs: dict, style_id: str) -> dict:
    if not isinstance(attrs, dict):
        raise HTTPException(422, "style must be a JSON object")
    if not isinstance(attrs.get("rules"), list):
        raise HTTPException(422, "style.rules must be a list")
    for i, rule in enumerate(attrs["rules"]):
        if not isinstance(rule, dict) or not isinstance(rule.get("style"), dict):
            raise HTTPException(422, f"rules[{i}] must be an object with a 'style' object")
    return {**attrs, "styleId": style_id}   # styleId is the identity — force it


def _style_usage(db: Session, catalog: str, style_id: str) -> list[str]:
    """services in the catalog that reference this styleId."""
    return [s.external_id for s in db.query(Service).filter(Service.catalog == catalog)
            if str(s.attrs.get("styleId")) == style_id]


def _sid(style: Style) -> str:
    return style.attrs.get("styleId") or style.key.split(":", 1)[-1]


@router.get("/api/admin/styles")
def list_styles(catalog: str, q: str | None = None,
                claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    usage: dict[str, int] = {}
    for s in db.query(Service).filter(Service.catalog == catalog):
        ref = s.attrs.get("styleId")
        if ref is not None:
            usage[str(ref)] = usage.get(str(ref), 0) + 1
    items = []
    for r in db.query(Style).filter(Style.catalog == catalog).order_by(Style.position).all():
        sid = _sid(r)
        if q and q.lower() not in sid.lower():
            continue
        rules = r.attrs.get("rules") or []
        items.append({"key": r.key, "styleId": sid, "rules": len(rules),
                      "firstStyle": rules[0].get("style", {}) if rules else {},
                      "usage": usage.get(sid, 0)})
    return {"total": len(items), "items": items}


@router.get("/api/admin/styles/{key}")
def get_style(key: str, claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    style = db.get(Style, key)
    if style is None:
        raise HTTPException(404, "unknown style")
    return {"key": key, "catalog": style.catalog, "styleId": _sid(style),
            "attrs": style.attrs, "usage": _style_usage(db, style.catalog, _sid(style))}


@router.post("/api/admin/styles")
def create_style(payload: dict = Body(...), claims: dict = Depends(require_admin),
                 db: Session = Depends(db_session)):
    catalog = (payload.get("catalog") or "").strip()
    style_id = (payload.get("styleId") or "").strip()
    if not catalog or not style_id:
        raise HTTPException(422, "catalog and styleId are required")
    key = scoped_key(catalog, style_id)
    if db.get(Style, key) is not None:
        raise HTTPException(409, f"style {style_id!r} already exists in {catalog!r}")
    attrs = _validate_style(payload.get("attrs") or _style_template(style_id), style_id)
    position = db.query(Style).filter(Style.catalog == catalog).count()
    db.add(Style(key=key, catalog=catalog, position=position, attrs=attrs))
    _audit(db, claims, "style.create", key, {"styleId": style_id})
    db.commit()
    return {"key": key, "styleId": style_id}


@router.put("/api/admin/styles/{key}")
def put_style(key: str, payload: dict = Body(...), claims: dict = Depends(require_admin),
              db: Session = Depends(db_session)):
    style = db.get(Style, key)
    if style is None:
        raise HTTPException(404, "unknown style")
    style.attrs = _validate_style(payload.get("attrs"), _sid(style))
    _audit(db, claims, "style.update", key, {"rules": len(style.attrs["rules"])})
    db.commit()
    return {"ok": True, "usage": _style_usage(db, style.catalog, _sid(style))}


@router.delete("/api/admin/styles/{key}")
def delete_style(key: str, claims: dict = Depends(require_admin), db: Session = Depends(db_session)):
    style = db.get(Style, key)
    if style is None:
        raise HTTPException(404, "unknown style")
    used_by = _style_usage(db, style.catalog, _sid(style))
    db.delete(style)
    _audit(db, claims, "style.delete", key, {"was_used_by": used_by})
    db.commit()
    return {"deleted": key, "was_used_by": used_by}


# ------------------------------------------------- capabilities import (WMS)

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@router.post("/api/admin/import-capabilities")
def import_capabilities(payload: dict = Body(...),
                        claims: dict = Depends(require_admin),
                        db: Session = Depends(db_session)):
    """Fetch a WMS GetCapabilities and turn its layers into service entries.

    dry_run=true (default) only previews; dry_run=false persists into the
    given catalog with ids "<id_prefix>_<layer name>".
    """
    url, catalog = payload["url"], payload["catalog"]
    dry_run = payload.get("dry_run", True)
    id_prefix = payload.get("id_prefix", "imp")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, "url must be http(s)")
    _assert_allowed_upstream(url)

    base = urlunsplit(urlsplit(url)._replace(query="", fragment=""))
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as http:
            response = http.get(base, params={"service": "WMS", "request": "GetCapabilities"})
        response.raise_for_status()
        if len(response.content) > CAPS_MAX_BYTES:
            raise HTTPException(502, "capabilities document too large")
        root = ET.fromstring(response.content)  # py3 ET refuses entity expansion
    except (httpx.HTTPError, ET.ParseError) as exc:
        raise HTTPException(502, f"capabilities fetch failed: {type(exc).__name__}") from exc

    version = root.get("version", "1.3.0")
    entries = []
    for layer in root.iter():
        if _localname(layer.tag) != "Layer":
            continue
        name = title = None
        for child in layer:
            if _localname(child.tag) == "Name":
                name = (child.text or "").strip()
            elif _localname(child.tag) == "Title":
                title = (child.text or "").strip()
        if not name:
            continue  # container layers without Name are not requestable
        entries.append({
            "id": f"{id_prefix}_{name}",
            "name": title or name,
            "url": base,
            "typ": "WMS",
            "layers": name,
            "format": "image/png",
            "version": version,
            "transparent": True,
            "singleTile": False,
            "gfiAttributes": "showAll",
        })

    if not dry_run:
        position = db.query(Service).filter(Service.catalog == catalog).count()
        for n, attrs in enumerate(entries):
            db.merge(Service(key=scoped_key(catalog, attrs["id"]), catalog=catalog,
                             external_id=attrs["id"], position=position + n,
                             attrs=attrs))
        _audit(db, claims, "capabilities.import", base,
               {"catalog": catalog, "layers": [e["id"] for e in entries]})
        db.commit()
    return {"url": base, "version": version, "dry_run": dry_run,
            "layers": entries}
