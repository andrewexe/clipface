"""Alembic environment. The engine passes an open connection; see engine.db.session."""

from alembic import context

from engine.db.models import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("run migrations through engine.db.session.run_migrations()")

context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
