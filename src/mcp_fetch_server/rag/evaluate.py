"""Retrieval evaluation.

Every accuracy change in this system is a guess until it is measured. Turning
reranking on, widening the context window, changing the chunk budget: each
sounds like an improvement and any of them can make retrieval worse on a
particular corpus. This module makes that checkable.

A golden set is JSONL, one case per line::

    {"question": "how are chunks split?", "doc_ids": ["ab12"], "must_contain": "heading"}

``doc_ids`` or ``urls`` name the documents that should be retrieved;
``must_contain`` is a substring that should appear in the retrieved text.

A golden set can also be built from the corpus itself. Enrichment already
asked the local model which questions each document answers, so those
questions, paired with the document they came from, are a real evaluation set
that costs nothing to produce.

Metrics reported are recall@k (was the right document found at all), MRR (how
near the top) and nDCG@k (rank-weighted, so being second beats being eighth).
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_fetch_server.rag.catalog import Catalog
from mcp_fetch_server.rag.retrieve import Retriever

logger = logging.getLogger(__name__)


class EvalError(Exception):
    """Raised when a golden set cannot be read or is unusable."""


@dataclass(slots=True)
class GoldenCase:
    question: str
    doc_ids: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    must_contain: str = ""

    def matches(self, doc_id: str, url: str, text: str) -> bool:
        if self.doc_ids and doc_id in self.doc_ids:
            return True
        if self.urls and any(url.startswith(expected) for expected in self.urls):
            return True
        if self.must_contain and self.must_contain.lower() in text.lower():
            return True
        return False

    @property
    def is_usable(self) -> bool:
        return bool(self.question.strip()) and bool(
            self.doc_ids or self.urls or self.must_contain
        )


@dataclass(slots=True)
class CaseResult:
    case: GoldenCase
    hit_rank: int | None  # 1-based rank of the first correct hit, None if missed
    retrieved: int = 0
    duration: float = 0.0
    top_title: str = ""

    @property
    def found(self) -> bool:
        return self.hit_rank is not None

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.hit_rank if self.hit_rank else 0.0

    @property
    def ndcg(self) -> float:
        """nDCG with a single relevant document, so the ideal DCG is 1."""
        return 1.0 / math.log2(self.hit_rank + 1) if self.hit_rank else 0.0


@dataclass(slots=True)
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)
    top_k: int = 8
    duration: float = 0.0
    settings_note: str = ""

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def recall(self) -> float:
        return self._mean(result.found for result in self.results)

    @property
    def mrr(self) -> float:
        return self._mean(result.reciprocal_rank for result in self.results)

    @property
    def ndcg(self) -> float:
        return self._mean(result.ndcg for result in self.results)

    @property
    def hit_at_1(self) -> float:
        return self._mean(result.hit_rank == 1 for result in self.results)

    @property
    def median_latency(self) -> float:
        if not self.results:
            return 0.0
        latencies = sorted(result.duration for result in self.results)
        return latencies[len(latencies) // 2]

    def _mean(self, values) -> float:
        collected = [float(value) for value in values]
        return sum(collected) / len(collected) if collected else 0.0

    @property
    def misses(self) -> list[CaseResult]:
        return [result for result in self.results if not result.found]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cases": self.total,
            "top_k": self.top_k,
            f"recall@{self.top_k}": round(self.recall, 4),
            "hit@1": round(self.hit_at_1, 4),
            "mrr": round(self.mrr, 4),
            f"ndcg@{self.top_k}": round(self.ndcg, 4),
            "median_latency_seconds": round(self.median_latency, 3),
            "duration_seconds": round(self.duration, 2),
            "settings": self.settings_note,
            "misses": [result.case.question for result in self.misses],
        }

    def render(self) -> str:
        if not self.total:
            return "No evaluation cases were run."
        lines = [
            f"{self.total} case(s), top_k={self.top_k}{self.settings_note}",
            "",
            f"  recall@{self.top_k}  {self.recall:6.1%}   the right document was found at all",
            f"  hit@1       {self.hit_at_1:6.1%}   it was ranked first",
            f"  MRR         {self.mrr:6.3f}   how near the top, on average",
            f"  nDCG@{self.top_k}    {self.ndcg:6.3f}   rank-weighted",
            "",
            f"  median latency {self.median_latency * 1000:.0f} ms"
            f"   ({self.duration:.1f}s total)",
        ]
        if self.misses:
            lines.append("")
            lines.append(f"Missed {len(self.misses)}:")
            lines.extend(f"  - {result.case.question[:90]}" for result in self.misses[:15])
            if len(self.misses) > 15:
                lines.append(f"  ... and {len(self.misses) - 15} more")
        return "\n".join(lines)


def load_golden(path: Path | str) -> list[GoldenCase]:
    """Read a JSONL golden set, skipping blank lines and comments."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"Could not read golden set {target}: {exc}") from exc

    cases: list[GoldenCase] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            payload = json.loads(stripped)
        except ValueError as exc:
            raise EvalError(f"{target}:{number} is not valid JSON: {exc}") from exc

        case = GoldenCase(
            question=str(payload.get("question") or ""),
            doc_ids=[str(item) for item in payload.get("doc_ids") or []],
            urls=[str(item) for item in payload.get("urls") or []],
            must_contain=str(payload.get("must_contain") or ""),
        )
        if not case.is_usable:
            raise EvalError(
                f"{target}:{number} needs a question and one of doc_ids, urls or must_contain"
            )
        cases.append(case)

    if not cases:
        raise EvalError(f"{target} contains no cases")
    return cases


