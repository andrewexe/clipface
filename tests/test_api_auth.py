"""The engine API requires the session token on every route and only answers loopback."""

import pytest

ROUTES = ["/health", "/settings/secrets", "/audit", "/logs"]


@pytest.mark.parametrize("path", ROUTES)
def test_rejects_missing_token(harness, path):
    assert harness.client.get(path).status_code == 401


@pytest.mark.parametrize(
    "header",
    ["Bearer wrong", "Bearer ", "Basic " + "t" * 64, "t" * 64, "Bearer " + "t" * 63],
)
def test_rejects_bad_token(harness, header):
    response = harness.client.get("/health", headers={"Authorization": header})
    assert response.status_code == 401


def test_health_ok(harness):
    response = harness.client.get("/health", headers=harness.auth)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == {"ok": True, "revision": "0001"}
    assert body["keychain"]["available"] is True


def test_rejects_non_loopback_host_header(harness):
    response = harness.client.get("/health", headers={**harness.auth, "Host": "evil.example.com"})
    assert response.status_code == 400


def test_no_docs_or_schema_exposed(harness):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert harness.client.get(path, headers=harness.auth).status_code == 404


def test_cors_allows_app_origin_only(harness):
    def preflight(origin):
        return harness.client.options(
            "/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    allowed = preflight("http://tauri.localhost")
    assert allowed.headers.get("access-control-allow-origin") == "http://tauri.localhost"
    blocked = preflight("https://evil.example.com")
    assert "access-control-allow-origin" not in blocked.headers
