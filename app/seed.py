"""First-run seed. On an empty database, import the bundled `basic` portal
(seed/basic/) so a fresh clone is immediately useful — one small, public,
all-layers-visible starting point. Idempotent: does nothing if any portal
already exists.
"""
import json
import os

from .db import Portal, RestService, Service, SessionLocal, Style, scoped_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_DIR = os.path.join(ROOT, "seed", "basic")


def _load(*parts):
    with open(os.path.join(SEED_DIR, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def seed_if_empty():
    with SessionLocal() as db:
        if db.query(Portal).count() > 0:
            return
    if not os.path.isdir(SEED_DIR):
        return
    config = _load("config.json")
    services = _load("resources", "services.json")
    rest = _load("resources", "rest-services.json")
    styles = _load("resources", "style_v3.json")
    with SessionLocal.begin() as db:
        for pos, s in enumerate(services):
            if "id" not in s:
                continue
            db.merge(Service(key=scoped_key("basic", str(s["id"])), catalog="basic",
                             external_id=str(s["id"]), position=pos, attrs=s,
                             is_public=True, allow_transactions=False))
        for pos, r in enumerate(rest):
            db.merge(RestService(key=scoped_key("basic", str(r["id"])), catalog="basic",
                                 position=pos, attrs=r))
        for pos, st in enumerate(styles):
            db.merge(Style(key=scoped_key("basic", str(st["styleId"])), catalog="basic",
                           position=pos, attrs=st))
        db.merge(Portal(slug="basic", catalog="basic",
                        description="Starting-point portal (bundled seed)",
                        portal_config=config["portalConfig"],
                        layer_config=config["layerConfig"], active_snapshot_id=None))
    print("seeded 'basic' portal from bundled seed (empty database)")
