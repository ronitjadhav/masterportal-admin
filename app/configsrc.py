"""Config materialization — one filtering path for live AND published sources.

A "source" is the raw render-input for a portal (unfiltered):
    {portal_config, layer_config, services:[{key, external_id, attrs, is_public}],
     styles:[attrs...], rest_services:[attrs...]}

It comes either from the live DB (draft) or from a frozen Snapshot. Role
filtering, URL rewriting and the isSecured flag are then applied identically
on top, so publishing changes only WHAT is served, never HOW it is filtered.
"""
from . import access, settings
from .db import (ModuleRole, Portal, RestService, Service, ServiceRole,
                 Snapshot, Style)


def legend_urls_of(attrs: dict) -> list[str]:
    """Upstream legend URLs in a stable order (see proxy legend route)."""
    urls = []
    legend_url = attrs.get("legendURL")
    if isinstance(legend_url, str) and legend_url.startswith(("http://", "https://")):
        urls.append(legend_url)
    legend = attrs.get("legend")
    if isinstance(legend, list):
        urls += [u for u in legend if isinstance(u, str) and u.startswith(("http://", "https://"))]
    return urls


def live_source(portal: Portal, db) -> dict:
    """Freeze-shaped view of the portal's current draft + catalog."""
    services = (db.query(Service).filter(Service.catalog == portal.catalog)
                .order_by(Service.position).all())
    styles = (db.query(Style).filter(Style.catalog == portal.catalog)
              .order_by(Style.position).all())
    rest = (db.query(RestService).filter(RestService.catalog == portal.catalog)
            .order_by(RestService.position).all())
    return {
        "portal_config": portal.portal_config,
        "layer_config": portal.layer_config,
        "services": [{"key": s.key, "external_id": s.external_id,
                      "attrs": s.attrs, "is_public": s.is_public} for s in services],
        "styles": [s.attrs for s in styles],
        "rest_services": [r.attrs for r in rest],
    }


def active_source(portal: Portal, db) -> dict:
    """What portal users receive: the active snapshot, or the live draft."""
    if portal.active_snapshot_id is None:
        return live_source(portal, db)
    snap = db.get(Snapshot, portal.active_snapshot_id)
    if snap is None:                       # dangling pointer → fail safe to live
        return live_source(portal, db)
    return snap.data


def with_live_access(source: dict, db, catalog: str) -> dict:
    """Overlay the LIVE `is_public` of each service onto a (possibly frozen)
    source. Security state — who may see a service — must always be current, so
    securing a layer takes effect immediately even on a published snapshot; the
    snapshot still governs WHAT exists (attrs, layer tree, styles). This mirrors
    role grants, which are already read live (service_grants). Without it, a
    layer secured after publish stays advertised to anonymous users — the proxy
    still blocks the tiles (it reads the live row), but services.json would leak
    the layer's metadata and omit isSecured, breaking the client too.
    """
    live = {key: pub for key, pub in
            db.query(Service.key, Service.is_public).filter(Service.catalog == catalog)}
    services = [{**s, "is_public": live.get(s["key"], s["is_public"])}
                for s in source["services"]]
    return {**source, "services": services}


def service_grants(db, catalog: str) -> dict[str, set[str]]:
    grants: dict[str, set[str]] = {}
    for row in db.query(ServiceRole).filter(ServiceRole.service_key.like(f"{catalog}:%")):
        grants.setdefault(row.service_key, set()).add(row.role)
    return grants


def module_restrictions(db, slug: str) -> dict[str, set[str]]:
    restricted: dict[str, set[str]] = {}
    for row in db.query(ModuleRole).filter(ModuleRole.portal_slug == slug):
        restricted.setdefault(row.module_type, set()).add(row.role)
    return restricted


def public_service_attrs(key: str, attrs: dict, is_public: bool) -> dict:
    """The services.json entry as clients see it: proxy URL, never the upstream."""
    out = dict(attrs)
    if isinstance(out.get("url"), str):
        out["url"] = f"{settings.PUBLIC_BASE_URL}/geo/{key}"
    n = 0
    if legend_urls_of(attrs):
        if isinstance(out.get("legendURL"), str) and out["legendURL"].startswith(("http://", "https://")):
            out["legendURL"] = f"{settings.PUBLIC_BASE_URL}/legends/{key}/0"
            n = 1
        if isinstance(out.get("legend"), list):
            rewritten = []
            for u in out["legend"]:
                if isinstance(u, str) and u.startswith(("http://", "https://")):
                    rewritten.append(f"{settings.PUBLIC_BASE_URL}/legends/{key}/{n}")
                    n += 1
                else:
                    rewritten.append(u)
            out["legend"] = rewritten
    # ponytail: WMTS capabilitiesUrl/urls still go direct — URL-hiding is
    # best-effort cosmetics; the boundary is the proxy's 401/403.
    if not is_public:
        out["isSecured"] = True
    return out


def build_services(source: dict, grants: dict, uroles: set[str] | None) -> list[dict]:
    return [public_service_attrs(s["key"], s["attrs"], s["is_public"])
            for s in source["services"]
            if access.service_allowed(s["is_public"], grants.get(s["key"], set()), uroles)]


def build_config(source: dict, grants: dict, restricted: dict, uroles: set[str] | None) -> dict:
    known = {s["external_id"] for s in source["services"]}
    allowed = {s["external_id"] for s in source["services"]
               if access.service_allowed(s["is_public"], grants.get(s["key"], set()), uroles)}
    layer_config = (source["layer_config"] if allowed == known
                    else access.filter_layer_config(source["layer_config"], known, allowed))
    portal_config = access.filter_portal_config(source["portal_config"], restricted, uroles)
    return {"portalConfig": portal_config, "layerConfig": layer_config}
