"""Runs the real engine as a child process, the way the desktop app does.

Covers the Phase 0 checks: health returns OK, a saved key survives a restart, and the
key never appears in logs or process output.

Set CLIPPER_ENGINE_BIN to a PyInstaller-built engine to run the frozen-binary smoke test.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
TOKEN = "e2e-" + "0123456789abcdef" * 4
SECRET = "sk-ant-e2e-7c1d9a44b0f2-DO-NOT-LEAK"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class EngineProcess:
    def __init__(self, data_dir: Path, command: list[str] | None = None, env: dict | None = None):
        full_env = {
            **os.environ,
            "CLIPPER_SESSION_TOKEN": TOKEN,
            "CLIPPER_DATA_DIR": str(data_dir),
            **(env or {}),
        }
        self.proc = subprocess.Popen(
            [*(command or [sys.executable, "-m", "engine"]), "--exit-on-stdin-close"],
            cwd=REPO,
            env=full_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.first_line = self.proc.stdout.readline().strip()
        self.port = None
        if self.first_line.startswith("CLIPPER_ENGINE_READY "):
            self.port = json.loads(self.first_line.split(" ", 1)[1])["port"]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> tuple[int, str]:
        """Close stdin (what the app does on exit) and return (exit code, all output)."""
        self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise
        return code, self.first_line + self.proc.stdout.read() + self.proc.stderr.read()


@pytest.fixture
def keyring_env(tmp_path):
    return {
        "PYTHON_KEYRING_BACKEND": "tests.fake_keyring.FileKeyring",
        "CLIPPER_TEST_KEYRING_FILE": str(tmp_path / "fake-keychain.json"),
    }


def test_secret_survives_restart_and_never_logged(tmp_path, keyring_env):
    data_dir = tmp_path / "data"

    first = EngineProcess(data_dir, env=keyring_env)
    assert first.port, first.first_line
    health = httpx.get(f"{first.url}/health", headers=AUTH).json()
    assert health["status"] == "ok"
    assert health["db"]["revision"] == "0003"
    saved = httpx.put(
        f"{first.url}/settings/secrets/anthropic_api_key", json={"value": SECRET}, headers=AUTH
    )
    assert saved.status_code == 200
    code, output_1 = first.stop()
    assert code == 0  # stdin close is a clean shutdown

    second = EngineProcess(data_dir, env=keyring_env)
    assert second.port and second.port != 0
    listing = httpx.get(f"{second.url}/settings/secrets", headers=AUTH)
    by_name = {item["name"]: item for item in listing.json()["secrets"]}
    assert by_name["anthropic_api_key"]["is_set"] is True
    assert by_name["twitch_client_id"]["is_set"] is False
    assert SECRET not in listing.text
    _, output_2 = second.stop()

    for output in (output_1, output_2):
        assert SECRET not in output
        assert TOKEN not in output
    files = [p for p in data_dir.rglob("*") if p.is_file()]
    assert any(p.name == "engine.log" for p in files)
    for path in files:
        content = path.read_bytes()
        assert SECRET.encode() not in content, path
        assert TOKEN.encode() not in content, path


def test_requires_session_token(tmp_path):
    engine = EngineProcess(tmp_path / "data", env={"CLIPPER_SESSION_TOKEN": ""})
    assert engine.first_line.startswith("CLIPPER_ENGINE_ERROR")
    assert engine.proc.wait(timeout=10) == 2
    assert not (tmp_path / "data").exists()


def test_listens_on_loopback_only(tmp_path, keyring_env):
    engine = EngineProcess(tmp_path / "data", env=keyring_env)
    try:
        assert httpx.get(f"{engine.url}/health").status_code == 401
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            lan_ip = probe.getsockname()[0]
        except OSError:
            lan_ip = None
        finally:
            probe.close()
        if not lan_ip or lan_ip.startswith("127."):
            pytest.skip("no non-loopback interface to probe")
        with pytest.raises(OSError):
            socket.create_connection((lan_ip, engine.port), timeout=2).close()
    finally:
        engine.stop()


@pytest.mark.skipif(not os.environ.get("CLIPPER_ENGINE_BIN"), reason="CLIPPER_ENGINE_BIN not set")
def test_frozen_engine_smoke(tmp_path):
    engine = EngineProcess(tmp_path / "data", command=[os.environ["CLIPPER_ENGINE_BIN"]])
    assert engine.port, engine.first_line
    try:
        health = httpx.get(f"{engine.url}/health", headers=AUTH).json()
        assert health["status"] == "ok"
        assert health["db"]["revision"] == "0003"  # migrations were bundled and ran
        listing = httpx.get(f"{engine.url}/settings/secrets", headers=AUTH)
        assert listing.status_code == 200
    finally:
        code, _ = engine.stop()
    assert code == 0
