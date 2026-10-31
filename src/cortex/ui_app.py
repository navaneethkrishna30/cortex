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
templates.filters["when"] = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _qr_data_uri(uri: str) -> str:
    return segno.make(uri).svg_data_uri(scale=6, border=2)


def build_ui_app(auth: AuthStore, store: Store, settings: Settings) -> Starlette:
    # Secrets waiting for their first confirming code; never saved until then.
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
            response = await handler(request)
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self' 'unsafe-inline'"
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
        token = str(form.get("setup_token", ""))
        password = str(form.get("password", ""))
        if not auth.check_setup_token(token):
            auth.log("setup_failed", "bad setup token", ip(request))
            return page("setup.html", 403, error="Wrong setup token. It is printed in the server console.")
        if len(password) < MIN_PASSWORD:
            return page("setup.html", 400, error=f"Use a long passphrase, at least {MIN_PASSWORD} characters.")
        secret = pending_totp.setdefault(token, pyotp.random_base32())
        code = str(form.get("totp", "")).strip()
        if code and pyotp.TOTP(secret).verify(code, valid_window=1):
            auth.finish_setup(password, secret)
            pending_totp.clear()
            return session_response("/ui")
        uri = pyotp.TOTP(secret).provisioning_uri(name="owner", issuer_name="Cortex")
        return page(
            "setup_totp.html", setup_token=token, password=password, secret=secret, qr=_qr_data_uri(uri),
            error="That code did not match." if code else None,
        )

    async def login(request: Request) -> Response:
        if request.method == "GET":
            return page("login.html", error=None)
        form = await request.form()
        if not auth.check_password(str(form.get("password", "")), ip(request)):
            wait = int(auth.locked_for("password"))
            return page("login.html", 401, error=f"Locked for {wait}s." if wait else "Wrong password.")
        auth.log("ui_login", "", ip(request))
        return session_response("/ui")

    async def logout(request: Request) -> Response:
        auth.end_session(request.cookies.get(COOKIE, ""))
        response = RedirectResponse("/ui/login", 303)
        response.delete_cookie(COOKIE, path="/ui")
        return response

    async def home(request: Request) -> Response:
        return page(
            "home.html", tokens=auth.list_tokens(), audit=auth.audit(), new_token=None, error=None,
            public_url=settings.public_url,
        )

    async def enter_code(request: Request) -> Response:
        """Invariant 3: a login code is accepted only here, behind a fresh TOTP."""
        form = await request.form()
        row = auth.pending_by_code(str(form.get("code", "")))
        if not auth.check_totp(str(form.get("totp", "")), ip(request)):
            return page("home.html", 401, tokens=auth.list_tokens(), audit=auth.audit(), new_token=None,
                        error="Wrong TOTP code.", public_url=settings.public_url)
        if not row:
            auth.log("code_rejected", "", ip(request))
            return page("home.html", 404, tokens=auth.list_tokens(), audit=auth.audit(), new_token=None,
                        error="No active request has that code.", public_url=settings.public_url)
        ticket = secrets.token_urlsafe(16)
        confirmable[ticket] = row["id"]
        return page("confirm.html", ticket=ticket, request=row)

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
            pass
        return RedirectResponse("/ui", 303)

    async def create_token(request: Request) -> Response:
        form = await request.form()
        new_token, error = None, None
        name = str(form.get("name", "")).strip()
        if not auth.check_totp(str(form.get("totp", "")), ip(request)):
            error = "Wrong TOTP code."
        elif not name:
            error = "Name the client this token is for."
        else:
            new_token = auth.create_static_token(name, "read" if form.get("scope") == "read" else "write")
        return page("home.html", tokens=auth.list_tokens(), audit=auth.audit(), new_token=new_token,
                    error=error, public_url=settings.public_url)

    async def revoke(request: Request) -> Response:
        form = await request.form()
        auth.revoke(int(form.get("id", 0)), ip(request))
        return RedirectResponse("/ui", 303)

    async def reenroll(request: Request) -> Response:
        if request.method == "GET":
            return page("reenroll.html", stage="verify", error=None)
        form = await request.form()
        secret = str(form.get("new_secret", ""))
        if secret:
            ticket_ok = pending_totp.get("reenroll") == secret
            if ticket_ok and pyotp.TOTP(secret).verify(str(form.get("new_totp", "")).strip(), valid_window=1):
                auth.change_totp(secret)
                pending_totp.pop("reenroll", None)
                return RedirectResponse("/ui", 303)
            error = "That code did not match."
        else:
            if not (auth.check_password(str(form.get("password", "")), ip(request)) and auth.check_totp(str(form.get("totp", "")), ip(request))):
                return page("reenroll.html", 401, stage="verify", error="Password or TOTP was wrong.")
            secret, error = pyotp.random_base32(), None
            pending_totp["reenroll"] = secret
        uri = pyotp.TOTP(secret).provisioning_uri(name="owner", issuer_name="Cortex")
        return page("reenroll.html", stage="enrol", secret=secret, qr=_qr_data_uri(uri), error=error)

    async def files(request: Request) -> Response:
        vault = store.vault
        path = request.query_params.get("path")
        context = {"tree": vault.all_paths(), "path": None, "content": "", "hash": "", "error": None, "message": None}
        status = 200
        if request.method == "POST":
            form = await request.form()
            path = str(form.get("path", "")).strip()
            content = str(form.get("content", "")).replace("\r\n", "\n")
            expected = str(form.get("hash", "")) or None
            try:
                clean = vault.clean(path)
                if clean.startswith("core/") and form.get("confirm_core") != "yes":
                    raise VaultError("core/ is loaded into every session. Tick the confirmation to save it.")
                result = store.write(clean, content, expected, actor=HUMAN, human=True)
                context.update(path=clean, content=vault.read_text(clean), hash=result["hash"], message="Saved and committed.")
                context["tree"] = vault.all_paths()
                return page("files.html", **context)
            except VaultError as error:
                context.update(path=path, content=content, hash=expected or "", error=str(error))
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
        Route("/ui/approve", guarded(enter_code), methods=["POST"]),
        Route("/ui/decide", guarded(decide), methods=["POST"]),
        Route("/ui/tokens", guarded(create_token), methods=["POST"]),
        Route("/ui/tokens/revoke", guarded(revoke), methods=["POST"]),
        Route("/ui/reenroll", guarded(reenroll), methods=["GET", "POST"]),
        Route("/ui/files", guarded(files), methods=["GET", "POST"]),
        Mount("/ui/static", StaticFiles(directory=static)),
    ])
