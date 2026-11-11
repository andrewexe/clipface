"""Secrets go to the keychain and nowhere else: not responses, logs, or the database."""

import logging
import sqlite3

import keyring
from keyring.backends import fail

SECRET = "sk-ant-test-9f8e7d6c5b4a3210-DO-NOT-LEAK"


def put(harness, name, value):
    return harness.client.put(
        f"/settings/secrets/{name}", json={"value": value}, headers=harness.auth
    )


def listed(harness):
    body = harness.client.get("/settings/secrets", headers=harness.auth).json()
    return {item["name"]: item for item in body["secrets"]}


def test_set_stores_in_keychain_and_reports_only_status(harness):
    assert listed(harness)["anthropic_api_key"]["is_set"] is False

    response = put(harness, "anthropic_api_key", SECRET)
    assert response.status_code == 200
    assert SECRET not in response.text
    assert harness.keyring.store[("clipper", "anthropic_api_key")] == SECRET

    listing = harness.client.get("/settings/secrets", headers=harness.auth)
    assert SECRET not in listing.text
    assert listed(harness)["anthropic_api_key"]["is_set"] is True


def test_value_is_trimmed(harness):
    assert put(harness, "twitch_client_id", f"  {SECRET}\n").status_code == 200
    assert harness.keyring.store[("clipper", "twitch_client_id")] == SECRET


def test_delete(harness):
    put(harness, "anthropic_api_key", SECRET)
    response = harness.client.delete("/settings/secrets/anthropic_api_key", headers=harness.auth)
    assert response.status_code == 200
    assert harness.keyring.store == {}
    assert listed(harness)["anthropic_api_key"]["is_set"] is False
    # Deleting again is a no-op, not an error.
    again = harness.client.delete("/settings/secrets/anthropic_api_key", headers=harness.auth)
    assert again.status_code == 200


def test_unknown_name_is_404_and_stores_nothing(harness):
    assert put(harness, "not_a_real_setting", SECRET).status_code == 404
    assert harness.keyring.store == {}


def test_invalid_values_rejected_without_echo(harness):
    for bad in ["", "   ", "has space inside", "x" * 1025]:
        response = put(harness, "anthropic_api_key", bad)
        assert response.status_code == 422, bad
    oversized = SECRET * 40
    response = put(harness, "anthropic_api_key", oversized)
    assert response.status_code == 422
    assert SECRET not in response.text
    assert harness.keyring.store == {}


def test_secret_never_reaches_logs(harness):
    put(harness, "anthropic_api_key", SECRET)
    assert "secret anthropic_api_key saved" in harness.log_text()
    assert SECRET not in harness.log_text()


def test_redactor_scrubs_accidental_logging(harness):
    put(harness, "anthropic_api_key", SECRET)
    log = logging.getLogger("some.future.module")
    log.warning("request failed with key=%s", SECRET)
    try:
        raise RuntimeError(f"upstream said: bad key {SECRET}")
    except RuntimeError:
        log.exception("traceback path")
    text = harness.log_text()
    assert SECRET not in text
    assert "key=[REDACTED]" in text
    assert "bad key [REDACTED]" in text


def test_secret_never_reaches_database_or_audit(harness):
    put(harness, "anthropic_api_key", SECRET)
    harness.client.delete("/settings/secrets/anthropic_api_key", headers=harness.auth)

    audit = harness.client.get("/audit", headers=harness.auth)
    assert SECRET not in audit.text
    actions = [(e["actor"], e["action"], e["object_id"]) for e in audit.json()["entries"]]
    assert actions == [
        ("user", "secret.delete", "anthropic_api_key"),
        ("user", "secret.set", "anthropic_api_key"),
    ]

    harness.ctx.db_engine.dispose()
    connection = sqlite3.connect(harness.ctx.config.db_path)
    dump = "\n".join(connection.iterdump())
    connection.close()
    assert SECRET not in dump
    for path in harness.ctx.config.data_dir.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


def test_logs_endpoint_is_redacted(harness):
    put(harness, "anthropic_api_key", SECRET)
    logging.getLogger("x").error("oops %s", SECRET)
    response = harness.client.get("/logs", headers=harness.auth)
    assert response.status_code == 200
    assert SECRET not in response.text
    assert any("[REDACTED]" in line for line in response.json()["lines"])


def test_no_keychain_returns_503_and_never_falls_back_to_disk(harness):
    keyring.set_keyring(fail.Keyring())
    health = harness.client.get("/health", headers=harness.auth).json()
    assert health["keychain"]["available"] is False

    assert put(harness, "anthropic_api_key", SECRET).status_code == 503
    assert listed(harness)["anthropic_api_key"]["is_set"] is False
    for path in harness.ctx.config.data_dir.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path
