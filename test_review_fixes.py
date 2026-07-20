"""Standalone checks for the security-review fixes — no Keycloak needed.

Covers the three fixes whose logic is unit-testable in isolation:
  1. proxy._is_transaction — encoding-robust WFS-T mutation gate (UTF-16 bypass)
  2. proxy._host_is_public — SSRF guard rejects CGNAT/shared address space
  3. configsrc.with_live_access — live is_public overlaid onto a frozen snapshot

Run: python test_review_fixes.py
"""
import os
import tempfile

_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db}"

from app.proxy import _host_is_public, _is_transaction          # noqa: E402
from app import configsrc                                        # noqa: E402
from app.db import Base, Service, SessionLocal, engine           # noqa: E402


def test_transaction_gate():
    tx = '<?xml version="1.0"?><wfs:Transaction xmlns:wfs="x"><wfs:Delete/></wfs:Transaction>'
    getfeature = '<?xml version="1.0"?><wfs:GetFeature xmlns:wfs="x"><wfs:Query/></wfs:GetFeature>'
    # plain UTF-8
    assert _is_transaction(tx.encode("utf-8")) is True
    assert _is_transaction(getfeature.encode("utf-8")) is False
    # the bypass the raw-bytes regex missed: UTF-16 (LE and BE) and UTF-32
    assert _is_transaction(tx.encode("utf-16")) is True, "UTF-16 transaction slipped the gate"
    assert _is_transaction(tx.encode("utf-16-be")) is True
    assert _is_transaction(tx.encode("utf-32")) is True
    # a legit UTF-16 GetFeature must still pass (no false positive)
    assert _is_transaction(getfeature.encode("utf-16")) is False
    assert _is_transaction(b"") is False


def test_ssrf_shared_address_space():
    # literal IPs → getaddrinfo returns them locally (no network needed)
    assert _host_is_public("100.100.100.200") is False, "CGNAT/shared space allowed"
    assert _host_is_public("100.64.0.1") is False
    assert _host_is_public("127.0.0.1") is False
    assert _host_is_public("169.254.169.254") is False
    assert _host_is_public("10.0.0.1") is False
    assert _host_is_public("8.8.8.8") is True          # genuinely public
    assert _host_is_public("100.128.0.1") is True      # just outside 100.64/10


def test_live_access_overlay():
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        db.add(Service(key="cat:42", catalog="cat", external_id="42",
                       position=0, attrs={"name": "L"}, is_public=False))  # secured LIVE
    # a stale snapshot that froze the service as public
    snapshot_source = {"services": [{"key": "cat:42", "external_id": "42",
                                     "attrs": {"name": "L"}, "is_public": True}]}
    with SessionLocal() as db:
        overlaid = configsrc.with_live_access(snapshot_source, db, "cat")
    assert overlaid["services"][0]["is_public"] is False, "live secure not applied over snapshot"
    # a key not present live keeps its frozen value (deleted-from-catalog case)
    snap2 = {"services": [{"key": "cat:gone", "external_id": "g",
                           "attrs": {}, "is_public": True}]}
    with SessionLocal() as db:
        assert configsrc.with_live_access(snap2, db, "cat")["services"][0]["is_public"] is True


def main():
    test_transaction_gate()
    test_ssrf_shared_address_space()
    test_live_access_overlay()
    print("review fixes: mutation-gate encoding-robust; SSRF blocks CGNAT; live is_public overlays snapshot")


if __name__ == "__main__":
    try:
        main()
    finally:
        os.unlink(_db)
