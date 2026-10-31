"""Wires the pieces together and runs both listeners in one process."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .auth import AuthStore
from .config import Settings
from .embed import make_embedder
from .gitrepo import Git
from .index import Index
from .mcp_server import build_mcp
from .public_app import build_public_app
from .store import Store, build_context
from .ui_app import build_ui_app
from .vault import Vault
from .watcher import Background

log = logging.getLogger("cortex")


def open_store(settings: Settings) -> Store:
    vault = Vault(settings)
    git = Git(settings.vault)
    git.init()
    return Store(vault, Index(vault, make_embedder(settings)), git)


def build_apps(store: Store, auth: AuthStore, settings: Settings):
    mcp = build_mcp(store, build_context(store.vault))
    store.on_change = lambda: mcp.set_instructions(build_context(store.vault))
    return build_public_app(mcp, auth, settings), build_ui_app(auth, store, settings)


async def serve(settings: Settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store = open_store(settings)
    store.sync_outside_edits()
    auth = AuthStore(settings.auth_db)
    if not auth.is_set_up:
        print(f"First-run setup token: {auth.new_setup_token()}", flush=True)
    public, ui = build_apps(store, auth, settings)
    background = Background(store)
    background.start()
    servers = [
        uvicorn.Server(uvicorn.Config(public, host=settings.host, port=settings.mcp_port, proxy_headers=False)),
        uvicorn.Server(uvicorn.Config(ui, host=settings.host, port=settings.ui_port, proxy_headers=False, lifespan="off")),
    ]
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        background.stop()
