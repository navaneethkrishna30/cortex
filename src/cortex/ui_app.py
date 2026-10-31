"""The tailnet listener: /ui only. Reached through `tailscale serve`, never a public tunnel."""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import pyotp
import segno
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import AuthError, AuthStore
from .config import Settings
from .gitrepo import HUMAN
from .store import Store
from .vault import VaultError

MIN_PASSWORD = 12
COOKIE = "cortex_ui"
templates = Environment(loader=PackageLoader("cortex", "templates"), autoescape=select_autoescape(["html"]))
templates.filters["when"] = lambda t: time.strftime("%d %b %Y, %H:%M", time.localtime(t))

# Audit events as a person would say them; `bad` marks the ones worth noticing.
EVENTS = {
    "setup_complete": ("Setup finished", False),
    "setup_failed": ("Wrong setup token", True),
    "auth_reset": ("Login reset from the server", True),
    "ui_login": ("Logged in", False),
    "login_failed": ("Wrong password", True),
    "totp_failed": ("Wrong authenticator code", True),
    "totp_reenrolled": ("Authenticator changed", False),
    "token_created": ("Token created", False),
    "token_revoked": ("Access revoked", False),
    "token_failed": ("Client sign-in refused", True),
    "refresh_reuse": ("Reused refresh token; client cut off", True),
    "login_approved": ("Client approved", False),
    "login_denied": ("Client denied", False),
    "code_rejected": ("Unknown login code", True),
}
FOLDER_HINTS = {
    "core": "Loaded every session",
    "memory": "One fact per file",
    "entities": "Cards for things that change",
    "docs": "Read only when asked",
    "logs": "Append-only",
}


def _ago(moment: float) -> str:
    seconds = int(time.time() - moment)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


templates.filters["ago"] = _ago
templates.filters["event_label"] = lambda name: EVENTS.get(name, (name, False))[0]
templates.filters["event_bad"] = lambda name: EVENTS.get(name, (name, False))[1]


def _tree(paths: list[str]) -> list[dict]:
    """Group notes under their top folder, in the vault's loading order."""
    tree = []
    for folder, hint in FOLDER_HINTS.items():
        files = [{"path": p, "name": p[len(folder) + 1 : -3]} for p in paths if p.startswith(folder + "/")]
        tree.append({"name": folder, "hint": hint, "files": files})
    return tree


def _qr_data_uri(uri: str) -> str:
    return segno.make(uri).svg_data_uri(scale=6, border=2)


