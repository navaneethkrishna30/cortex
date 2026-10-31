"""The public listener: /mcp and /oauth only. It has no UI routes at all."""

from __future__ import annotations

import contextlib
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import REQUEST_TTL, AuthError, AuthStore
from .config import Settings
from .mcp_server import Caller, CortexMCP, current_caller
from .ui_app import templates

# The waiting page shares the UI stylesheet and fonts; nothing here is secret.
STATIC = Path(__file__).parent / "static"
RATE_WINDOW = 60
# The waiting page alone polls 30 times a minute.
RATE_LIMITS = {"/oauth/": 120, "/mcp": 300}


def client_ip(scope_or_request) -> str:
    """Forwarded IPs come from the tunnel. Used for rate limits only, never for access."""
    headers = dict(scope_or_request.get("headers", [])) if isinstance(scope_or_request, dict) else None
    forwarded = headers.get(b"x-forwarded-for", b"").decode() if headers is not None else scope_or_request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope_or_request["client"] if isinstance(scope_or_request, dict) else scope_or_request.client
    return (client[0] if client else "") or "unknown"


class RateLimit:
    """Per-IP sliding window over the public routes."""

    def __init__(self, app, limits=RATE_LIMITS):
        self.app = app
        self.limits = limits
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            prefix = next((p for p in self.limits if scope["path"].startswith(p)), None)
            if prefix:
                hits = self.hits[(prefix, client_ip(scope))]
                now = time.monotonic()
                while hits and hits[0] < now - RATE_WINDOW:
                    hits.popleft()
                if len(hits) >= self.limits[prefix]:
                    await JSONResponse({"error": "rate_limited"}, 429, {"Retry-After": "60"})(scope, receive, send)
                    return
                hits.append(now)
        await self.app(scope, receive, send)


class BearerAuth:
    """Guards /mcp and tells the tools who is calling."""

    def __init__(self, app, auth: AuthStore, settings: Settings):
        self.app = app
        self.auth = auth
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        header = dict(scope["headers"]).get(b"authorization", b"").decode()
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        row = self.auth.check_bearer(token) if token else None
        if not row:
            metadata = f"{self.settings.public_url}/.well-known/oauth-protected-resource"
            challenge = f'Bearer resource_metadata="{metadata}"'
            await JSONResponse({"error": "invalid_token"}, 401, {"WWW-Authenticate": challenge})(scope, receive, send)
            return
        reset = current_caller.set(Caller(row["name"], row["scope"]))
        try:
            await self.app(scope, receive, send)
        finally:
            current_caller.reset(reset)


def _error(error: str, description: str = "", status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status)


def build_public_app(mcp: CortexMCP, auth: AuthStore, settings: Settings) -> Starlette:
    base = settings.public_url
    secure = settings.cookie_secure

    async def resource_metadata(request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
            "scopes_supported": ["read", "write"],
        })

    async def server_metadata(request: Request) -> JSONResponse:
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["read", "write"],
        })

    async def register(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            client_id = auth.register_client(
                str(body.get("client_name", "unnamed client")), list(body.get("redirect_uris", [])), settings.redirect_allowlist
            )
        except AuthError as error:
            return _error("invalid_redirect_uri", str(error))
        except (ValueError, AttributeError, TypeError):
            return _error("invalid_client_metadata")
        return JSONResponse({
            "client_id": client_id,
            "client_name": body.get("client_name", "unnamed client"),
            "redirect_uris": body["redirect_uris"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }, 201)

    async def authorize(request: Request) -> Response:
        query = request.query_params
        client = auth.client(query.get("client_id", ""))
        redirect_uri = query.get("redirect_uri", "")
        if not client or redirect_uri not in client["redirect_uris"].split("\n"):
            return _error("invalid_request", "unknown client or redirect_uri")
        if query.get("response_type") != "code" or query.get("code_challenge_method") != "S256" or not query.get("code_challenge"):
            return _error("invalid_request", "PKCE with S256 is required")
        requested = query.get("scope", "write").split()
        scope = "write" if "write" in requested or not requested else "read"
        secret, code = auth.create_login_request(
            client["id"], redirect_uri, query["code_challenge"], scope, query.get("state"), client_ip(request)
        )
        page = templates.get_template("waiting.html").render(code=code, seconds=REQUEST_TTL)
        response = HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
        response.set_cookie("cortex_login", secret, max_age=REQUEST_TTL, httponly=True, secure=secure, samesite="lax", path="/oauth")
        return response

    async def poll(request: Request) -> JSONResponse:
        result = auth.poll(request.cookies.get("cortex_login"))
        if result["status"] != "approved":
            return JSONResponse({"status": result["status"]})
        params = {"code": result["code"]}
        if result["state"]:
            params["state"] = result["state"]
        joiner = "&" if "?" in result["redirect_uri"] else "?"
        response = JSONResponse({"status": "approved", "redirect": f"{result['redirect_uri']}{joiner}{urlencode(params)}"})
        response.delete_cookie("cortex_login", path="/oauth")
        return response

    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        headers = {"Cache-Control": "no-store"}
        try:
            if form.get("grant_type") == "authorization_code":
                issued = auth.exchange_code(
                    str(form.get("code", "")), str(form.get("client_id", "")),
                    str(form.get("redirect_uri", "")), str(form.get("code_verifier", "")),
                )
            elif form.get("grant_type") == "refresh_token":
                issued = auth.refresh(str(form.get("refresh_token", "")))
            else:
                return _error("unsupported_grant_type")
        except AuthError as error:
            auth.log("token_failed", str(error), client_ip(request))
            return _error("invalid_grant")
        return JSONResponse(issued, headers=headers)

    # The tunnel fronts this port under the public hostname; bearer tokens, not Host, guard it.
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    routes = [
        Route("/.well-known/oauth-protected-resource", resource_metadata),
        Route("/.well-known/oauth-protected-resource/mcp", resource_metadata),
        Route("/.well-known/oauth-authorization-server", server_metadata),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize),
        Route("/oauth/poll", poll),
        Route("/oauth/token", token, methods=["POST"]),
        Mount("/oauth/static", StaticFiles(directory=STATIC)),
        *mcp_app.routes,
    ]
    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(RateLimit), Middleware(BearerAuth, auth=auth, settings=settings)],
    )
