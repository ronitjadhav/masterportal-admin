"""Smoke-test the admin BFF login end-to-end (the one flow the e2e's bearer
tokens don't exercise). Drives a real OIDC code+PKCE login through the one
origin and asserts (1) a genuine admin login succeeds and (2) a forged callback
without the matching state cookie is refused — the login-CSRF guard.

Needs the FULL one-origin stack up (portal, backend, Keycloak behind nginx):
    sh deploy/gen-certs.sh
    docker compose -f docker-compose.prod.yml up -d --build
Then: python smoke_login.py [https://localhost:9001] [admin-demo] [admin-demo]

Not in CI: it needs nginx + the whole stack and scrapes Keycloak's login form
(brittle across IdP versions). Run it by hand after a deploy.
"""
import re
import sys
import warnings

import httpx

warnings.filterwarnings("ignore")   # self-signed demo cert

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://localhost:9001"
USER = sys.argv[2] if len(sys.argv) > 2 else "admin-demo"
PW = sys.argv[3] if len(sys.argv) > 3 else "admin-demo"


def main():
    # Positive: a real admin login must still succeed with the state cookie.
    c = httpx.Client(verify=False, follow_redirects=True, timeout=30)
    r = c.get(f"{BASE}/admin/login")
    assert r.status_code == 200, f"login redirect chain ended {r.status_code}"
    m = re.search(r'action="([^"]+login-actions/authenticate[^"]+)"', r.text)
    assert m, f"no Keycloak login form (redirect_uri rejected?): {r.text[:200]}"
    assert "admin_login_state" in c.cookies, "state cookie not set at /admin/login"
    c.post(m.group(1).replace("&amp;", "&"),
           data={"username": USER, "password": PW, "credentialId": ""})
    me = c.get(f"{BASE}/api/admin/me")
    assert me.status_code == 200, f"/api/admin/me after login = {me.status_code}: {me.text[:200]}"
    assert "admin_session" in c.cookies, "no session cookie after login"
    print("login OK — /api/admin/me:", me.json())

    # Negative: a callback without the matching state cookie is refused (CSRF).
    c2 = httpx.Client(verify=False, follow_redirects=False, timeout=30)
    r3 = c2.get(f"{BASE}/admin/callback", params={"code": "x", "state": "y"})
    assert r3.status_code == 400, f"forged callback should be 400, got {r3.status_code}"
    print("CSRF guard OK — forged callback ->", r3.status_code, r3.json().get("detail"))
    print("BFF login smoke test PASSED")


if __name__ == "__main__":
    main()
