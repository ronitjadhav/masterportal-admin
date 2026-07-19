"""Manage role grants (deny-by-default; the `admin` role always passes).

Usage:
  python grant.py service <catalog> <external_id> <role> [<role> ...]
  python grant.py module <portal_slug> <module_type> <role> [<role> ...]
  python grant.py portal <portal_slug> <role> [<role> ...]
  python grant.py list
"""
import sys

from app.db import (ModuleRole, PortalRole, ServiceRole, SessionLocal,
                    init_db, scoped_key)


def main():
    init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    with SessionLocal.begin() as db:
        if cmd == "service":
            key = scoped_key(sys.argv[2], sys.argv[3])
            for role in sys.argv[4:]:
                db.merge(ServiceRole(service_key=key, role=role))
            print(f"service {key}: granted to {sys.argv[4:]}")
        elif cmd == "module":
            slug, module = sys.argv[2], sys.argv[3]
            for role in sys.argv[4:]:
                db.merge(ModuleRole(portal_slug=slug, module_type=module, role=role))
            print(f"module {module!r} in portal {slug!r}: restricted to {sys.argv[4:]}")
        elif cmd == "portal":
            slug = sys.argv[2]
            for role in sys.argv[3:]:
                db.merge(PortalRole(portal_slug=slug, role=role))
            print(f"portal {slug!r}: restricted to {sys.argv[3:]}")
        elif cmd == "list":
            for r in db.query(ServiceRole):
                print(f"service {r.service_key} -> {r.role}")
            for r in db.query(ModuleRole):
                print(f"module {r.portal_slug}/{r.module_type} -> {r.role}")
            for r in db.query(PortalRole):
                print(f"portal {r.portal_slug} -> {r.role}")
        else:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
