"""Database setup and models.

ponytail: SQLite by default so learning needs zero infrastructure.
Set DATABASE_URL=postgresql+psycopg://... when moving to Postgres — nothing else changes.
"""
import os

from sqlalchemy import JSON, Boolean, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///admin.db")

# SQLite connections are cheap — NullPool sidesteps pool-exhaustion entirely.
# Postgres gets a real pool sized generously for the proxy's concurrency.
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy.pool import NullPool
    engine = create_engine(DATABASE_URL, poolclass=NullPool)
else:
    engine = create_engine(DATABASE_URL, pool_size=20, max_overflow=30, pool_timeout=10)

SessionLocal = sessionmaker(bind=engine)


def scoped_key(catalog: str, external_id: str) -> str:
    """Internal key for catalog-scoped rows: '<catalog>:<external_id>'.

    Two catalogs may legitimately reuse the same external id (services.json
    ids are only unique within one source file), so external ids alone can
    never be primary keys or proxy paths.
    """
    return f"{catalog}:{external_id}"


class Base(DeclarativeBase):
    pass


class Service(Base):
    """One services.json entry (a layer/service in a catalog)."""

    __tablename__ = "services"

    key: Mapped[str] = mapped_column(String, primary_key=True)  # scoped_key()
    catalog: Mapped[str] = mapped_column(String, index=True)
    external_id: Mapped[str] = mapped_column(String)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # Whole services.json entry as one JSON blob. attrs["url"] is the REAL
    # upstream URL — it never leaves the server; clients only see /geo/<key>.
    attrs: Mapped[dict] = mapped_column(JSON)
    # False → login required to load the layer (Phase 3 adds per-role grants).
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    # Name of an env var holding "user:password" for upstream basic auth.
    # Must start with UPSTREAM_SECRET_ (see proxy.py) — only the env var NAME
    # is stored, the secret itself stays outside the DB.
    upstream_auth_env: Mapped[str | None] = mapped_column(String, nullable=True)
    # False → POST bodies containing WFS <Transaction> and POSTs to sub-paths
    # (OGC API create/update) are rejected. Mutating upstreams are opt-in.
    allow_transactions: Mapped[bool] = mapped_column(Boolean, default=False)


class RestService(Base):
    """One rest-services.json entry (print, CSW, geocoder, ...)."""

    __tablename__ = "rest_services"

    key: Mapped[str] = mapped_column(String, primary_key=True)  # scoped_key()
    catalog: Mapped[str] = mapped_column(String, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    attrs: Mapped[dict] = mapped_column(JSON)


class Style(Base):
    """One style_v3.json entry."""

    __tablename__ = "styles"

    key: Mapped[str] = mapped_column(String, primary_key=True)  # scoped_key()
    catalog: Mapped[str] = mapped_column(String, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    attrs: Mapped[dict] = mapped_column(JSON)


class Portal(Base):
    """One Masterportal instance: its UI config, tree, and its catalog.

    portal_config / layer_config are the editable DRAFT. What portal users
    actually receive comes from active_snapshot_id when set (publish/rollback);
    a null pointer serves the live draft (handy in dev).
    """

    __tablename__ = "portals"

    slug: Mapped[str] = mapped_column(String, primary_key=True)
    # Which catalog this portal's services/styles/rest-services come from.
    # Several portals may share one catalog.
    catalog: Mapped[str] = mapped_column(String, index=True)
    # Free-text admin note shown in the console (added via migration 2 — the
    # first schema change delivered through Alembic instead of rm admin.db).
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    portal_config: Mapped[dict] = mapped_column(JSON)
    # ponytail: layer tree stored as the raw config.json "layerConfig" blob;
    # normalize into tree_node rows when the tree editor needs it.
    layer_config: Mapped[dict] = mapped_column(JSON)
    active_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Snapshot(Base):
    """An immutable, published render-input for a portal.

    `data` freezes everything needed to serve (portal_config, layer_config,
    services with their attrs+is_public, styles, rest_services) so a later
    catalog edit — or deletion — can never invalidate a rollback. Role
    filtering and the proxy's authz stay LIVE on top; only *what exists and
    how it's arranged* is frozen, never *who may see it*.
    """

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_slug: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer)
    created_ts: Mapped[str] = mapped_column(String)
    created_by: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON)


class ServiceRole(Base):
    """Grant: role may use a secured service. A secured service with no rows
    is accessible only to the `admin` role (deny-by-default)."""

    __tablename__ = "service_roles"

    service_key: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)


class PortalRole(Base):
    """If any rows exist for a portal, only those roles (and admin) may load
    its configs. No rows → portal is open."""

    __tablename__ = "portal_roles"

    portal_slug: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)


class ModuleRole(Base):
    """If any rows exist for (portal, module type), that menu module is only
    served to those roles (and admin). No rows → module is open."""

    __tablename__ = "module_roles"

    portal_slug: Mapped[str] = mapped_column(String, primary_key=True)
    module_type: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)


class AuditLog(Base):
    """Append-only record of every admin mutation."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String)      # ISO-8601 UTC
    actor: Mapped[str] = mapped_column(String)   # token sub / username
    action: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON)


def init_db():
    """Bring the schema to head via Alembic (idempotent) — replaces
    create_all, so every schema change ships as a migration and existing data
    survives upgrades. A fresh DB gets all tables from the initial migration.

    ponytail: run on startup for single-worker dev/small deployments; a
    multi-worker deploy should run `alembic upgrade head` as a separate step.
    """
    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    command.upgrade(cfg, "head")


def db_session():
    """FastAPI dependency yielding a session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
