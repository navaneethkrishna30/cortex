"""Telemetry is off and free unless an OTLP endpoint is set; on, it measures tool calls, search,
Voyage, backfill and sync without ever carrying note text, queries or paths."""

import json
import subprocess
import sys

import anyio
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cortex import telemetry
from cortex.embed import VoyageEmbedder
from cortex.mcp_server import Caller, build_mcp, current_caller

SECRET_WORDS = "zanzibar"


def test_off_never_imports_the_sdk_or_exporters(tmp_path):
    """A search and a sync with the endpoint unset, in a clean interpreter. (The API alone is
    already loaded by the mcp library, and is a no-op without a provider.)"""
    script = f"""
import os, sys
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
from pathlib import Path
from cortex import telemetry
from cortex.config import Settings
from cortex.embed import HashEmbedder
from cortex.index import Index
from cortex.search import search
from cortex.server import build_apps
from cortex.store import Store
from cortex.vault import Vault
vault = Path({str(tmp_path)!r}) / "vault"
(vault / "memory").mkdir(parents=True)
(vault / "memory/tea.md").write_text("Prefers green tea.\\n")
settings = Settings(vault=vault, auth_db=vault.parent / "auth.sqlite")
store = Store(Vault(settings), Index(Vault(settings), HashEmbedder()))
store.sync_outside_edits()
assert telemetry.setup("test") is False
search(store.index, "tea")
print(sorted(m for m in sys.modules if m.startswith(("opentelemetry.sdk", "opentelemetry.exporter"))))
"""
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


@pytest.fixture
def exported(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    spans, reader, logs = InMemorySpanExporter(), InMemoryMetricReader(), InMemoryLogRecordExporter()
    assert telemetry.setup("test", exporters=(spans, reader, logs)) is True
    yield spans, reader
    telemetry.shutdown()


def metric_points(reader) -> dict[str, list]:
    found: dict[str, list] = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found.setdefault(metric.name, []).extend(metric.data.data_points)
    return found


def finished(spans) -> list:
    telemetry._providers[0].force_flush()
    return spans.get_finished_spans()


def call(mcp, tool, arguments):
    return anyio.run(mcp.call_tool, tool, arguments)


def test_a_tool_call_is_one_span_with_search_inside_and_counted_by_outcome(store, exported):
    spans, reader = exported
    store.write("memory/trip.md", f"Nav visits {SECRET_WORDS} in spring.\n", actor="a")
    mcp = build_mcp(store, "")
    current_caller.set(Caller("t", "read"))
    call(mcp, "search", {"query": SECRET_WORDS})
    with pytest.raises(ToolError):
        call(mcp, "write", {"path": "memory/x.md", "content": "x"})

    done = finished(spans)
    (tool,) = [s for s in done if s.name == "mcp.tool" and s.attributes["mcp.tool.name"] == "search"]
    (search,) = [s for s in done if s.name == "search"]
    assert search.parent.span_id == tool.context.span_id
    assert search.attributes["search.semantic"] is True and search.attributes["search.reranked"] is False
    assert "search.keyword_ms" in search.attributes and "search.total_ms" in search.attributes

    points = metric_points(reader)
    calls = {(p.attributes["tool"], p.attributes["outcome"]): p.value for p in points["cortex.tool.calls"]}
    assert calls == {("search", "ok"): 1, ("write", "refused"): 1}
    stages = {p.attributes["stage"] for p in points["cortex.search.duration"]}
    assert stages == {"embed", "keyword", "vector", "total"}
    searches = points["cortex.search.count"]
    assert [dict(p.attributes) for p in searches] == [{"semantic": True, "reranked": False, "exact": False}]

    # Nothing about the vault leaves the server.
    blob = json.dumps([dict(s.attributes or {}) for s in finished(spans)]) + str(points)
    assert SECRET_WORDS not in blob and "memory/" not in blob


def test_a_refusal_and_a_crash_are_told_apart(store, exported, monkeypatch):
    _, reader = exported
    mcp = build_mcp(store, "")
    current_caller.set(Caller("t", "write"))
    with pytest.raises(ToolError):
        call(mcp, "read", {"path": "memory/missing.md"})
    monkeypatch.setattr(store, "list", lambda folder: 1 / 0)
    with pytest.raises(ToolError):
        call(mcp, "list", {"folder": ""})
    calls = {(p.attributes["tool"], p.attributes["outcome"]) for p in metric_points(reader)["cortex.tool.calls"]}
    assert calls == {("read", "refused"), ("list", "error")}


def test_voyage_requests_count_rate_limits_and_batch_sizes(exported, monkeypatch):
    _, reader = exported
    ok = {"data": [{"index": 0, "embedding": [0.0] * 4}, {"index": 1, "embedding": [1.0] * 4}]}
    replies = [httpx.Response(429), httpx.Response(200, json=ok)]
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    monkeypatch.setattr("cortex.voyage.time.sleep", lambda s: None)
    transport = httpx.MockTransport(lambda request: replies.pop(0))
    embedder = VoyageEmbedder("voyage-4-lite", 4, client=httpx.Client(transport=transport))
    assert len(embedder.embed(["a", "b"], patient=True)) == 2

    points = metric_points(reader)
    outcomes = {p.attributes["outcome"]: p.value for p in points["cortex.voyage.requests"]}
    assert outcomes == {"429": 1, "ok": 1}
    (batch,) = points["cortex.voyage.batch_size"]
    assert batch.count == 2 and batch.sum == 4


def test_backfill_sync_and_pending_are_measured(store, exported):
    _, reader = exported
    (store.vault.root / "docs").mkdir()
    (store.vault.root / "docs/hand.md").write_text("# Hand\nEdited over SSH\n")
    telemetry.watch_pending(store.index.pending_count)
    store.sync_outside_edits()
    points = metric_points(reader)
    assert sum(p.value for p in points["cortex.sync.changed"]) == 1
    assert sum(p.value for p in points["cortex.backfill.chunks"]) >= 1
    assert [p.value for p in points["cortex.pending.notes"]] == [0]
