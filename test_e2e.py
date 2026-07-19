"""End-to-end checks. Needs the backend on :8000 and Keycloak on :8080
(docker compose up -d) and internet access to the demo upstreams.

Self-seeding: the fixtures (a small `basic` catalog + the Hamburg `master`
catalog with a couple of secured/granted layers) are imported automatically if
missing, so this runs green from a clean clone — no manual setup.

Run: .venv/bin/python test_e2e.py
"""
import json
import os
import subprocess
import sys

import httpx

from app import settings

MP = os.path.join(os.path.dirname(__file__) or ".", "..", "masterportal", "portal")


def _seed(*args):
    subprocess.run([sys.executable, *args], check=True,
                   cwd=os.path.dirname(__file__) or ".")


def ensure_fixtures():
    """Provision the portals/catalogs the checks rely on, idempotently."""
    adm = {"Authorization": f"Bearer {get_token_client('masterportal-admin', 'admin-demo', 'admin-demo')}"}
    have = {p["slug"] for p in httpx.get(f"{BASE}/api/admin/portals", headers=adm, timeout=120).json()}
    if "basic" not in have:
        _seed("import_portal.py", f"{MP}/basic", "basic")
    if "master" not in have:
        _seed("import_portal.py", f"{MP}/master", "master")
    # RBAC demo fixtures on master (idempotent): secure DGM1 admin-only, grant
    # DOP to role 'user', restrict the draw module to admin.
    _seed("enable_login.py", "master", "19173")
    _seed("enable_login.py", "master", "34127")
    _seed("grant.py", "service", "master", "34127", "user")
    _seed("grant.py", "module", "master", "draw", "admin")

BASE = "http://localhost:8000"          # direct backend access
PUB = settings.PUBLIC_BASE_URL          # what clients see inside served configs
KC = "http://localhost:8080/auth/realms/masterportal/protocol/openid-connect/token"
SECURED, PUBLIC_WFS, OAF = "master:19173", "master:20609", "master:27926"
UPSTREAM_HOSTS = ("geodienste.hamburg.de", "deegree.pro")


def get_token(username, password, scope=None):
    data = {"grant_type": "password", "client_id": "masterportal",
            "username": username, "password": password}
    if scope:
        data["scope"] = scope
    return httpx.post(KC, data=data).raise_for_status().json()


def get_token_client(client_id, username, password):
    return httpx.post(KC, data={"grant_type": "password", "client_id": client_id,
        "username": username, "password": password}).raise_for_status().json()["access_token"]


