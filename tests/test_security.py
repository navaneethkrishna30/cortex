"""The three login invariants, listener separation, scopes and first-run setup."""

import base64
import hashlib
import json
import re

import pyotp
import pytest
from starlette.testclient import TestClient

from cortex.auth import AuthStore
from cortex.server import build_apps

PASSWORD = "correct horse battery staple"
REDIRECT = "http://localhost:9999/cb"
VERIFIER = "v" * 50
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def world(store, settings):
    auth = AuthStore(settings.auth_db)
    public, ui = build_apps(store, auth, settings)
    with TestClient(public, base_url="https://public") as public_client:
        ui_client = TestClient(ui, base_url="https://tailnet")
        yield auth, public_client, ui_client, store


def enrol(auth, ui):
    token = auth.new_setup_token()
    first = ui.post("/ui/setup", data={"setup_token": token, "password": PASSWORD, "confirm": PASSWORD})
    secret = re.search(r'<code id="totp-secret">([A-Z2-7]+)</code>', first.text).group(1)
    assert PASSWORD not in first.text
    done = ui.post("/ui/setup", data={"setup_token": token, "totp": pyotp.TOTP(secret).now()})
    assert done.status_code == 200 and auth.is_set_up
    return secret


def fresh_totp(auth, secret, step_offset=0):
    """A code not yet used: single-use steps mean tests must move forward in time."""
    import time

    auth.db.execute("DELETE FROM settings WHERE key='totp_last_step'")
    auth.db.commit()
    return pyotp.TOTP(secret).at(int(time.time()))


def start_login(public):
    client_id = public.post("/oauth/register", json={"client_name": "laptop", "redirect_uris": [REDIRECT]}).json()["client_id"]
    page = public.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "s1", "scope": "write",
    })
    code = re.search(r"<code>(.+?)</code>", page.text).group(1)
    return client_id, code


def approve(ui, auth, secret, code, scope="write"):
    page = ui.post("/ui/approve", data={"code": code, "totp": fresh_totp(auth, secret)})
    assert page.status_code == 200, page.text
    ticket = re.search(r'name="ticket" value="(.+?)"', page.text).group(1)
    ui.post("/ui/decide", data={"ticket": ticket, "decision": "approve", "scope": scope})


def rpc(client, token, method, params=None):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    if method != "initialize":
        headers["MCP-Protocol-Version"] = "2025-06-18"
    else:
        body["params"] = init
    response = client.post("/mcp", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_invariant_1_server_generates_codes_without_clashes(world):
    auth = world[0]
    client = auth.register_client("c", [REDIRECT], [REDIRECT])
    codes = {auth.create_login_request(client, REDIRECT, CHALLENGE, "write", None, f"ip{i}")[1] for i in range(15)}
    assert len(codes) == 15
    assert all(re.fullmatch(r"[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}", code) for code in codes)
    # There is no input through which a client could pick its own code.
    assert "code" not in world[1].get("/.well-known/oauth-authorization-server").json()


def test_invariant_2_token_goes_only_to_secret_holder(world):
    auth, public, ui, _ = world
    secret = enrol(auth, ui)
    client_id, code = start_login(public)
    # Someone else's browser, knowing the displayed code but not the request secret, gets nothing.
    stranger = TestClient(public.app, base_url="https://public")
    assert stranger.get("/oauth/poll").json() == {"status": "unknown"}
    assert public.get("/oauth/poll").json() == {"status": "pending"}
    approve(ui, auth, secret, code)
    assert stranger.get("/oauth/poll").json() == {"status": "unknown"}
    redirect = public.get("/oauth/poll").json()["redirect"]
    auth_code = re.search(r"code=([^&]+)", redirect).group(1)
    assert "state=s1" in redirect
    # A second poll must not hand out another code.
    assert public.get("/oauth/poll").json()["status"] in ("issued", "unknown")
    # Wrong verifier burns the code.
    form = {"grant_type": "authorization_code", "code": auth_code, "client_id": client_id, "redirect_uri": REDIRECT}
    assert public.post("/oauth/token", data={**form, "code_verifier": "wrong"}).status_code == 400
    assert public.post("/oauth/token", data={**form, "code_verifier": VERIFIER}).status_code == 400


def test_invariant_3_code_needs_tailnet_ui_login_and_totp(world):
    auth, public, ui, _ = world
    secret = enrol(auth, ui)
    _, code = start_login(public)
    # No public route takes a code.
    assert public.post("/ui/approve", data={"code": code, "totp": "000000"}).status_code == 404
    # A logged-out UI visitor is sent to login.
    anonymous = TestClient(ui.app, base_url="https://tailnet", follow_redirects=False)
    assert anonymous.post("/ui/approve", data={"code": code}).status_code == 303
    # A logged-in one still needs a correct TOTP.
    wrong = ui.post("/ui/approve", data={"code": code, "totp": "000000"})
    assert wrong.status_code == 401
    assert public.get("/oauth/poll").json() == {"status": "pending"}


def test_totp_codes_are_single_use(world):
    auth, _, ui, _ = world
    secret = enrol(auth, ui)
    code = fresh_totp(auth, secret)
    assert auth.check_totp(code) is True
    assert auth.check_totp(code) is False


def test_full_login_then_tools_then_refresh_rotation(world):
    auth, public, ui, store = world
    secret = enrol(auth, ui)
    client_id, code = start_login(public)
    approve(ui, auth, secret, code)
    redirect = public.get("/oauth/poll").json()["redirect"]
    auth_code = re.search(r"code=([^&]+)", redirect).group(1)
    tokens = public.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": auth_code, "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": VERIFIER,
    }).json()
    assert tokens["expires_in"] == 3600

    rpc(public, tokens["access_token"], "tools/call", {"name": "write", "arguments": {"path": "memory/oauth-works.md", "content": "OAuth works."}})
    assert store.git.run("log", "-1", "--format=%an").strip() == "laptop"

    rotated = public.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    # Reusing the old refresh token revokes the family.
    assert public.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).status_code == 400
    assert public.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rotated.json()["refresh_token"]}).status_code == 400


