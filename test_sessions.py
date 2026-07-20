"""Standalone check for the DB-backed admin session store — no Keycloak needed.

Proves the property that motivated moving sessions out of a process dict:
sessions and in-flight logins survive a restart, and the expiry-cleanup + GC
queries used by the login/callback endpoints do what they claim.

Run: python test_sessions.py
"""
import os
import tempfile
import time

_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db}"

from app.db import AdminPendingLogin, AdminSession, Base, SessionLocal, engine  # noqa: E402
from app.admin import SESSION_GC_GRACE                                          # noqa: E402


def main():
    Base.metadata.create_all(engine)
    now = int(time.time())

    with SessionLocal.begin() as db:
        db.add(AdminPendingLogin(state="s1", verifier="v1", created=now))
        db.add(AdminPendingLogin(state="old", verifier="v0", created=now - 1000))     # expired
        db.add(AdminSession(sid="sid1", access="a", refresh="r", exp=now + 300))
        db.add(AdminSession(sid="dead", access="a", refresh=None,
                            exp=now - SESSION_GC_GRACE - 1))                           # GC-able

    # Simulate a RESTART: a brand-new session/connection must still see the rows
    # (the whole point — a process dict would have lost them).
    with SessionLocal() as db:
        assert db.get(AdminSession, "sid1") is not None, "session lost across restart"
        assert db.get(AdminPendingLogin, "s1") is not None, "pending login lost across restart"

    # pending-login cleanup (mirrors admin_login): >600s old removed, fresh kept
    with SessionLocal.begin() as db:
        db.query(AdminPendingLogin).filter(AdminPendingLogin.created < now - 600).delete()
    with SessionLocal() as db:
        assert db.get(AdminPendingLogin, "old") is None, "expired pending login not cleaned"
        assert db.get(AdminPendingLogin, "s1") is not None, "fresh pending login wrongly cleaned"

    # session GC (mirrors admin_callback): far-expired removed, live kept
    with SessionLocal.begin() as db:
        db.query(AdminSession).filter(AdminSession.exp < now - SESSION_GC_GRACE).delete()
    with SessionLocal() as db:
        assert db.get(AdminSession, "dead") is None, "dead session not GC'd"
        assert db.get(AdminSession, "sid1") is not None, "live session wrongly GC'd"

    print("session store: persists across restart; pending-login expiry + session GC OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        os.unlink(_db)