def golden_from_corpus(catalog: Catalog, *, limit: int | None = None) -> list[GoldenCase]:
    """Build a golden set from the questions enrichment already produced.

    Each question was written by the model *from* a specific document, so that
    document is the expected answer. It is not a substitute for questions real
    users asked, but it is free, it is in the corpus's own vocabulary, and it
    catches regressions.
    """
    cases: list[GoldenCase] = []
    for record in catalog.iter_documents():
        for question in record.questions:
            if not question.strip():
                continue
            cases.append(GoldenCase(question=question.strip(), doc_ids=[record.doc_id]))
            if limit is not None and len(cases) >= limit:
                return cases
    return cases


def write_golden(cases: Sequence[GoldenCase], path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "question": case.question,
                **({"doc_ids": case.doc_ids} if case.doc_ids else {}),
                **({"urls": case.urls} if case.urls else {}),
                **({"must_contain": case.must_contain} if case.must_contain else {}),
            },
            ensure_ascii=False,
        )
        for case in cases
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


async def run_evaluation(
    cases: Sequence[GoldenCase],
    *,
    retriever: Retriever | None = None,
    top_k: int = 8,
    expand: bool = False,
    rerank: bool | None = None,
    on_result=None,
) -> EvalReport:
    """Run every case and report retrieval quality."""
    started = time.perf_counter()
    active = retriever or Retriever()
    owns_retriever = retriever is None

    notes: list[str] = []
    if expand:
        notes.append("expand")
    if rerank is not None:
        notes.append("rerank" if rerank else "no-rerank")
    report = EvalReport(
        top_k=top_k, settings_note=f", {', '.join(notes)}" if notes else ""
    )

    try:
        for case in cases:
            case_started = time.perf_counter()
            rank: int | None = None
            hits = []
            try:
                result = await active.search(
                    case.question, top_k=top_k, expand=expand, rerank=rerank
                )
                hits = result.hits
                for position, hit in enumerate(hits, start=1):
                    if case.matches(hit.doc_id, hit.url, hit.text):
                        rank = position
                        break
            except Exception as exc:
                logger.warning("Evaluation case failed (%s): %s", case.question[:60], exc)

            outcome = CaseResult(
                case=case,
                hit_rank=rank,
                retrieved=len(hits),
                duration=time.perf_counter() - case_started,
                top_title=hits[0].title if hits else "",
            )
            report.results.append(outcome)
            if on_result is not None:
                on_result(outcome)
    finally:
        if owns_retriever:
            active.close()

    report.duration = time.perf_counter() - started
    return report
