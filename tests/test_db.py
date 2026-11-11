"""Migrations build the planned schema, are repeatable, and match the models."""

from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from engine.db.audit import write_audit
from engine.db.models import AuditLog, Base, Streamer, StreamSession
from engine.db.session import create_db_engine, make_session_factory, run_migrations

TABLES = {"streamers", "sessions", "moments", "clips", "accounts", "publish_jobs", "audit_log"}


@pytest.fixture
def db(tmp_path):
    engine = create_db_engine(tmp_path / "test.db")
    yield engine
    engine.dispose()


def test_fresh_database_gets_all_tables(db):
    assert run_migrations(db) == "0001"
    assert set(inspect(db).get_table_names()) == TABLES | {"alembic_version"}


def test_migrations_are_idempotent_and_keep_data(db):
    run_migrations(db)
    factory = make_session_factory(db)
    with factory.begin() as session:
        session.add(Streamer(platform="twitch", username="someone"))
    assert run_migrations(db) == "0001"
    with factory() as session:
        assert session.query(Streamer).count() == 1


def test_models_match_migrations(db):
    run_migrations(db)
    with db.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_path_with_spaces_and_percent(tmp_path):
    folder = tmp_path / "Andrew's PC 100%"
    folder.mkdir()
    engine = create_db_engine(folder / "clipper.db")
    try:
        assert run_migrations(engine) == "0001"
    finally:
        engine.dispose()


def test_constraints_enforced(db):
    run_migrations(db)
    factory = make_session_factory(db)

    with pytest.raises(IntegrityError), factory.begin() as session:
        session.add(Streamer(platform="youtube", username="x"))  # not a source platform

    with pytest.raises(IntegrityError), factory.begin() as session:
        session.add(Streamer(platform="kick", username="x", permission_status="sure"))

    with pytest.raises(IntegrityError), factory.begin() as session:  # foreign keys are on
        session.add(StreamSession(streamer_id=999, started_at=datetime.now(UTC)))

    with factory.begin() as session:
        session.add(Streamer(platform="kick", username="dup"))
    with pytest.raises(IntegrityError), factory.begin() as session:
        session.add(Streamer(platform="kick", username="dup"))


def test_streamer_defaults_are_safe(db):
    run_migrations(db)
    factory = make_session_factory(db)
    with factory.begin() as session:
        session.add(Streamer(platform="twitch", username="new"))
    with factory() as session:
        streamer = session.query(Streamer).one()
        assert streamer.permission_status == "none"  # so auto-publish is off by default
        assert streamer.added_at.tzinfo is not None


def test_audit_helper(db):
    run_migrations(db)
    factory = make_session_factory(db)
    with factory.begin() as session:
        write_audit(session, actor="system", action="engine.start", details={"v": 1})
    with factory() as session:
        entry = session.query(AuditLog).one()
        assert (entry.actor, entry.action, entry.details) == ("system", "engine.start", {"v": 1})
    with pytest.raises(ValueError):
        write_audit(factory(), actor="hacker", action="x")


def test_naive_datetimes_rejected(db):
    run_migrations(db)
    factory = make_session_factory(db)
    with pytest.raises(Exception, match="naive datetime"), factory.begin() as session:
        session.add(Streamer(platform="twitch", username="t", added_at=datetime(2026, 1, 1)))
