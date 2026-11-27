import anyio

from cortex.mcp_server import Caller, build_mcp, current_caller
from cortex.search import search
from cortex.store import build_context as context


def test_expired_note_leaves_search_and_memory_index(store):
    store.write("memory/trip.md", "---\nstatus: current\nexpires: 2000-01-01\n---\nNav is in Goa this week.", actor="a")
    store.write("memory/home.md", "---\nstatus: current\nexpires: 2999-01-01\n---\nNav lives in Hyderabad.", actor="a")
    result = search(store.index, "Nav")
    assert [r["path"] for r in result["results"]] == ["memory/home.md"]
    assert {"path": "memory/trip.md", "reason": "status: expired"} in result["excluded"]
    assert not search(store.index, "Goa", exact=True)["results"]
    index = context(store.vault)
    assert "Hyderabad" in index and "Goa" not in index


def test_expiry_is_checked_at_query_time(store):
    store.write("memory/trip.md", "---\nstatus: current\nexpires: 2999-01-01\n---\nNav is in Goa this week.", actor="a")
    assert search(store.index, "Goa")["results"]
    store.index.db.execute("UPDATE expiry SET expires='2000-01-01'")  # the date passes, no reindex
    assert not search(store.index, "Goa")["results"]


def test_bad_expires_value_is_ignored(store):
    store.write("memory/trip.md", "---\nstatus: current\nexpires: soon\n---\nNav is in Goa.", actor="a")
    assert search(store.index, "Goa")["results"]


def test_every_tool_asks_to_load_up_front(store):
    current_caller.set(Caller("t", "write"))
    tools = anyio.run(build_mcp(store, "").list_tools)
    assert len(tools) == 8
    assert all((t.meta or {}).get("anthropic/alwaysLoad") is True for t in tools)