def test_ui_routes_absent_from_public_and_mcp_absent_from_ui(world):
    _, public, ui, _ = world
    for path in ("/ui", "/ui/login", "/ui/setup", "/ui/files", "/ui/static/easymde.min.js"):
        assert public.get(path).status_code in (401, 404), path
    assert ui.post("/mcp", json={}).status_code == 404
    assert ui.get("/oauth/authorize").status_code == 404


def test_mcp_requires_a_valid_token(world):
    _, public, _, _ = world
    response = public.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
    bad = public.post("/mcp", headers={**MCP_HEADERS, "Authorization": "Bearer nope"}, json={})
    assert bad.status_code == 401


def test_read_token_sees_and_calls_no_write_tools(world):
    auth, public, _, store = world
    reader, writer = auth.create_static_token("reader", "read"), auth.create_static_token("writer", "write")
    names = {t["name"] for t in rpc(public, reader, "tools/list")["result"]["tools"]}
    assert names == {"search", "read", "list"}
    assert len(rpc(public, writer, "tools/list")["result"]["tools"]) == 8
    denied = rpc(public, reader, "tools/call", {"name": "write", "arguments": {"path": "memory/x.md", "content": "x"}})
    assert denied.get("result", {}).get("isError") or "error" in denied
    assert not (store.vault.root / "memory/x.md").exists()


def test_instructions_carry_core_and_memory_and_update(world):
    auth, public, _, store = world
    token = auth.create_static_token("t", "write")
    assert "One fact, one place" in rpc(public, token, "initialize")["result"]["instructions"]
    store.write("memory/new-fact.md", "A brand new fact.", actor="t")
    assert "brand new fact" in rpc(public, token, "initialize")["result"]["instructions"]


def test_revoked_token_stops_working(world):
    auth, public, _, _ = world
    token = auth.create_static_token("t", "read")
    row = auth.check_bearer(token)
    auth.revoke(row["id"])
    assert auth.check_bearer(token) is None


def test_setup_needs_the_console_token_and_ends_once(world):
    auth, _, ui, _ = world
    auth.new_setup_token()
    assert ui.post("/ui/setup", data={"setup_token": "guess", "password": PASSWORD}).status_code == 403
    enrol(auth, ui)
    assert auth._get("setup_token_hash") is None
    assert "Log in" in ui.get("/ui/setup").text


def test_password_lockout_after_five_failures(world):
    auth, _, ui, _ = world
    enrol(auth, ui)
    for _ in range(5):
        auth.check_password("wrong")
    assert auth.locked_for("password") > 0
    assert auth.check_password(PASSWORD) is False


def test_pending_requests_evict_oldest_not_block(world):
    auth = world[0]
    client = auth.register_client("c", [REDIRECT], [REDIRECT])
    first_secret, _ = auth.create_login_request(client, REDIRECT, CHALLENGE, "read", None, "attacker")
    for _ in range(5):
        auth.create_login_request(client, REDIRECT, CHALLENGE, "read", None, "attacker")
    assert auth.poll(first_secret) == {"status": "evicted"}
    owner_secret, _ = auth.create_login_request(client, REDIRECT, CHALLENGE, "read", None, "owner")
    assert auth.poll(owner_secret) == {"status": "pending"}


def test_redirect_allowlist(world):
    _, public, _, _ = world
    assert public.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}).status_code == 400


def test_ui_core_edit_needs_confirmation_and_commits_as_human(world):
    auth, _, ui, store = world
    enrol(auth, ui)
    current = store.vault.hash("core/rules.md")
    data = {"path": "core/rules.md", "content": "# Rules\nEdited.\n", "hash": current}
    assert ui.post("/ui/files", data=data).status_code == 409
    assert ui.post("/ui/files", data={**data, "confirm_core": "yes"}).status_code == 200
    assert store.git.run("log", "-1", "--format=%an").strip() == "human"
    stale = ui.post("/ui/files", data={**data, "confirm_core": "yes"})
    assert stale.status_code == 409 and "stale" in stale.text.lower()


def test_ui_cross_origin_post_refused(world):
    auth, _, ui, _ = world
    enrol(auth, ui)
    assert ui.post("/ui/tokens", data={}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_setup_refuses_mismatched_passwords(world):
    auth, _, ui, _ = world
    token = auth.new_setup_token()
    page = ui.post("/ui/setup", data={"setup_token": token, "password": PASSWORD, "confirm": PASSWORD + "x"})
    assert page.status_code == 400 and "match" in page.text and not auth.is_set_up
