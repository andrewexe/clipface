"""App settings: validated, persisted, audited; never a place for secrets."""

import json

from engine.settings import AppSettings, SettingsStore


def test_defaults_match_the_plan(tmp_path):
    settings = SettingsStore(tmp_path / "s.json").get()
    assert (settings.buffer_minutes, settings.segment_seconds) == (20, 10)
    assert settings.raw_retention_hours == 6


def test_update_persists_and_reports_changes(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    settings, changes = store.update({"buffer_minutes": 30, "segment_seconds": 10})
    assert settings.buffer_minutes == 30
    assert changes == {"buffer_minutes": {"from": 20, "to": 30}}
    assert SettingsStore(tmp_path / "s.json").get().buffer_minutes == 30


def test_damaged_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert SettingsStore(path).get() == AppSettings()
    path.write_text(json.dumps({"buffer_minutes": 99999, "unknown": 1}))
    assert SettingsStore(path).get() == AppSettings()


def test_api_get_patch_validate_audit(harness):
    got = harness.client.get("/settings/app", headers=harness.auth).json()
    assert got["disk_cap_gb"] == 50

    ok = harness.client.patch("/settings/app", json={"disk_cap_gb": 120}, headers=harness.auth)
    assert ok.status_code == 200 and ok.json()["disk_cap_gb"] == 120

    for bad in (
        {"disk_cap_gb": 0},
        {"buffer_minutes": "lots"},
        {"nope": 1},
        {"stream_quality": "../x"},
    ):
        response = harness.client.patch("/settings/app", json=bad, headers=harness.auth)
        assert response.status_code == 422, bad
    assert harness.client.get("/settings/app", headers=harness.auth).json()["disk_cap_gb"] == 120

    entries = harness.client.get("/audit", headers=harness.auth).json()["entries"]
    assert [e["action"] for e in entries] == ["settings.update"]
    assert entries[0]["details"] == {"disk_cap_gb": {"from": 50.0, "to": 120.0}}


def test_safety_switches_cannot_be_changed_through_general_settings(harness):
    for patch in ({"autonomy_level": "L2"}, {"publishing_paused": True},
                  {"telegram_chat_id": 12345}, {"autonomy_l2_since": "2020-01-01T00:00:00+00:00"}):  # fmt: skip
        response = harness.client.patch("/settings/app", json=patch, headers=harness.auth)
        assert response.status_code == 422 and "its own control" in response.json()["detail"]
    settings = harness.ctx.settings.get()
    assert settings.autonomy_level == "L1" and settings.telegram_chat_id == 0
    assert settings.publishing_paused is False