def build_ui_app(auth: AuthStore, store: Store, settings: Settings) -> Starlette:
    # Setups waiting for their first confirming code: setup token -> (password hash, TOTP secret).
    # Nothing is saved until the code matches, and the password never goes back to the browser.
    pending_setup: dict[str, tuple[str, str]] = {}
    pending_totp: dict[str, str] = {}
    # Requests whose code + TOTP have passed, waiting for the final Confirm press.
    confirmable: dict[str, int] = {}

    def page(name: str, status: int = 200, **context) -> HTMLResponse:
        return HTMLResponse(templates.get_template(name).render(**context), status)

    def ip(request: Request) -> str:
        return request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0]

    def logged_in(request: Request) -> bool:
        return auth.check_session(request.cookies.get(COOKIE))

    def same_origin(request: Request) -> bool:
        """Backs up SameSite=Strict for every state-changing request."""
        origin = request.headers.get("origin")
        if not origin:
            return True
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        return origin.split("://", 1)[-1] == host

    def guarded(handler, *, login: bool = True):
        async def wrapper(request: Request) -> Response:
            if request.method == "POST" and not same_origin(request):
                return Response("cross-origin request refused", 403)
            if not auth.is_set_up and handler is not setup:
                return RedirectResponse("/ui/setup", 303)
            if login and not logged_in(request):
                return RedirectResponse("/ui/login", 303)
            # Reloading a result page must land somewhere useful, not on a 405.
            if request.method == "GET" and handler in (enter_code, create_token):
                return RedirectResponse("/ui", 303)
            response = await handler(request)
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'"
            )
            return response

        return wrapper

    def session_response(target: str) -> Response:
        response = RedirectResponse(target, 303)
        response.set_cookie(
            COOKIE, auth.new_session(), max_age=30 * 86400, httponly=True,
            secure=settings.cookie_secure, samesite="strict", path="/ui",
        )
        return response

    async def setup(request: Request) -> Response:
        if auth.is_set_up:
            return RedirectResponse("/ui/login", 303)
        if request.method == "GET":
            return page("setup.html", error=None)
        form = await request.form()
        token = str(form.get("setup_token", "")).strip()
        if not auth.check_setup_token(token):
            auth.log("setup_failed", "bad setup token", ip(request))
            return page("setup.html", 403, error="That setup token doesn't match. Copy it from the server console.", field="setup_token")
        if token not in pending_setup or "password" in form:
            password = str(form.get("password", ""))
            if len(password) < MIN_PASSWORD:
                return page("setup.html", 400, setup_token=token, field="password",
                            error=f"Use at least {MIN_PASSWORD} characters. A few random words work well.")
            if password != form.get("confirm"):
                return page("setup.html", 400, setup_token=token, field="confirm", error="The two passwords don't match.")
            pending_setup[token] = (auth.hash_password(password), pyotp.random_base32())
        password_hash, secret = pending_setup[token]
        code = str(form.get("totp", "")).strip()
        if code and pyotp.TOTP(secret).verify(code, valid_window=1):
            auth.finish_setup(password_hash, secret)
            pending_setup.clear()
            return session_response("/ui?welcome=1")
        uri = pyotp.TOTP(secret).provisioning_uri(name="owner", issuer_name="Cortex")
        return page("setup_totp.html", setup_token=token, secret=secret, qr=_qr_data_uri(uri),
                    error="That code didn't match. Wait for the next one and try again." if code else None)

    async def login(request: Request) -> Response:
        if request.method == "GET":
            return page("login.html", error=None)
        form = await request.form()
        if not auth.check_password(str(form.get("password", "")), ip(request)):
            wait = int(auth.locked_for("password"))
            return page("login.html", 401, error=f"Too many attempts. Try again in {wait} seconds." if wait else "That password isn't right.")
        auth.log("ui_login", "", ip(request))
        return session_response("/ui")

    async def logout(request: Request) -> Response:
        auth.end_session(request.cookies.get(COOKIE, ""))
        response = RedirectResponse("/ui/login", 303)
        response.delete_cookie(COOKIE, path="/ui")
        return response

    def access_page(status: int = 200, **context) -> HTMLResponse:
        context = {"new_token": None, "new_token_name": None, "error": None, "notice": None, "error_form": None, **context}
        return page("home.html", status, active="access", tokens=auth.list_tokens(), audit=auth.audit(50),
                    public_url=settings.public_url, **context)

    async def home(request: Request) -> Response:
        notices = {"approved": "Client approved. It will connect within a few seconds.", "denied": "Login denied.",
                   "revoked": "Access revoked.", "expired": "That login request expired. Start again from the client."}
        notice = notices.get(request.query_params.get("done", ""))
        return access_page(notice=notice, welcome="welcome" in request.query_params)

    async def enter_code(request: Request) -> Response:
        """Invariant 3: a login code is accepted only here, behind a fresh TOTP."""
        form = await request.form()
        row = auth.pending_by_code(str(form.get("code", "")))
        if not auth.check_totp(str(form.get("totp", "")), ip(request)):
            wait = int(auth.locked_for("totp"))
            message = f"Too many attempts. Try again in {wait} seconds." if wait else "That authenticator code isn't right."
            return access_page(401, error=message, error_form="approve", code_value=form.get("code", ""))
        if not row:
            auth.log("code_rejected", "", ip(request))
            return access_page(404, error="No active login has that code. It may have expired.", error_form="approve")
        ticket = secrets.token_urlsafe(16)
        confirmable[ticket] = row["id"]
        return page("confirm.html", active="access", ticket=ticket, request=row)

    async def decide(request: Request) -> Response:
        form = await request.form()
        request_id = confirmable.pop(str(form.get("ticket", "")), None)
        if request_id is None:
            return RedirectResponse("/ui", 303)
        approve = form.get("decision") == "approve"
        scope = "read" if form.get("scope") == "read" else "write"
        try:
            auth.decide(request_id, approve, scope, ip(request))
        except AuthError:
            return RedirectResponse("/ui?done=expired", 303)
        return RedirectResponse(f"/ui?done={'approved' if approve else 'denied'}", 303)

    async def create_token(request: Request) -> Response:
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if not name:
            return access_page(400, error="Name the client this token is for.", error_form="token")
        if not auth.check_totp(str(form.get("totp", "")), ip(request)):
            return access_page(401, error="That authenticator code isn't right.", error_form="token", name_value=name)
        token = auth.create_static_token(name, "read" if form.get("scope") == "read" else "write")
        return access_page(new_token=token, new_token_name=name)

    async def revoke(request: Request) -> Response:
        form = await request.form()
        auth.revoke(int(form.get("id", 0)), ip(request))
        return RedirectResponse("/ui?done=revoked", 303)

    async def reenroll(request: Request) -> Response:
        if request.method == "GET":
            return page("reenroll.html", active="security", stage="verify")
        form = await request.form()
        secret = str(form.get("new_secret", ""))
        if secret:
            ticket_ok = pending_totp.get("reenroll") == secret
            if ticket_ok and pyotp.TOTP(secret).verify(str(form.get("new_totp", "")).strip(), valid_window=1):
                auth.change_totp(secret)
                pending_totp.pop("reenroll", None)
                return RedirectResponse("/ui", 303)
            error = "That code didn't match. Wait for the next one and try again."
        else:
            if not (auth.check_password(str(form.get("password", "")), ip(request)) and auth.check_totp(str(form.get("totp", "")), ip(request))):
                return page("reenroll.html", 401, active="security", stage="verify", error="The password or authenticator code isn't right.")
            secret, error = pyotp.random_base32(), None
            pending_totp["reenroll"] = secret
        uri = pyotp.TOTP(secret).provisioning_uri(name="owner", issuer_name="Cortex")
        return page("reenroll.html", active="security", stage="enrol", secret=secret, qr=_qr_data_uri(uri), error=error)

    async def files(request: Request) -> Response:
        vault = store.vault
        path = request.query_params.get("path")
        context = {"tree": _tree(vault.all_paths()), "path": None, "content": "", "hash": "", "error": None,
                   "message": None, "active": "files", "new": "new" in request.query_params}
        status = 200
        if request.method == "POST":
            form = await request.form()
            path = str(form.get("path", "")).strip()
            content = str(form.get("content", "")).replace("\r\n", "\n")
            expected = str(form.get("hash", "")) or None
            try:
                clean = vault.clean(path)
                if clean.startswith("core/") and form.get("confirm_core") != "yes":
                    raise VaultError("core/ loads into every session. Tick the box below to confirm this change.")
                result = store.write(clean, content, expected, actor=HUMAN, human=True)
                context.update(path=clean, content=vault.read_text(clean), hash=result["hash"], message="Saved and committed.", new=False)
                context["tree"] = _tree(vault.all_paths())
                return page("files.html", **context)
            except VaultError as error:
                context.update(path=path, content=content, hash=expected or "", error=str(error), new=not expected)
                status = 409
        elif path:
            try:
                clean = vault.clean(path, must_exist=True)
                context.update(path=clean, content=vault.read_text(clean), hash=vault.hash(clean))
            except VaultError as error:
                context.update(error=str(error))
                status = 404
        return page("files.html", status, **context)

    static = Path(__file__).parent / "static"
    return Starlette(routes=[
        Route("/", lambda request: RedirectResponse("/ui", 303)),
        Route("/ui", guarded(home)),
        Route("/ui/setup", guarded(setup, login=False), methods=["GET", "POST"]),
        Route("/ui/login", guarded(login, login=False), methods=["GET", "POST"]),
        Route("/ui/logout", guarded(logout), methods=["POST"]),
        Route("/ui/approve", guarded(enter_code), methods=["GET", "POST"]),
        Route("/ui/decide", guarded(decide), methods=["POST"]),
        Route("/ui/tokens", guarded(create_token), methods=["GET", "POST"]),
        Route("/ui/tokens/revoke", guarded(revoke), methods=["POST"]),
        Route("/ui/reenroll", guarded(reenroll), methods=["GET", "POST"]),
        Route("/ui/files", guarded(files), methods=["GET", "POST"]),
        Mount("/ui/static", StaticFiles(directory=static)),
    ])
