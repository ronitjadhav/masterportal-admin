"""Alembic environment — targets the app's own metadata + DATABASE_URL.

render_as_batch=True so SQLite can emulate ALTER TABLE (add/drop column)
via table copy; harmless on Postgres.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.db import DATABASE_URL, Base
import app.db  # noqa: F401  (import so every model is registered on Base.metadata)

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=DATABASE_URL, target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = create_engine(DATABASE_URL)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
