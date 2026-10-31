"""The tailnet listener: /ui only. Reached through `tailscale serve`, never a public tunnel."""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

import pyotp
import segno
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import AuthError, AuthStore
from .config import Settings
from .gitrepo import HUMAN
from .search import search
from .store import Store
from .vault import ARCHIVE, VaultError

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


NEW_NOTE_FOLDERS = [name for name in FOLDER_HINTS if name != "logs"]
MOVABLE_FOLDERS = ("memory", "entities", "docs")
ACTIVITY_ROWS = 10
DETAILS = {"invalid_grant": "Expired or invalid sign-in", "bad setup token": ""}


def _event_detail(detail: str | None, event: str) -> str:
    """Audit detail in plain words, without repeating the event or the IP column."""
    text = DETAILS.get(detail or "", detail or "")
    text = re.sub(r"\s+from\s+\S+$", "", text)
    return text.replace("(write)", "· read and write").replace("(read)", "· read only")


def _sentence(error: Exception) -> str:
    text = str(error)
    return text[:1].upper() + text[1:]


def _ago(moment: float) -> str:
    seconds = int(time.time() - moment)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


templates.filters["ago"] = _ago
templates.filters["event_label"] = lambda name: EVENTS.get(name, (name, False))[0]
templates.filters["event_bad"] = lambda name: EVENTS.get(name, (name, False))[1]
templates.filters["event_detail"] = _event_detail