def main():
    grant = get_token("demo", "demo", scope="openid")
    token, id_token = grant["access_token"], grant["id_token"]
    auth = {"Authorization": f"Bearer {token}"}
    admin_auth = {"Authorization": f"Bearer {get_token('admin-demo', 'admin-demo')['access_token']}"}
    ensure_fixtures()
    print("1. got Keycloak tokens for 'demo' (user) and 'admin-demo' (admin); fixtures ensured")

    services = httpx.get(f"{BASE}/api/portals/master/services.json",
                         headers=admin_auth, timeout=120).raise_for_status()
    assert services.headers.get("cache-control") == "no-store"
    services = services.json()
    for host in UPSTREAM_HOSTS:
        leaked = [s["id"] for s in services if host in json.dumps(s)]
        assert not leaked, f"upstream {host} leaked in services.json: {leaked[:5]}"
    svc = {s["id"]: s for s in services}
    assert svc["19173"]["url"] == f"{PUB}/geo/{SECURED}" and svc["19173"]["isSecured"] is True
    assert svc["19173"]["legendURL"] == f"{PUB}/legends/{SECURED}/0"
    assert svc["20609"]["url"] == f"{PUB}/geo/{PUBLIC_WFS}" and "isSecured" not in svc["20609"]
    print(f"2. services.json as admin ({len(services)} services): scoped keys, no upstream hosts leak")

    # --- Phase 3: deny-by-default role model. 19173 is secured with NO grants
    # (admin-only); 34127 is secured and granted to role "user"; module "draw"
    # is restricted to admin.
    anon_ids = {s["id"] for s in httpx.get(f"{BASE}/api/portals/master/services.json", timeout=120).json()}
    demo_ids = {s["id"] for s in httpx.get(f"{BASE}/api/portals/master/services.json",
                                           headers=auth, timeout=120).json()}
    assert "19173" not in anon_ids and "34127" not in anon_ids
    assert "19173" not in demo_ids and "34127" in demo_ids
    assert "19173" in svc and "34127" in svc  # admin sees both
    assert httpx.get(f"{BASE}/geo/{SECURED}").status_code == 401           # anonymous
    assert httpx.get(f"{BASE}/geo/{SECURED}", headers=auth).status_code == 403      # wrong role
    assert httpx.get(f"{BASE}/geo/master:34127", headers=auth,
                     params={"service": "WMS", "request": "GetCapabilities"},
                     timeout=30).status_code == 200                        # granted role
    print("2b. role model: anonymous < user < admin see/reach strictly more services")

    demo_cfg = httpx.get(f"{BASE}/api/portals/master/config.json", headers=auth).json()
    admin_cfg = httpx.get(f"{BASE}/api/portals/master/config.json", headers=admin_auth).json()

    def module_types(cfg):
        return {el.get("type") for menu in ("mainMenu", "secondaryMenu")
                for sec in cfg["portalConfig"].get(menu, {}).get("sections", []) for el in sec}

    def tree_ids(cfg):
        ids = set()
        def walk(els):
            for el in els or []:
                if el.get("type") == "folder":
                    walk(el.get("elements"))
                else:
                    i = el.get("id")
                    ids.update(map(str, i if isinstance(i, list) else [i]))
        for group in cfg["layerConfig"].values():
            walk(group.get("elements"))
        return ids

    assert "draw" not in module_types(demo_cfg) and "draw" in module_types(admin_cfg)
    assert "19173" not in tree_ids(demo_cfg) and "19173" in tree_ids(admin_cfg)
    assert "34127" in tree_ids(demo_cfg)
    print("2c. config.json filtered per role: tree entries and menu modules differ")

    # portal scoping: basic must serve ONLY its own catalog
    basic = httpx.get(f"{BASE}/api/portals/basic/services.json").raise_for_status().json()
    assert len(basic) == 23, f"basic portal serves {len(basic)} services, expected 23"
    assert all(s["url"].startswith(f"{PUB}/geo/basic:") for s in basic if "url" in s and s["url"].startswith("http"))
    print(f"3. portal scoping: basic serves exactly its own {len(basic)} services")

    caps = {"service": "WMS", "request": "GetCapabilities"}
    assert httpx.get(f"{BASE}/geo/{SECURED}", params=caps).status_code == 401
    assert httpx.get(f"{BASE}/legends/{SECURED}/0").status_code == 401
    assert httpx.get(f"{BASE}/geo/{SECURED}", params=caps,
                     headers={"Authorization": "Bearer eyJhbGciOiJub25lIn0.e30."}).status_code == 401
    tampered = {"Authorization": f"Bearer {token[:-20]}AAAAAAAAAAAAAAAAAAAA"}
    assert httpx.get(f"{BASE}/geo/{SECURED}", params=caps, headers=tampered).status_code == 401
    # RFC 8725: an ID token must NOT be accepted as a bearer credential
    assert httpx.get(f"{BASE}/geo/{SECURED}", params=caps,
                     headers={"Authorization": f"Bearer {id_token}"}).status_code == 401
    assert httpx.get(f"{BASE}/geo/{SECURED}", params=caps, headers=auth).status_code == 403
    r = httpx.get(f"{BASE}/geo/{SECURED}", params=caps, headers=admin_auth, timeout=30)
    assert r.status_code == 200, r.text[:200]
    print("4. secured layer: anonymous/alg=none/tampered/ID-token=401, ungranted role=403, admin=200")

    assert "geodienste.hamburg.de" not in r.text and f"{PUB}/geo/{SECURED}" in r.text
    print("5. GetCapabilities XML rewritten — upstream host hidden there too")

    r = httpx.get(f"{BASE}/geo/{SECURED}", headers=admin_auth, timeout=30, params={
        "service": "WMS", "version": "1.3.0", "request": "GetMap",
        "layers": "WMS_DGM1_HAMBURG", "styles": "", "crs": "EPSG:25832",
        "bbox": "561000,5932000,562000,5933000",
        "width": "256", "height": "256", "format": "image/png"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/"), r.text[:200]
    print(f"6. GetMap through proxy: real tile, {len(r.content)} bytes")

    r = httpx.get(f"{BASE}/legends/{SECURED}/0", headers=admin_auth, timeout=30)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    print(f"7. legend via pinned route: {len(r.content)} bytes")

    r = httpx.get(f"{BASE}/geo/{PUBLIC_WFS}", params={"service": "WFS", "request": "GetCapabilities"}, timeout=30)
    assert r.status_code == 200 and "geodienste.hamburg.de" not in r.text
    print("8. public WFS anonymous OK, capabilities rewritten")

    # mutations are opt-in: WFS-T Transaction and OGC API writes are refused
    wfst = '<wfs:Transaction xmlns:wfs="http://www.opengis.net/wfs"></wfs:Transaction>'
    assert httpx.post(f"{BASE}/geo/{PUBLIC_WFS}", content=wfst,
                      headers={"Content-Type": "application/xml"}).status_code == 403
    assert httpx.post(f"{BASE}/geo/{OAF}/collections/x/items", content="{}").status_code == 403
    # ...but plain POST GetFeature (Masterportal's filter module) still works
    getfeature = ('<wfs:GetFeature xmlns:wfs="http://www.opengis.net/wfs" service="WFS" version="1.1.0">'
                  f'<wfs:Query typeName="{svc["20609"]["featureType"]}"/></wfs:GetFeature>')
    r = httpx.post(f"{BASE}/geo/{PUBLIC_WFS}", content=getfeature,
                   headers={"Content-Type": "application/xml"}, timeout=30)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    print("9. WFS-T Transaction=403, OAF write=403, POST GetFeature still 200")

    # OGC API Features: path suffix forwarding + JSON link rewriting
    collection = svc["27926"]["collection"]
    r = httpx.get(f"{BASE}/geo/{OAF}/collections/{collection}/items",
                  params={"limit": 2, "f": "json"}, timeout=60)
    data = r.json()
    assert r.status_code == 200 and len(data.get("features", [])) == 2
    assert "geodienste.hamburg.de" not in json.dumps(data), "upstream leaked in OAF links"
    print("10. OAF items via path suffix, absolute links rewritten to proxy")

    # traversal + open-proxy guards
    assert httpx.get(f"{BASE}/geo/{OAF}/..%2F..%2Fetc").status_code in (400, 404)
    assert httpx.get(f"{BASE}/geo/nope").status_code == 404
    assert httpx.get(f"{BASE}/legends/{SECURED}/99", headers=admin_auth).status_code == 404
    assert httpx.request("DELETE", f"{BASE}/geo/{PUBLIC_WFS}").status_code == 405
    print("11. traversal=4xx, unknown key/legend=404, non-GET/POST=405 (not an open proxy)")

    cfg = httpx.get(f"{BASE}/api/portals/master/config.json", headers=admin_auth)
    assert cfg.headers.get("cache-control") == "no-store"
    assert {"type": "login"} in cfg.json()["portalConfig"]["mainMenu"]["sections"][0]
    print("12. login module present, config responses are no-store")

    # --- Phase 4: admin API identity isolation (audit P0)
    portal_admin = get_token_client("masterportal", "admin-demo", "admin-demo")   # admin user, PORTAL audience
    admin_user = get_token_client("masterportal-admin", "demo", "demo")           # admin audience, non-admin role
    admin_tok = get_token_client("masterportal-admin", "admin-demo", "admin-demo")
    adm = {"Authorization": f"Bearer {admin_tok}"}
    assert httpx.get(f"{BASE}/api/admin/portals").status_code == 401                                  # anon
    assert httpx.get(f"{BASE}/api/admin/portals",
                     headers={"Authorization": f"Bearer {portal_admin}"}).status_code == 401          # wrong audience
    assert httpx.get(f"{BASE}/api/admin/portals",
                     headers={"Authorization": f"Bearer {admin_user}"}).status_code == 403            # wrong role
    assert httpx.get(f"{BASE}/api/admin/portals", headers=adm).status_code == 200
    # reverse: an admin-audience token is not valid on the portal geo API
    assert httpx.get(f"{BASE}/geo/{SECURED}", params=caps, headers=adm).status_code == 401
    print("13. admin API: anon=401, portal-audience token=401, non-admin=403, admin=200; admin token rejected on /geo")

    # admin mutation is audited; unknown fields and bad secret env refused
    assert httpx.patch(f"{BASE}/api/admin/services/{SECURED}", headers=adm,
                       json={"allow_transactions": True}).json()["allow_transactions"] is True
    httpx.patch(f"{BASE}/api/admin/services/{SECURED}", headers=adm, json={"allow_transactions": False})
    assert httpx.patch(f"{BASE}/api/admin/services/{SECURED}", headers=adm,
                       json={"upstream_auth_env": "PATH"}).status_code == 422
    latest = httpx.get(f"{BASE}/api/admin/audit", headers=adm).json()[0]
    assert latest["action"] == "service.patch" and latest["actor"] == "admin-demo", latest
    print("14. admin PATCH audited (latest entry = service.patch by admin-demo); bad-prefix secret env=422")

    caps_preview = httpx.post(f"{BASE}/api/admin/import-capabilities", headers=adm, timeout=60,
                              json={"url": "https://geodienste.hamburg.de/HH_WMS_DGM1",
                                    "catalog": "master", "dry_run": True}).json()
    assert caps_preview["dry_run"] and caps_preview["layers"]
    print(f"15. WMS capabilities import (dry-run): {len(caps_preview['layers'])} layers parsed")

    # --- Phase 4: draft/publish snapshots (tested on 'basic' to stay isolated).
    # deterministic start: 453 public, unpublished (independent of prior runs)
    httpx.patch(f"{BASE}/api/admin/services/basic:453", headers=adm, json={"is_public": True})
    httpx.post(f"{BASE}/api/admin/portals/basic/activate", headers=adm, json={"version": None})
    live_count = len(httpx.get(f"{BASE}/api/portals/basic/services.json").json())

    pub = httpx.post(f"{BASE}/api/admin/portals/basic/publish", headers=adm).json()
    v1 = pub["version"]
    snaps = httpx.get(f"{BASE}/api/admin/portals/basic/snapshots", headers=adm).json()
    assert snaps["active_version"] == v1 and snaps["draft_dirty"] is False
    assert len(httpx.get(f"{BASE}/api/portals/basic/services.json").json()) == live_count
    print(f"16. published basic v{v1}; served from snapshot, count unchanged ({live_count})")

    # mutate the LIVE draft: secure a service. Snapshot must NOT change; draft goes dirty.
    httpx.patch(f"{BASE}/api/admin/services/basic:453", headers=adm, json={"is_public": False})
    served = {s["id"]: s for s in httpx.get(f"{BASE}/api/portals/basic/services.json").json()}
    assert served["453"].get("isSecured") is not True, "snapshot leaked a live edit"
    snaps = httpx.get(f"{BASE}/api/admin/portals/basic/snapshots", headers=adm).json()
    assert snaps["draft_dirty"] is True
    print("17. live edit (secure basic:453) does NOT affect active snapshot; draft marked dirty")

    # publish v2 (now reflects the edit), then roll back to v1 (453 public again)
    v2 = httpx.post(f"{BASE}/api/admin/portals/basic/publish", headers=adm).json()["version"]
    served = {s["id"]: s for s in httpx.get(f"{BASE}/api/portals/basic/services.json").json()}
    assert served.get("453", {}).get("isSecured") is True or "453" not in served
    httpx.post(f"{BASE}/api/admin/portals/basic/activate", headers=adm, json={"version": v1})
    served = {s["id"]: s for s in httpx.get(f"{BASE}/api/portals/basic/services.json").json()}
    assert served["453"].get("isSecured") is not True, "rollback did not restore v1"
    print(f"18. published v{v2} (453 secured), rolled back to v{v1} (453 public again)")

    # --- Phase 5: ETag revalidation on published config (basic is published now)
    r1 = httpx.get(f"{BASE}/api/portals/basic/config.json")
    assert r1.status_code == 200 and r1.headers.get("etag")
    assert "must-revalidate" in r1.headers.get("cache-control", "")
    etag = r1.headers["etag"]
    r2 = httpx.get(f"{BASE}/api/portals/basic/config.json", headers={"If-None-Match": etag})
    assert r2.status_code == 304 and not r2.content, "unchanged config should 304 with no body"
    # big services.json revalidates too
    s_etag = httpx.get(f"{BASE}/api/portals/basic/services.json").headers["etag"]
    assert httpx.get(f"{BASE}/api/portals/basic/services.json",
                     headers={"If-None-Match": s_etag}).status_code == 304
    print("18b. published config: ETag + private/must-revalidate; unchanged → 304 (no body)")

    # --- Phase 4: tree editor (validated draft save, tested on 'basic').
    # Unpublish first so config.json reflects the live draft, not a snapshot.
    httpx.post(f"{BASE}/api/admin/portals/basic/activate", headers=adm, json={"version": None})
    # live draft: no-store, and a stale If-None-Match must NOT 304
    d = httpx.get(f"{BASE}/api/portals/basic/config.json", headers={"If-None-Match": etag})
    assert d.status_code == 200 and d.headers.get("cache-control") == "no-store" and not d.headers.get("etag")
    print("18c. unpublished draft: no-store, no ETag, no 304 on stale validator")

    # --- Phase 5: gzip compression (httpx sends Accept-Encoding: gzip by default)
    big = httpx.get(f"{BASE}/api/portals/basic/services.json")
    assert big.headers.get("content-encoding") == "gzip", big.headers
    assert isinstance(big.json(), list)                    # still decodes fine
    tiny = httpx.get(f"{BASE}/healthz")
    assert tiny.headers.get("content-encoding") in (None, "identity")   # below threshold
    print("18d. gzip: large config compressed (Vary: Accept-Encoding), tiny responses left alone")

    # --- Phase 5: structured access logging (X-Request-ID + JSON line)
    import json as _json
    from app.logging_setup import format_line, _claimed_actor
    parsed = _json.loads(format_line("INFO", "http_request", {"status": 200, "path": "/x", "actor": "demo"}))
    assert parsed["msg"] == "http_request" and parsed["status"] == 200 and parsed["actor"] == "demo"
    assert _claimed_actor(f"Bearer {token}") in ("demo", grant.get("sub"))   # unverified parse
    assert _claimed_actor("Bearer garbage") is None and _claimed_actor("") is None
    # X-Request-ID: echoed when supplied, generated otherwise
    assert httpx.get(f"{BASE}/healthz", headers={"X-Request-ID": "trace-abc"}).headers.get("x-request-id") == "trace-abc"
    assert httpx.get(f"{BASE}/healthz").headers.get("x-request-id")
    print("18e. access log: JSON formatter + claimed-actor parse; X-Request-ID echoed/generated")
    import copy as _copy
    t = httpx.get(f"{BASE}/api/admin/portals/basic/tree", headers=adm).json()
    assert set(t["layer_config"]) <= {"baselayer", "subjectlayer"} and t["names"]
    orig_tree = _copy.deepcopy(t["layer_config"])

    edited = _copy.deepcopy(orig_tree)
    edited["subjectlayer"]["elements"].append(
        {"type": "folder", "name": "E2E Folder", "elements": [{"id": "682"}]})
    r = httpx.put(f"{BASE}/api/admin/portals/basic/tree", headers=adm,
                  json={"layer_config": edited})
    assert r.status_code == 200 and r.json()["warnings"] == [], r.text
    served = httpx.get(f"{BASE}/api/portals/basic/config.json").json()["layerConfig"]
    folder = [e for e in served["subjectlayer"]["elements"] if e.get("type") == "folder"]
    assert folder and folder[0]["name"] == "E2E Folder" and folder[0]["elements"][0]["id"] == "682"
    print("16b. tree editor: folder+layer saved to draft and served")

    # structural validation (422) and soft unknown-id warning
    assert httpx.put(f"{BASE}/api/admin/portals/basic/tree", headers=adm,
                     json={"layer_config": {"bogus": {"elements": []}}}).status_code == 422
    assert httpx.put(f"{BASE}/api/admin/portals/basic/tree", headers=adm,
                     json={"layer_config": {"subjectlayer": {"elements": [{"type": "folder", "elements": []}]}}}).status_code == 422
    warn = httpx.put(f"{BASE}/api/admin/portals/basic/tree", headers=adm, json={"layer_config":
                     {"baselayer": {"elements": []}, "subjectlayer": {"elements": [{"id": "NOPE"}]}}}).json()
    assert warn["warnings"], warn
    print("16c. tree validation: unknown group/nameless folder=422, unknown id=soft warning")

    # non-admin cannot save the tree
    assert httpx.put(f"{BASE}/api/admin/portals/basic/tree",
                     headers={"Authorization": f"Bearer {admin_user}"},
                     json={"layer_config": orig_tree}).status_code == 403
    # restore basic's real tree
    httpx.put(f"{BASE}/api/admin/portals/basic/tree", headers=adm, json={"layer_config": orig_tree})
    print("16d. tree save requires admin (non-admin=403); basic tree restored")

    # --- Vector styles CRUD (style.json), on the master catalog
    httpx.request("DELETE", f"{BASE}/api/admin/styles/master:e2e-style", headers=adm)
    lst = httpx.get(f"{BASE}/api/admin/styles?catalog=master&q=1711", headers=adm).json()
    assert any(i["styleId"] == "1711" for i in lst["items"]), "expected style 1711 in master"
    c = httpx.post(f"{BASE}/api/admin/styles", headers=adm, json={"catalog": "master", "styleId": "e2e-style"})
    assert c.status_code == 200 and c.json()["styleId"] == "e2e-style"
    assert httpx.post(f"{BASE}/api/admin/styles", headers=adm,
                      json={"catalog": "master", "styleId": "e2e-style"}).status_code == 409   # dup
    # edit to a polygon style; styleId is forced immutable
    put = httpx.put(f"{BASE}/api/admin/styles/master:e2e-style", headers=adm, json={"attrs": {
        "styleId": "IGNORED", "rules": [{"style": {"polygonFillColor": [255, 0, 0, 0.5],
        "polygonStrokeColor": [0, 0, 0, 1], "polygonStrokeWidth": 2}}]}})
    assert put.status_code == 200
    got = httpx.get(f"{BASE}/api/admin/styles/master:e2e-style", headers=adm).json()
    assert got["styleId"] == "e2e-style" and got["attrs"]["rules"][0]["style"]["polygonFillColor"] == [255, 0, 0, 0.5]
    assert httpx.put(f"{BASE}/api/admin/styles/master:e2e-style", headers=adm,
                     json={"attrs": {"rules": "nope"}}).status_code == 422                     # bad shape
    # served style.json (any portal on the master catalog) includes it
    assert any(s.get("styleId") == "e2e-style" for s in httpx.get(f"{BASE}/api/portals/demo/style.json").json())
    assert httpx.post(f"{BASE}/api/admin/styles", headers={"Authorization": f"Bearer {admin_user}"},
                      json={"catalog": "master", "styleId": "x"}).status_code == 403           # non-admin
    assert httpx.request("DELETE", f"{BASE}/api/admin/styles/master:e2e-style", headers=adm).status_code == 200
    print("18f. styles CRUD: list/create/edit/get/delete; styleId immutable; bad shape=422; non-admin=403; served")

    # --- Portal lifecycle + config editing (create → settings → modules → delete)
    httpx.request("DELETE", f"{BASE}/api/admin/portals/e2e-portal", headers=adm)  # clean prior run
    r = httpx.post(f"{BASE}/api/admin/portals", headers=adm,
                   json={"slug": "e2e-portal", "title": "E2E Portal", "catalog": "basic",
                         "description": "created by the test"})
    assert r.status_code == 200, r.text
    # description column (delivered via Alembic migration) round-trips
    listed = {p["slug"]: p for p in httpx.get(f"{BASE}/api/admin/portals", headers=adm).json()}
    assert listed["e2e-portal"]["description"] == "created by the test"
    assert httpx.post(f"{BASE}/api/admin/portals", headers=adm,
                      json={"slug": "Bad Slug"}).status_code == 422          # bad slug
    assert httpx.post(f"{BASE}/api/admin/portals", headers=adm,
                      json={"slug": "e2e-portal"}).status_code == 409         # duplicate
    print("19. portal create from starter; bad slug=422, duplicate=409")

    httpx.patch(f"{BASE}/api/admin/portals/e2e-portal/settings", headers=adm,
                json={"description": "edited via settings"})
    assert httpx.get(f"{BASE}/api/admin/portals/e2e-portal/settings", headers=adm).json()["description"] == "edited via settings"
    st = httpx.patch(f"{BASE}/api/admin/portals/e2e-portal/settings", headers=adm, json={
        "title": {"text": "Renamed", "logo": "/l.png"},
        "map": {"startingMapMode": "3D", "startZoomLevel": 4},
        "controls": {"zoom": True, "fullScreen": True}}).json()
    assert st["title"]["text"] == "Renamed" and st["map"]["startingMapMode"] == "3D"
    assert st["controls"]["fullScreen"] and st["map"]["startZoomLevel"] == 4
    assert httpx.patch(f"{BASE}/api/admin/portals/e2e-portal/settings", headers=adm,
                       json={"map": {"startingMapMode": "4D"}}).status_code == 422
    served = httpx.get(f"{BASE}/api/portals/e2e-portal/config.json").json()["portalConfig"]
    assert served["mainMenu"]["title"]["text"] == "Renamed" and served["map"]["startingMapMode"] == "3D"
    print("20. settings PATCH (branding/map/controls) → draft → served; bad mode=422")

    mods = httpx.post(f"{BASE}/api/admin/portals/e2e-portal/modules", headers=adm,
                      json={"menu": "mainMenu", "type": "measure", "enabled": True}).json()
    assert "measure" in mods["modules"]["mainMenu"]
    mods = httpx.post(f"{BASE}/api/admin/portals/e2e-portal/modules", headers=adm,
                      json={"menu": "mainMenu", "type": "about", "enabled": False}).json()
    assert "about" not in mods["modules"]["mainMenu"]
    assert httpx.post(f"{BASE}/api/admin/portals/e2e-portal/modules", headers=adm,
                      json={"menu": "mainMenu", "type": "nope", "enabled": True}).status_code == 422
    # non-admin cannot mutate portals
    assert httpx.post(f"{BASE}/api/admin/portals", headers={"Authorization": f"Bearer {admin_user}"},
                      json={"slug": "x"}).status_code == 403
    print("21. module enable/disable; bad module=422; non-admin create=403")

    # raw portalConfig advanced editor (reachability escape hatch)
    raw = httpx.get(f"{BASE}/api/admin/portals/e2e-portal/portal-config", headers=adm).json()["portal_config"]
    raw.setdefault("mainMenu", {})["searchBar"] = {"minCharacters": 3,
        "searchInterfaces": [{"type": "gazetteer"}]}   # a field no form covers
    assert httpx.put(f"{BASE}/api/admin/portals/e2e-portal/portal-config", headers=adm,
                     json={"portal_config": raw}).status_code == 200
    served = httpx.get(f"{BASE}/api/portals/e2e-portal/config.json").json()["portalConfig"]
    assert "searchBar" in served["mainMenu"], "raw edit not served"
    assert httpx.put(f"{BASE}/api/admin/portals/e2e-portal/portal-config", headers=adm,
                     json={"portal_config": {"map": []}}).status_code == 422   # structural guard
    assert httpx.put(f"{BASE}/api/admin/portals/e2e-portal/portal-config",
                     headers={"Authorization": f"Bearer {admin_user}"},
                     json={"portal_config": {}}).status_code == 403
    print("22. raw portalConfig editor: uncovered field (searchBar) saved+served; bad shape=422; non-admin=403")

    assert httpx.request("DELETE", f"{BASE}/api/admin/portals/e2e-portal", headers=adm).status_code == 200
    assert httpx.get(f"{BASE}/api/portals/e2e-portal/config.json").status_code == 404
    print("23. portal delete removes it entirely")

    # cleanup: restore basic to live-unpublished + 453 public
    httpx.patch(f"{BASE}/api/admin/services/basic:453", headers=adm, json={"is_public": True})
    httpx.post(f"{BASE}/api/admin/portals/basic/activate", headers=adm, json={"version": None})

    # --- Phase 5: rate limiting. Unit-test the window, then prove the 429 path.
    from app.ratelimit import SlidingWindow
    w = SlidingWindow(3, 60)
    assert [w.check("a")[0] for _ in range(4)] == [True, True, True, False]
    assert w.check("a")[1] > 0 and w.check("b")[0] is True   # retry-after set; other caller free
    print("24. rate-limit window: 3 allowed then blocked (with Retry-After), per-caller isolated")

    # burst config.json past CONFIG_RATE_PER_MIN → some 429 with Retry-After (done last)
    codes = [httpx.get(f"{BASE}/api/portals/basic/config.json") for _ in range(340)]
    limited = [r for r in codes if r.status_code == 429]
    assert limited, "expected some 429s in a 340-request burst"
    assert "Retry-After" in limited[0].headers
    print(f"25. proxy/config rate limit: {len(limited)}/340 burst requests got 429 + Retry-After")
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
