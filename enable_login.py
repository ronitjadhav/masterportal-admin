"""Add the login button to a portal's menu and mark a service login-only.

Usage: python enable_login.py <portal-slug> [<service-id-to-secure> ...]
"""
import sys

from app.db import Portal, Service, SessionLocal, scoped_key
from sqlalchemy.orm.attributes import flag_modified


def main():
    slug = sys.argv[1]
    secure_ids = sys.argv[2:]
    with SessionLocal.begin() as db:
        portal = db.get(Portal, slug)
        if portal is None:
            sys.exit(f"unknown portal: {slug}")
        sections = portal.portal_config.setdefault("mainMenu", {}).setdefault("sections", [[]])
        if not any(e.get("type") == "login" for e in sections[0]):
            sections[0].insert(0, {"type": "login"})
            flag_modified(portal, "portal_config")
            print(f"added login module to portal {slug!r}")
        for sid in secure_ids:
            service = db.get(Service, scoped_key(portal.catalog, sid))
            if service is None:
                sys.exit(f"unknown service {sid!r} in catalog {portal.catalog!r}")
            service.is_public = False
            print(f"service {sid!r} ({service.attrs.get('name')}) now requires login")


if __name__ == "__main__":
    main()