def _tree(paths: list[str], current: str | None = None) -> dict:
    """Nest note paths into folders. The vault's own folders always show, in loading order."""
    root = {"folders": [], "files": []}

    def folder_in(parent: dict, name: str, path: str) -> dict:
        for folder in parent["folders"]:
            if folder["name"] == name:
                return folder
        folder = {
            "name": name, "path": path, "folders": [], "files": [], "hint": FOLDER_HINTS.get(path),
            "can_add": path.split("/")[0] != "logs",
            "has_current": bool(current and current.startswith(path + "/")),
        }
        parent["folders"].append(folder)
        return folder

    for name in FOLDER_HINTS:
        folder_in(root, name, name)
    for path in paths:
        *folders, file_name = path.split("/")
        node, walked = root, []
        for name in folders:
            walked.append(name)
            node = folder_in(node, name, "/".join(walked))
        node["files"].append({"path": path, "name": file_name})

    def sort(node: dict) -> None:
        node["files"].sort(key=lambda f: f["name"].lower())
        for folder in node["folders"]:
            folder["folders"].sort(key=lambda f: f["name"].lower())
            sort(folder)

    def mark_open(node: dict) -> None:
        """Open what has something to show; an empty folder stays shut."""
        for folder in node["folders"]:
            mark_open(folder)
            has_content = bool(folder["files"] or folder["folders"])
            folder["open"] = folder["has_current"] or ("/" not in folder["path"] and folder["path"] != "logs" and has_content)

    sort(root)
    mark_open(root)
    return root


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
                "default-src 'self'; style-src 'self'; img-src 'self' data:; script-src 'self'"
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

    def access_page(status: int = 200, failures_only: bool = False, show_all: bool = False, **context) -> HTMLResponse:
        rows = auth.audit(500)
        if failures_only:
            rows = [row for row in rows if EVENTS.get(row["event"], ("", False))[1]]
        activity = {"audit": rows if show_all else rows[:ACTIVITY_ROWS], "failures_only": failures_only,
                    "more_activity": not show_all and len(rows) > ACTIVITY_ROWS, "activity_total": len(rows)}
        context = {"new_token": None, "new_token_name": None, "error": None, "notice": None, "error_form": None, **context}
        return page("home.html", status, active="access", tokens=auth.list_tokens(), public_url=settings.public_url, **activity, **context)

    async def home(request: Request) -> Response:
        notices = {"approved": "Client approved. It will connect within a few seconds.", "denied": "Login denied.",
                   "revoked": "Access revoked.", "expired": "That login request expired. Start again from the client."}
        notice = notices.get(request.query_params.get("done", ""))
        return access_page(notice=notice, welcome="welcome" in request.query_params,
                           failures_only=request.query_params.get("activity") == "failures",
                           show_all="all" in request.query_params)

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

    def files_page(status: int = 200, **context) -> HTMLResponse:
        vault = store.vault
        paths = vault.all_paths()
        base = {"path": None, "content": "", "hash": "", "error": None, "message": None, "active": "files", "new": False,
                "q": None, "results": [], "history": None, "version": None, "new_folders": NEW_NOTE_FOLDERS,
                "new_folder": None, "new_name": ""}
        context = {**base, **context}
        path = context["path"]
        modified = sorted(paths, key=lambda p: (vault.root / p).stat().st_mtime, reverse=True)[:8]
        return page("files.html", status, tree=_tree(paths, path),
                    movable=bool(path) and path.split("/")[0] in MOVABLE_FOLDERS,
                    recent=[{"path": p, "modified": (vault.root / p).stat().st_mtime} for p in modified],
                    overview=[{"name": name, "hint": hint, "count": sum(p.startswith(name + "/") for p in paths)}
                              for name, hint in FOLDER_HINTS.items()], **context)

    async def files(request: Request) -> Response:
        vault = store.vault
        params = request.query_params
        if request.method == "POST":
            form = await request.form()
            folder, name = str(form.get("folder", "")), str(form.get("name", "")).strip()
            path = str(form.get("path", "")).strip() or (f"{folder}/{name}" if name else "")
            content = str(form.get("content", "")).replace("\r\n", "\n")
            expected = str(form.get("hash", "")) or None
            try:
                if not path:
                    raise VaultError("Give the note a name.")
                clean = vault.clean(path)
                if clean.startswith("core/") and form.get("confirm_core") != "yes":
                    raise VaultError("core/ loads into every session. Confirm this change before saving.")
                result = store.write(clean, content, expected, actor=HUMAN, human=True)
                return files_page(path=clean, content=vault.read_text(clean), hash=result["hash"], message="Saved and committed.")
            except VaultError as error:
                is_new = not expected
                return files_page(409, path=None if is_new else path, content=content, hash=expected or "", error=_sentence(error),
                                  new=is_new, new_folder=folder or path.split("/")[0], new_name=name or path.split("/", 1)[-1])
        if "new" in params:
            return files_page(new=True, new_folder=params.get("folder", "").strip("/").split("/")[0] or None)
        query = params.get("q", "").strip()
        if query and not params.get("path"):
            return files_page(q=query, results=search(store.index, query, scope="all", limit=20)["results"])
        message = {"archived": "Note archived.", "moved": "Note moved."}.get(params.get("done", ""))
        path = params.get("path")
        if path:
            try:
                clean = vault.clean(path, must_exist=True)
            except VaultError:
                return files_page(404, error=f"There's no note at {path}. It may have moved or been archived.")
            return files_page(path=clean, content=vault.read_text(clean), hash=vault.hash(clean), message=message)
        return files_page(message=message)

    async def note_history(request: Request) -> Response:
        vault = store.vault
        path, rev = request.query_params.get("path", ""), request.query_params.get("rev", "")
        try:
            clean = vault.clean(path, must_exist=True)
        except VaultError:
            return files_page(404, error=f"There's no note at {path}. It may have moved or been archived.")
        version = store.git.show(rev, clean) if re.fullmatch(r"[0-9a-f]{40}", rev) else None
        return files_page(path=clean, history=store.git.history(clean), version=version)

    async def move_note(request: Request) -> Response:
        form = await request.form()
        path = str(form.get("path", ""))
        try:
            result = store.move(path, str(form.get("to", "")).strip(), actor=HUMAN)
        except VaultError as error:
            return files_page(409, error=_sentence(error))
        return RedirectResponse(f"/ui/files?path={quote(result['path'])}&done=moved", 303)

    async def archive_note(request: Request) -> Response:
        form = await request.form()
        path = str(form.get("path", ""))
        try:
            store.move(path, f"{ARCHIVE}/{path}", actor=HUMAN)
        except VaultError as error:
            return files_page(409, error=_sentence(error))
        return RedirectResponse("/ui/files?done=archived", 303)

    async def not_found(request: Request, exc: HTTPException) -> HTMLResponse:
        if exc.status_code == 404:
            return page("error.html", 404, heading="Page not found", detail="This address doesn't lead anywhere in Cortex.")
        return page("error.html", exc.status_code, heading="Something went wrong", detail=str(exc.detail))

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
        Route("/ui/files/history", guarded(note_history)),
        Route("/ui/files/move", guarded(move_note), methods=["POST"]),
        Route("/ui/files/archive", guarded(archive_note), methods=["POST"]),
        Mount("/ui/static", StaticFiles(directory=static)),
    ], exception_handlers={HTTPException: not_found})
