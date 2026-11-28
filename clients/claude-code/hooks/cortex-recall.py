#!/usr/bin/env python3
"""UserPromptSubmit hook: on the first real prompt of a session, search Cortex and add the
top matches to the context. Fails silent: a slow or down Cortex never blocks a prompt.
Needs CORTEX_URL (the server's /mcp address); without it the hook does nothing, so no address is
ever baked in. Install: clients/README.md."""

import contextlib
import json
import os
import sys
import urllib.request
from pathlib import Path

URL = os.environ.get("CORTEX_URL", "")
TOKEN_FILE = Path(os.environ.get("CORTEX_TOKEN_FILE", "~/.config/cortex/claude-code.token")).expanduser()
MARKERS = Path("~/.cache/cortex-recall").expanduser()
TIMEOUT = 4
MIN_PROMPT = 15  # "yes", "go on": wait for a prompt with something to search for
TOP = 5


def post(body: dict, token: str, session: str | None) -> tuple[dict | None, str | None]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(URL, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        session = response.headers.get("Mcp-Session-Id") or session
        text = response.read().decode()
    if not text.strip():
        return None, session
    if text.lstrip().startswith("{"):
        return json.loads(text), session
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return (json.loads(data[-1]) if data else None), session


def search(query: str) -> list[dict]:
    token = TOKEN_FILE.read_text().strip()
    _, session = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "cortex-recall-hook", "version": "1"}}}, token, None)
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, session)
    reply, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "search", "arguments": {"query": query}}}, token, session)
    result = (reply or {}).get("result", {})
    data = result.get("structuredContent")
    if data is None:
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        data = json.loads(texts[0]) if texts else {}
    data = data.get("result", data)
    return data.get("results", [])[:TOP]


def main() -> None:
    if not URL:
        return
    event = json.load(sys.stdin)
    prompt = (event.get("prompt") or "").strip()
    marker = MARKERS / str(event.get("session_id", "none"))
    if marker.exists() or len(prompt) < MIN_PROMPT:
        return
    MARKERS.mkdir(parents=True, exist_ok=True)
    marker.touch()
    results = search(prompt[:500])
    if not results:
        return
    lines = [f"- {r['path']}: {' '.join(r.get('snippet', '').split())[:300]}" for r in results]
    context = ("Cortex memory matching this prompt (auto-search at session start; read a note for "
               "more, search again for other topics):\n" + "\n".join(lines))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": context}}))


if __name__ == "__main__":
    # A hook that crashes would interrupt the prompt; a missed recall costs nothing.
    with contextlib.suppress(Exception):
        main()
