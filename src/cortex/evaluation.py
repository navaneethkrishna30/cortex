"""Search eval: score search against questions with known answers, to compare settings."""

from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass
from pathlib import Path

from .index import Index
from .rerank import Reranker
from .search import DEFAULT_LIMIT, search

MISS_SHOWN = 3


@dataclass
class Question:
    text: str
    # Any of these counts as the answer, for questions more than one note can answer.
    expected: list[str]


def load_questions(file: str | Path) -> list[Question]:
    """TSV of question<TAB>expected, where expected is one path or `path1|path2`.

    Blank lines and lines starting with # are skipped; columns after the second are ignored.
    """
    with Path(file).open() as handle:
        rows = [r for r in csv.reader(handle, delimiter="\t") if r and not r[0].startswith("#")]
    return [Question(row[0], [p.strip() for p in row[1].split("|") if p.strip()]) for row in rows]


def first_hit(expected: list[str], got: list[str]) -> int | None:
    """1-based rank of the first result that is any expected path."""
    return next((rank for rank, path in enumerate(got, 1) if path in expected), None)


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: always one of the measured values, which suits small samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def summarise(outcomes: list[dict]) -> dict:
    """hit@1, hit@8 (counts), MRR, latency percentiles and the questions not answered first."""
    ranks = [o["rank"] for o in outcomes]
    latencies = [o["ms"] for o in outcomes]
    return {
        "questions": len(outcomes),
        "hit@1": sum(rank == 1 for rank in ranks),
        "hit@8": sum(rank is not None and rank <= 8 for rank in ranks),
        "mrr": sum(1 / rank for rank in ranks if rank) / len(outcomes) if outcomes else 0.0,
        "latency_ms": {f"p{p}": round(percentile(latencies, p), 1) for p in (50, 95, 99)},
        "reranked": sum(o["reranked"] for o in outcomes),
        "misses": [
            {"question": o["question"], "expected": o["expected"], "got": o["got"][:MISS_SHOWN], "rank": o["rank"]}
            for o in outcomes if o["rank"] != 1
        ],
    }


def run(index: Index, questions: list[Question], reranker: Reranker | None = None) -> dict:
    """Search every question as an agent would (scope all, 8 results) and summarise."""
    outcomes = []
    for question in questions:
        start = time.perf_counter()
        found = search(index, question.text, "all", limit=DEFAULT_LIMIT, reranker=reranker)
        ms = (time.perf_counter() - start) * 1000
        got = [r["path"] for r in found["results"]]
        outcomes.append({
            "question": question.text, "expected": question.expected, "got": got,
            "rank": first_hit(question.expected, got), "ms": ms, "reranked": found["reranked"],
        })
    return summarise(outcomes)


def format_report(report: dict) -> str:
    n = report["questions"]
    latency = report["latency_ms"]
    lines = [
        f"miss: {m['question']!r} wanted {'|'.join(m['expected'])}, got {m['got']}" for m in report["misses"]
    ]
    lines.append(
        f"hit@1 {report['hit@1']}/{n}  hit@8 {report['hit@8']}/{n}  MRR {report['mrr']:.3f}  "
        f"p50 {latency['p50']} ms  p95 {latency['p95']} ms  p99 {latency['p99']} ms  "
        f"reranked {report['reranked']}/{n}"
    )
    return "\n".join(lines)
