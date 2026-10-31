"""The MCP surface: eight tools whose descriptions carry the rules."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .search import search as run_search
from .store import Store
from .vault import VaultError

WRITE_TOOLS = {"write", "update", "supersede", "move", "save_log"}


@dataclass
class Caller:
    name: str
    scope: str  # "read" or "write"


current_caller: ContextVar[Caller | None] = ContextVar("cortex_caller", default=None)


class CortexMCP(MCPServer):
    """Read tokens neither see nor can call write tools."""

    def _is_read_only(self) -> bool:
        caller = current_caller.get()
        return caller is None or caller.scope != "write"

    async def list_tools(self):
        tools = await super().list_tools()
        return [t for t in tools if not (self._is_read_only() and t.name in WRITE_TOOLS)]

    async def call_tool(self, name, arguments, context=None):
        if name in WRITE_TOOLS and self._is_read_only():
            raise ToolError(f"unknown tool: {name}")
        return await super().call_tool(name, arguments, context)

    def set_instructions(self, text: str) -> None:
        self._lowlevel_server.instructions = text


def _actor() -> str:
    caller = current_caller.get()
    return caller.name if caller else "unknown"


async def _call(function, *args, **kwargs):
    """Run blocking vault work off the event loop; refusals reach the agent as tool errors."""
    try:
        return await anyio.to_thread.run_sync(lambda: function(*args, **kwargs))
    except VaultError as error:
        raise ToolError(str(error)) from error


def build_mcp(store: Store, instructions: str) -> CortexMCP:
    mcp = CortexMCP("cortex", instructions=instructions)

    @mcp.tool()
    async def search(query: str, scope: str | None = None, exact: bool = False) -> dict:
        """Find notes. Searches memory/ and entities/ and hides non-current notes. core/ is left out
        because it is already in your context, and .archive/ is never searched. Returns the 8 best
        matches at most, dropping any far weaker than the top result.
        `excluded` reports what was left out, with reasons and match counts for docs/ and logs/:
        if it shows matches there, tell the user. Use scope="all" to include docs/ and logs/ only
        when the user asks. exact=true does plain text matching."""
        return await _call(run_search, store.index, query, scope, exact)

    @mcp.tool()
    async def read(path: str) -> dict:
        """Read a note: content, hash, links, backlinks with relation types, and History.
        read("@context") returns core/ plus the memory index, for clients that dropped the server
        instructions. Memory is the summary; follow [[links]] only when the task needs more.
        If memory and an older doc disagree, memory wins."""
        return await _call(store.read, path)

    @mcp.tool(name="list")
    async def list_(folder: str = "") -> dict:
        """List the files in a folder, newest first."""
        return await _call(store.list, folder)

    @mcp.tool()
    async def write(path: str, content: str, expected_hash: str | None = None) -> dict:
        """Create a note, or replace one. One fact, one place: link instead of copying.
        Without expected_hash: create a one-line memory in memory/<slug>, a card in entities/, or a
        doc in docs/; refused if a live note already covers the subject (that note is returned; use
        update or supersede on it). With expected_hash from read: replace the content; refused if
        stale. Frontmatter relations must use the types in core/relations.md. core/ is refused."""
        return await _call(store.write, path, content, expected_hash, actor=_actor())

    @mcp.tool()
    async def update(path: str, fields: dict, reason: str, expected_hash: str) -> dict:
        """Change frontmatter fields of a note you just read: edits in place, appends History and
        commits. Returns stale_copies, other places still showing the old value; fix or flag them.
        Use this when a fact changes, status included; never create a second note on the same
        subject. To replace a note with another, use supersede."""
        return await _call(store.update, path, fields, reason, expected_hash, actor=_actor())

    @mcp.tool()
    async def supersede(old: str, new: str, reason: str) -> dict:
        """Retire a note or memory line in favour of its replacement, in one commit. `new` is the
        path of an existing note (ending .md), or the replacement's text to create beside the old
        one. The old note stops surfacing but stays in History. Only between notes on the same fact."""
        return await _call(store.supersede, old, new, reason, actor=_actor())

    @mcp.tool()
    async def move(path: str, to: str) -> dict:
        """Rename a note and rewrite links to it, in one commit. Never delete: to forget, move the
        note to .archive/. Returns linked_from, the live notes that now link to the new path; after
        archiving, fix those links or flag them to the user."""
        return await _call(store.move, path, to, actor=_actor())

    @mcp.tool()
    async def save_log(kind: str, text: str, date: str | None = None) -> dict:
        """Append to logs/: kind is "daily", "debrief" or "raw". Append-only. date is YYYY-MM-DD and
        defaults to today; set it only to backfill an older day."""
        return await _call(store.save_log, kind, text, date, actor=_actor())

    return mcp
