"""What the container needs: a health route the watchdog can probe, and a configurable bind host."""

from starlette.testclient import TestClient

from cortex.auth import AuthStore
from cortex.config import load_settings
from cortex.server import build_apps


def test_healthz_answers_on_the_ui_listener_without_login(store, settings):
    _, ui = build_apps(store, AuthStore(settings.auth_db), settings)
    reply = TestClient(ui, base_url="https://tailnet").get("/healthz")
    assert reply.status_code == 200
    assert reply.json()["ok"] is True


def test_healthz_is_not_on_the_public_listener(store, settings):
    public, _ = build_apps(store, AuthStore(settings.auth_db), settings)
    with TestClient(public, base_url="https://public") as client:
        assert client.get("/healthz").status_code in (401, 404)


def test_host_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_HOST", "0.0.0.0")
    assert load_settings(tmp_path).host == "0.0.0.0"
    monkeypatch.delenv("CORTEX_HOST")
    assert load_settings(tmp_path).host == "127.0.0.1"
