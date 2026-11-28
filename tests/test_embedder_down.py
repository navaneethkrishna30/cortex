"""An API embedder can fail or be slow: notes still save, search falls back to keywords, vectors come later."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from cortex.auth import AuthStore
from cortex.config import load_settings
from cortex.embed import VOYAGE_EMBED_URL, HashEmbedder, VoyageEmbedder
from cortex.search import search
from cortex.server import build_apps


class Flaky(HashEmbedder):
    """A HashEmbedder that can be switched off, like an API that is down."""

    def __init__(self):
        super().__init__()
        self.down = False
        self.calls = 0

    def embed(self, texts, kind="document", patient=False):
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("voyage unreachable")
        return super().embed(texts, kind, patient)


@pytest.fixture
def flaky(store):
    store.index.embedder = Flaky()
    return store.index.embedder


def test_a_write_saves_and_is_found_by_keyword_while_the_embedder_is_down(store, flaky):
    flaky.down = True
    store.write("memory/funnel.md", "Funnel publishes the MCP listener.\n", actor="a")
    assert (store.vault.root / "memory/funnel.md").is_file()
    reply = search(store.index, "Funnel listener")
    assert reply["results"][0]["path"] == "memory/funnel.md"
    assert reply["semantic"] is False
    assert store.index.pending_count() == 1


def test_backfill_gives_the_vectors_once_the_embedder_is_back(store, flaky):
    flaky.down = True
    store.write("memory/funnel.md", "Funnel publishes the MCP listener.\n", actor="a")
    assert store.index.backfill() == 0
    flaky.down = False
    assert store.index.backfill() == 1
    assert store.index.pending_count() == 0
    assert "semantic" not in search(store.index, "Funnel listener")


def test_a_short_embedder_reply_leaves_the_note_pending_instead_of_looping(store, flaky):
    flaky.down = True
    store.write("memory/funnel.md", "Funnel publishes the MCP listener.\n", actor="a")
    flaky.down = False
    flaky.embed = lambda texts, kind="document", patient=False: []
    assert store.index.backfill() == 0
    assert store.index.pending_count() == 1


def test_an_outside_edit_while_the_embedder_is_down_is_still_indexed(store, flaky):
    flaky.down = True
    (store.vault.root / "docs").mkdir()
    (store.vault.root / "docs/hand.md").write_text("# Hand\nEdited over SSH\n")
    assert store.sync_outside_edits() == ["docs/hand.md"]
    assert search(store.index, "SSH", "all")["results"]
    assert store.index.pending_count() == 1


def test_a_sync_embeds_many_notes_in_one_call(store, flaky):
    (store.vault.root / "docs").mkdir()
    for n in range(20):
        (store.vault.root / f"docs/n{n}.md").write_text(f"# Note {n}\nBody {n}\n")
    flaky.calls = 0
    store.index.sync()
    assert flaky.calls == 1
    assert store.index.pending_count() == 0


def test_a_note_rewritten_during_backfill_keeps_only_fresh_vectors(store, flaky):
    flaky.down = True
    store.write("memory/a.md", "Old text about backups.\n", actor="a")
    flaky.down = False
    rows_before = store.index.db.execute("SELECT id FROM chunks WHERE path='memory/a.md'").fetchall()
    store.write("memory/a.md", "New text about restores.\n", store.vault.hash("memory/a.md"), actor="a")
    assert store.index.backfill() == 0
    vectors = store.index.db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0]
    chunks = store.index.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert vectors == chunks and rows_before


def test_healthz_reports_the_embedder_and_pending(store, settings, flaky):
    flaky.down = True
    store.write("memory/funnel.md", "Funnel publishes the MCP listener.\n", actor="a")
    _, ui = build_apps(store, AuthStore(settings.auth_db), settings)
    body = TestClient(ui, base_url="https://tailnet").get("/healthz").json()
    assert body["ok"] is True and body["pending"] == 1 and body["embedder"] == "hash:128"


def voyage(handler, monkeypatch, key="test-key"):
    monkeypatch.setenv("VOYAGE_API_KEY", key)
    return VoyageEmbedder("voyage-4-lite", 4, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_voyage_sends_kind_and_dimension_and_batches(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url == VOYAGE_EMBED_URL
        assert request.headers["authorization"] == "Bearer test-key"
        data = [{"index": i, "embedding": [float(i)] * 4} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data))})

    embedder = voyage(handler, monkeypatch)
    vectors = embedder.embed([f"t{i}" for i in range(130)], "document")
    assert len(vectors) == 130 and vectors[1] == [1.0] * 4
    assert [len(b["input"]) for b in seen] == [128, 2]
    assert seen[0]["input_type"] == "document" and seen[0]["output_dimension"] == 4
    embedder.embed(["q"], "query")
    assert seen[-1]["input_type"] == "query"


def test_voyage_needs_no_network_to_start_and_only_backfill_retries(monkeypatch):
    ok = {"data": [{"index": 0, "embedding": [0.0] * 4}]}
    replies = [httpx.Response(429), httpx.Response(200, json=ok), httpx.Response(429)]
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return replies.pop(0)

    monkeypatch.setattr("cortex.voyage.time.sleep", lambda s: None)
    embedder = voyage(handler, monkeypatch)
    assert embedder.dimensions == 4 and embedder.name == "voyage:voyage-4-lite"
    assert embedder.embed(["x"], patient=True) == [[0.0] * 4]
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed(["x"], "query")
    assert timeouts == [30.0, 30.0, 5.0]


def test_backfill_after_an_outside_edit_runs_without_the_store_lock(store, flaky):
    held = []
    flaky.embed = lambda texts, kind="document", patient=False: (
        held.append(store.lock._is_owned()) or HashEmbedder().embed(texts)
    )
    (store.vault.root / "docs").mkdir()
    (store.vault.root / "docs/hand.md").write_text("# Hand\nEdited over SSH\n")
    store.sync_outside_edits()
    assert held == [False]


def test_voyage_without_a_key_fails_the_call_not_the_start(monkeypatch):
    embedder = voyage(lambda request: httpx.Response(200), monkeypatch, key="")
    with pytest.raises(RuntimeError):
        embedder.embed(["x"])


def test_embedding_choice_comes_from_the_environment(tmp_path, monkeypatch):
    assert load_settings(tmp_path).embedding_provider == "voyage"
    monkeypatch.setenv("CORTEX_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("CORTEX_EMBEDDING_DIMENSIONS", "512")
    settings = load_settings(tmp_path)
    assert settings.embedding_provider == "hash" and settings.embedding_dimensions == 512
