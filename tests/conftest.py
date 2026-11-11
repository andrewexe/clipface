from __future__ import annotations

import logging
from dataclasses import dataclass

import keyring
import pytest
from fastapi.testclient import TestClient

from engine.api.app import AppContext, create_app
from engine.config import Config
from engine.db.session import create_db_engine, run_migrations
from engine.keychain import SecretStore
from engine.logging_setup import SecretRedactor, setup_logging
from tests.fake_keyring import MemoryKeyring

TOKEN = "t" * 64


@dataclass
class Harness:
    client: TestClient
    ctx: AppContext
    keyring: MemoryKeyring
    redactor: SecretRedactor

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    def log_text(self) -> str:
        for handler in logging.getLogger().handlers:
            handler.flush()
        return self.ctx.config.log_file.read_text(encoding="utf-8")


@pytest.fixture
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def harness(tmp_path, memory_keyring) -> Harness:
    config = Config(data_dir=tmp_path / "data")
    config.ensure_dirs()
    redactor = SecretRedactor()
    setup_logging(config, redactor, level=logging.DEBUG)
    db_engine = create_db_engine(config.db_path)
    run_migrations(db_engine)
    ctx = AppContext(config=config, token=TOKEN, db_engine=db_engine, secrets=SecretStore(redactor))
    client = TestClient(create_app(ctx), base_url="http://127.0.0.1")
    yield Harness(client=client, ctx=ctx, keyring=memory_keyring, redactor=redactor)
    db_engine.dispose()


@pytest.fixture
def anyio_backend():
    return "asyncio"
