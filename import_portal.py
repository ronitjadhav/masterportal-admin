"""Import an existing Masterportal portal folder into the database.

Reads the portal's config.js to find layerConf/restConf/styleConf — these can
be local paths (relative to the portal folder) or remote https URLs, exactly
as Masterportal itself supports.

Usage: python import_portal.py <portal_dir> <slug> [<catalog>]
e.g.:  python import_portal.py ../masterportal/portal/master master

<catalog> defaults to <slug>. Give several portals the same catalog name to
share one service/style set; different catalogs never collide even when they
reuse the same external service ids.
"""
import json
import re
import sys
from pathlib import Path

import httpx

from app.db import Portal, RestService, Service, SessionLocal, Style, init_db, scoped_key

FETCH_MAX_BYTES = 100 * 2**20


def conf_paths(portal_dir: Path) -> dict:
    """Pull layerConf/restConf/styleConf out of config.js.

    ponytail: a regex over the JS source, not a JS parser — these are always
    plain string literals in practice. Swap in a real parser if a portal ever
    computes them.
    """
    source = (portal_dir / "config.js").read_text(encoding="utf-8")
    found = dict(re.findall(r"(layerConf|restConf|styleConf)\s*:\s*[\"']([^\"']+)[\"']", source))
    missing = {"layerConf", "restConf", "styleConf"} - set(found)
    if missing:
        sys.exit(f"config.js is missing {sorted(missing)}")
    return found


def load(portal_dir: Path, ref: str):
    if ref.startswith(("http://", "https://")):
        print(f"fetching {ref} ...")
        with httpx.Client(timeout=60, follow_redirects=True, max_redirects=3) as http:
            with http.stream("GET", ref) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > FETCH_MAX_BYTES:
                        sys.exit(f"{ref} exceeds {FETCH_MAX_BYTES} bytes — refusing")
                return json.loads(bytes(body))
    return json.loads((portal_dir / ref).read_text(encoding="utf-8"))


def walk_layers(elements):
    """Yield every layer element (recursing into folders) of a layerConfig tree."""
    for el in elements or []:
        if el.get("type") == "folder":
            yield from walk_layers(el.get("elements"))
        else:
            yield el


def validate(config, services, styles):
    """Warn on broken references (the mistakes hand-edited configs always have).

    Warnings, not errors: real-world catalogs (e.g. Hamburg's) ship with a few
    broken references, and refusing the whole import would be worse.
    """
    service_ids = {str(s["id"]) for s in services if "id" in s}
    style_ids = {str(s["styleId"]) for s in styles if "styleId" in s}
    for group in config.get("layerConfig", {}).values():
        for el in walk_layers(group.get("elements")):
            el_id = el.get("id")
            for lid in el_id if isinstance(el_id, list) else [el_id]:
                # ids may carry a suffix ("8712.1" reuses service "8712")
                if str(lid) not in service_ids and str(lid).split(".")[0] not in service_ids:
                    print(f"WARNING: layerConfig references unknown service id {lid!r}")
            if el.get("styleId") and str(el["styleId"]) not in style_ids:
                print(f"WARNING: layer {el_id!r} references unknown styleId {el['styleId']!r}")


def main():
    portal_dir = Path(sys.argv[1])
    slug = sys.argv[2]
    catalog = sys.argv[3] if len(sys.argv) > 3 else slug

    refs = conf_paths(portal_dir)
    config = json.loads((portal_dir / "config.json").read_text(encoding="utf-8"))
    services = load(portal_dir, refs["layerConf"])
    rest_services = load(portal_dir, refs["restConf"])
    styles = load(portal_dir, refs["styleConf"])

    validate(config, services, styles)

    init_db()
    with SessionLocal.begin() as db:
        for pos, s in enumerate(services):
            if "id" not in s:
                print(f"WARNING: skipping catalog entry without id: {str(s)[:80]}")
                continue
            # import is an authoritative reset from the config file: services
            # come back public, transactions off. Re-run enable_login / grant.py
            # afterwards to re-apply access policy.
            db.merge(Service(key=scoped_key(catalog, str(s["id"])), catalog=catalog,
                             external_id=str(s["id"]), position=pos, attrs=s,
                             is_public=True, allow_transactions=False))
        for pos, r in enumerate(rest_services):
            db.merge(RestService(key=scoped_key(catalog, str(r["id"])), catalog=catalog,
                                 position=pos, attrs=r))
        for pos, s in enumerate(styles):
            db.merge(Style(key=scoped_key(catalog, str(s["styleId"])), catalog=catalog,
                           position=pos, attrs=s))
        db.merge(Portal(
            slug=slug,
            catalog=catalog,
            portal_config=config["portalConfig"],
            layer_config=config["layerConfig"],
        ))
    print(f"imported portal {slug!r} (catalog {catalog!r}): {len(services)} services, "
          f"{len(rest_services)} rest-services, {len(styles)} styles")


if __name__ == "__main__":
    main()
