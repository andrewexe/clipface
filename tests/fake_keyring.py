"""Keyring backends for tests only. Never used by the app.

MemoryKeyring keeps values in a dict. FileKeyring persists to the JSON file named by
CLIPPER_TEST_KEYRING_FILE so a subprocess test can prove a secret survives an engine
restart; select it with PYTHON_KEYRING_BACKEND=tests.fake_keyring.FileKeyring.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import keyring.errors
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1
    name = "test memory keyring"

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[(service, username)]


class FileKeyring(KeyringBackend):
    priority = 1
    name = "test file keyring"

    @property
    def _path(self) -> Path:
        return Path(os.environ["CLIPPER_TEST_KEYRING_FILE"])

    def _load(self) -> dict[str, str]:
        return json.loads(self._path.read_text()) if self._path.exists() else {}

    def get_password(self, service, username):
        return self._load().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        data = self._load()
        data[f"{service}/{username}"] = password
        self._path.write_text(json.dumps(data))

    def delete_password(self, service, username):
        data = self._load()
        if f"{service}/{username}" not in data:
            raise keyring.errors.PasswordDeleteError("not found")
        del data[f"{service}/{username}"]
        self._path.write_text(json.dumps(data))
