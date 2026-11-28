import logging
import threading

import httpx
import pytest

from cortex.config import Settings, load_settings
from cortex.rerank import VOYAGE_RERANK_URL, VoyageReranker, make_reranker
from cortex.search import search

FAKE_KEY = "test-key-not-real"


class FakeClient:
    """Stands in for httpx.Client: records each request and answers with `reply(request)`."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        request = {"url": url, "headers": headers, "json": json, "timeout": timeout}
        self.calls.append(request)
        answer = self.reply(request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))


def reversed_scores(request):
    """Ranks the last candidate first, so a note from below the usual cut must come up."""
    count = len(request["json"]["documents"])
    return {
        "object": "list",
        "data": [{"index": i, "relevance_score": round((i + 1) / count, 3)} for i in reversed(range(count))],
        "model": request["json"]["model"],
        "usage": {"total_tokens": 10},
    }


def reranker(reply=reversed_scores, timeout_s=3.0):
    client = FakeClient(reply)
    return VoyageReranker("rerank-3", FAKE_KEY, timeout_s, client=client), client


@pytest.fixture
def zebras(store):
    """Twelve notes that all match, more than the eight a search returns."""
    for n in range(12):
        store.write(f"memory/zebra-{n:02}.md", f"Zebra fact about topic{n} and thing{n}.", actor="a")
    return store


def paths(found):
    return [r["path"] for r in found["results"]]


def test_rerank_reorders_by_scores_and_promotes_from_below_the_cut(zebras):
    rrf = paths(search(zebras.index, "zebra", limit=30))
    assert len(rrf) == 12
    found = search(zebras.index, "zebra", reranker=reranker()[0])
    assert found["reranked"] is True
    assert paths(found) == list(reversed(rrf))[:8]
    assert rrf[-1] == paths(found)[0]
    assert found["results"][0]["rerank_score"] == 1.0


def test_request_matches_the_voyage_api(zebras):
    rerank, client = reranker()
    search(zebras.index, "zebra", reranker=rerank)
    [call] = client.calls
    assert call["url"] == VOYAGE_RERANK_URL == "https://api.voyageai.com/v1/rerank"
    assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert call["json"]["model"] == "rerank-3" and call["json"]["query"] == "zebra"
    assert len(call["json"]["documents"]) == 12
    assert all("Zebra fact" in document for document in call["json"]["documents"])
    assert call["timeout"] == 3.0


def test_candidates_are_capped(zebras):
    rerank, client = reranker()
    rerank.candidates = 10
    found = search(zebras.index, "zebra", reranker=rerank)
    assert len(client.calls[0]["json"]["documents"]) == 10
    assert len(found["results"]) == 8


def test_no_reranker_keeps_rrf_order_and_says_so(zebras):
    found = search(zebras.index, "zebra")
    assert found["reranked"] is False
    assert paths(found) == paths(search(zebras.index, "zebra", limit=30))[:8]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("refused"),
        httpx.Response(500, json={"detail": "boom"}, request=httpx.Request("POST", VOYAGE_RERANK_URL)),
        {"data": [{"index": 99, "relevance_score": 0.9}]},
        {"data": [{"index": 0, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.8}]},
        {"unexpected": True},
        {"data": [{"index": "0", "relevance_score": "high"}]},
    ],
)
def test_failures_keep_rrf_order(zebras, caplog, failure):
    expected = paths(search(zebras.index, "zebra"))
    rerank, client = reranker(lambda request: failure)
    with caplog.at_level(logging.WARNING, logger="cortex.rerank"):
        found = search(zebras.index, "zebra", reranker=rerank)
    assert len(client.calls) == 1
    assert found["reranked"] is False and paths(found) == expected
    assert "rerank failed" in caplog.text
    assert FAKE_KEY not in caplog.text


def test_failure_is_logged_once_per_call(zebras, caplog):
    rerank, _ = reranker(lambda request: httpx.ReadTimeout("timed out"))
    with caplog.at_level(logging.WARNING, logger="cortex.rerank"):
        search(zebras.index, "zebra", reranker=rerank)
    assert len([r for r in caplog.records if r.name == "cortex.rerank"]) == 1


def test_exact_is_never_reranked(zebras):
    rerank, client = reranker()
    found = search(zebras.index, "zebra", exact=True, reranker=rerank)
    assert client.calls == [] and found["reranked"] is False


def test_nothing_found_makes_no_call(store):
    rerank, client = reranker()
    found = search(store.index, "zebra", reranker=rerank)
    assert client.calls == [] and found["results"] == [] and found["reranked"] is False


def test_reranked_results_keep_links_and_backlinks(store):
    store.write("entities/server.md", "---\nstatus: current\n---\n# Server\nThe zebra server.\n", actor="a")
    store.write("entities/app.md", "---\nstatus: current\ndepends_on: server\n---\n# App\nThe zebra app.\n", actor="a")
    found = search(store.index, "zebra", reranker=reranker()[0])
    assert found["reranked"] is True
    by_path = {r["path"]: r for r in found["results"]}
    assert by_path["entities/app.md"]["links"] == ["entities/server.md"]
    assert by_path["entities/server.md"]["backlinks"] == ["entities/app.md"]


def test_index_lock_is_free_during_the_http_call(zebras):
    """A slow reranker must not hold up writes and other searches."""
    seen = []

    def reply(request):
        other = threading.Thread(
            target=lambda: seen.append(zebras.index.lock.acquire(timeout=1) and zebras.index.lock.release() is None)
        )
        other.start()
        other.join()
        return reversed_scores(request)

    search(zebras.index, "zebra", reranker=reranker(reply)[0])
    assert seen == [True]


# Settings and construction


def test_provider_none_builds_no_reranker(settings, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", FAKE_KEY)
    assert settings.rerank_provider == "none"
    assert make_reranker(settings) is None


def test_missing_key_builds_no_reranker(settings, monkeypatch, caplog):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    settings.rerank_provider = "voyage"
    with caplog.at_level(logging.WARNING, logger="cortex.rerank"):
        assert make_reranker(settings) is None
    assert "VOYAGE_API_KEY" in caplog.text


def test_voyage_reranker_from_settings(settings, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", FAKE_KEY)
    settings.rerank_provider = "voyage"
    settings.rerank_model = "rerank-3-lite"
    settings.rerank_timeout_s = 1.5
    settings.rerank_candidates = 20
    client = FakeClient(reversed_scores)
    built = make_reranker(settings, client=client)
    assert (built.model, built.timeout_s, built.candidates) == ("rerank-3-lite", 1.5, 20)
    assert FAKE_KEY not in repr(built)


def test_unknown_provider_is_refused(settings):
    settings.rerank_provider = "nope"
    with pytest.raises(ValueError):
        make_reranker(settings)


def test_rerank_settings_load_from_config(tmp_path):
    (tmp_path / ".cortex").mkdir()
    (tmp_path / ".cortex/config.toml").write_text(
        'rerank_provider = "voyage"\nrerank_model = "rerank-3-lite"\nrerank_timeout_s = 2\nrerank_candidates = 25\n'
    )
    loaded = load_settings(tmp_path)
    assert (loaded.rerank_provider, loaded.rerank_model, loaded.rerank_timeout_s, loaded.rerank_candidates) == (
        "voyage",
        "rerank-3-lite",
        2,
        25,
    )


def test_rerank_defaults():
    defaults = Settings(vault=None, auth_db=None)
    assert (defaults.rerank_provider, defaults.rerank_model, defaults.rerank_timeout_s, defaults.rerank_candidates) == (
        "none",
        "rerank-3",
        3.0,
        30,
    )
