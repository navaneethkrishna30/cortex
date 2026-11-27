import json
import shutil
import sys
from pathlib import Path

import pytest

from cortex.config import Settings
from cortex.embed import HashEmbedder
from cortex.evaluation import Question, first_hit, load_questions, percentile, run, summarise
from cortex.index import Index
from cortex.vault import Vault

SAMPLE = Path(__file__).parent / "eval"


@pytest.fixture
def sample_vault(tmp_path):
    """A copy, since opening a vault writes its index and git repo in place."""
    vault = tmp_path / "vault"
    shutil.copytree(SAMPLE / "vault", vault, ignore=shutil.ignore_patterns(".cortex"))
    (vault / ".cortex").mkdir()
    (vault / ".cortex/config.toml").write_text('[embedding]\nprovider = "hash"\n')
    return vault


@pytest.fixture
def sample_index(sample_vault, tmp_path):
    index = Index(Vault(Settings(vault=sample_vault, auth_db=tmp_path / "auth.sqlite")), HashEmbedder())
    index.sync()
    return index


# Metrics


def test_percentile_is_nearest_rank():
    values = list(range(1, 101))
    assert (percentile(values, 50), percentile(values, 95), percentile(values, 99)) == (50, 95, 99)
    assert percentile([7.0], 99) == 7.0
    assert percentile([3, 1, 2], 50) == 2
    assert percentile([], 50) == 0.0


def test_first_hit_takes_the_best_of_any_expected_path():
    got = ["a.md", "b.md", "c.md"]
    assert first_hit(["c.md"], got) == 3
    assert first_hit(["c.md", "b.md"], got) == 2
    assert first_hit(["z.md"], got) is None


def test_summary_metrics():
    outcomes = [
        {"question": "q1", "expected": ["a"], "got": ["a"], "rank": 1, "ms": 10.0, "reranked": True},
        {"question": "q2", "expected": ["b"], "got": ["x", "y", "b"], "rank": 3, "ms": 20.0, "reranked": True},
        {"question": "q3", "expected": ["c"], "got": ["x"], "rank": None, "ms": 30.0, "reranked": False},
        {"question": "q4", "expected": ["d", "e"], "got": ["x", "e", "y", "z"], "rank": 2, "ms": 40.0, "reranked": False},
    ]
    report = summarise(outcomes)
    assert report["questions"] == 4
    assert report["hit@1"] == 1 and report["hit@8"] == 3
    assert report["mrr"] == pytest.approx((1 + 1 / 3 + 0 + 1 / 2) / 4)
    assert report["latency_ms"] == {"p50": 20.0, "p95": 40.0, "p99": 40.0}
    assert report["reranked"] == 2
    assert [m["question"] for m in report["misses"]] == ["q2", "q3", "q4"]
    assert report["misses"][2] == {"question": "q4", "expected": ["d", "e"], "got": ["x", "e", "y"], "rank": 2}


def test_rank_past_eight_is_not_a_hit_at_eight():
    outcome = {"question": "q", "expected": ["a"], "got": [], "rank": 9, "ms": 1.0, "reranked": False}
    assert summarise([outcome])["hit@8"] == 0


# Question file


def test_single_path_file_still_loads():
    questions = load_questions(SAMPLE / "questions.tsv")
    assert len(questions) == 10
    assert questions[0] == Question("which database do I like for small projects", ["memory/favourite-database.md"])


def test_multiple_expected_paths(tmp_path):
    file = tmp_path / "q.tsv"
    file.write_text("# comment\n\nwhat editor\tmemory/editor.md | memory/vim.md\nwho\tentities/a.md\textra column\n")
    assert load_questions(file) == [
        Question("what editor", ["memory/editor.md", "memory/vim.md"]),
        Question("who", ["entities/a.md"]),
    ]


# On the sample vault


def test_run_on_the_sample_vault(sample_index):
    report = run(sample_index, load_questions(SAMPLE / "questions.tsv"))
    assert report["questions"] == 10
    assert 0 <= report["hit@1"] <= report["hit@8"] <= 10
    assert 0.0 <= report["mrr"] <= 1.0
    assert report["latency_ms"]["p50"] <= report["latency_ms"]["p95"] <= report["latency_ms"]["p99"]
    assert report["reranked"] == 0
    assert all(len(m["got"]) <= 3 for m in report["misses"])
    assert len(report["misses"]) == 10 - report["hit@1"]


def test_run_counts_reranked_searches(sample_index):
    class Reverse:
        candidates = 30

        def scores(self, query, documents):
            return list(range(len(documents)))

    plain = run(sample_index, [Question("when is my birthday", ["memory/birthday.md"])])
    reranked = run(sample_index, [Question("when is my birthday", ["memory/birthday.md"])], reranker=Reverse())
    assert plain["reranked"] == 0 and reranked["reranked"] == 1


def test_multi_path_question_hits_on_either(sample_index):
    one = run(sample_index, [Question("when is my birthday", ["memory/nowhere.md", "memory/birthday.md"])])
    assert one["hit@8"] == 1


# Command line


def cortex(monkeypatch, *args):
    from cortex.cli import main

    monkeypatch.setattr(sys, "argv", ["cortex", *args])
    main()


def test_cli_json_output(sample_vault, monkeypatch, capsys):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    cortex(monkeypatch, "--vault", str(sample_vault), "eval", str(SAMPLE / "questions.tsv"), "--json", "--rerank", "off")
    report = json.loads(capsys.readouterr().out)
    assert report["questions"] == 10 and report["reranked"] == 0
    assert set(report) >= {"hit@1", "hit@8", "mrr", "latency_ms", "misses"}


def test_cli_text_output(sample_vault, monkeypatch, capsys):
    cortex(monkeypatch, "--vault", str(sample_vault), "eval", str(SAMPLE / "questions.tsv"))
    out = capsys.readouterr().out
    assert "hit@1" in out and "hit@8" in out and "MRR" in out and "p99" in out


def test_cli_rerank_on_without_a_key_says_so(sample_vault, monkeypatch, capsys):
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    cortex(monkeypatch, "--vault", str(sample_vault), "eval", str(SAMPLE / "questions.tsv"), "--json", "--rerank", "on")
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reranked"] == 0
    assert "not reranked" in captured.err
