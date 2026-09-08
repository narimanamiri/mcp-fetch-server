"""Tests for the retrieval evaluation harness."""

from __future__ import annotations

import json

import pytest

from mcp_fetch_server.__main__ import main as cli_main
from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord, make_doc_id
from mcp_fetch_server.rag.evaluate import (
    CaseResult,
    EvalError,
    EvalReport,
    GoldenCase,
    golden_from_corpus,
    load_golden,
    run_evaluation,
    write_golden,
)
from mcp_fetch_server.rag.retrieve import RetrievalResult
from mcp_fetch_server.rag.store import SearchHit


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "corpus") as instance:
        yield instance


def add(catalog: Catalog, *, url: str, questions: list[str]) -> DocumentRecord:
    digest, blob_path = catalog.store_blob("# T\n\nBody.")
    record = DocumentRecord(
        doc_id=make_doc_id(url),
        url=url,
        content_hash=digest,
        doctype="markdown",
        blob_path=blob_path,
        source_path=f"/corpus/{url[-1]}.md",
        title="Doc",
        questions=questions,
        summary="s",
    )
    catalog.upsert_document(record)
    return record


class FakeRetriever:
    """Returns a scripted ranking so metrics can be asserted exactly."""

    def __init__(self, ranking: list[list[str]]):
        self.ranking = ranking
        self.calls = 0
        self.kwargs: list[dict] = []

    async def search(self, question, **kwargs):
        self.kwargs.append(kwargs)
        docs = self.ranking[self.calls] if self.calls < len(self.ranking) else []
        self.calls += 1
        return RetrievalResult(
            query=question,
            hits=[
                SearchHit(chunk_id=f"{doc}:0", doc_id=doc, score=1.0, url=f"https://x/{doc}")
                for doc in docs
            ],
        )

    def close(self):
        pass


# ----------------------------------------------------------------- cases


def test_case_matches_on_doc_id():
    case = GoldenCase(question="q", doc_ids=["abc"])
    assert case.matches("abc", "https://x", "text")
    assert not case.matches("other", "https://x", "text")


def test_case_matches_on_url_prefix():
    case = GoldenCase(question="q", urls=["https://local.archive/doc/guide"])
    assert case.matches("any", "https://local.archive/doc/guide-1234#p2", "t")
    assert not case.matches("any", "https://local.archive/doc/other", "t")


def test_case_matches_on_substring_case_insensitively():
    case = GoldenCase(question="q", must_contain="Dense Retriever")
    assert case.matches("any", "u", "we use dense retrievers here")
    assert not case.matches("any", "u", "unrelated")


def test_case_needs_a_question_and_an_expectation():
    assert not GoldenCase(question="", doc_ids=["a"]).is_usable
    assert not GoldenCase(question="q").is_usable
    assert GoldenCase(question="q", must_contain="x").is_usable


# --------------------------------------------------------------- metrics


def case_result(rank):
    return CaseResult(case=GoldenCase(question="q", doc_ids=["a"]), hit_rank=rank)


def test_metrics_for_a_perfect_run():
    report = EvalReport(results=[case_result(1) for _ in range(4)], top_k=5)
    assert report.recall == 1.0
    assert report.hit_at_1 == 1.0
    assert report.mrr == 1.0
    assert report.ndcg == 1.0


def test_metrics_for_a_total_miss():
    report = EvalReport(results=[case_result(None) for _ in range(3)], top_k=5)
    assert report.recall == 0.0
    assert report.mrr == 0.0
    assert report.ndcg == 0.0
    assert len(report.misses) == 3


def test_rank_two_scores_below_rank_one():
    first = EvalReport(results=[case_result(1)])
    second = EvalReport(results=[case_result(2)])
    assert second.mrr < first.mrr
    assert second.ndcg < first.ndcg
    assert second.recall == first.recall  # both found it


def test_empty_report():
    report = EvalReport()
    assert report.total == 0
    assert report.recall == 0.0
    assert "No evaluation cases" in report.render()


def test_report_renders_and_serialises():
    report = EvalReport(results=[case_result(1), case_result(None)], top_k=5)
    rendered = report.render()
    assert "recall@5" in rendered
    assert "Missed 1" in rendered

    payload = report.as_dict()
    assert payload["cases"] == 2
    assert payload["recall@5"] == 0.5
    assert payload["misses"] == ["q"]


# ---------------------------------------------------------- golden files


def test_load_golden_reads_jsonl(tmp_path):
    path = tmp_path / "golden.jsonl"
    path.write_text(
        '# a comment\n'
        '{"question": "one", "doc_ids": ["a"]}\n'
        "\n"
        '{"question": "two", "must_contain": "text"}\n',
        encoding="utf-8",
    )
    cases = load_golden(path)
    assert [case.question for case in cases] == ["one", "two"]


def test_load_golden_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(EvalError, match="not valid JSON"):
        load_golden(path)


def test_load_golden_rejects_cases_with_no_expectation(tmp_path):
    path = tmp_path / "weak.jsonl"
    path.write_text('{"question": "no expectation"}\n', encoding="utf-8")
    with pytest.raises(EvalError, match="doc_ids"):
        load_golden(path)


def test_load_golden_rejects_an_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(EvalError, match="no cases"):
        load_golden(path)


def test_missing_golden_file(tmp_path):
    with pytest.raises(EvalError, match="Could not read"):
        load_golden(tmp_path / "absent.jsonl")


def test_write_then_load_round_trips(tmp_path):
    cases = [
        GoldenCase(question="سوال فارسی", doc_ids=["fa1"]),
        GoldenCase(question="english", must_contain="text"),
    ]
    path = write_golden(cases, tmp_path / "out.jsonl")
    loaded = load_golden(path)
    assert loaded[0].question == "سوال فارسی"
    assert loaded[0].doc_ids == ["fa1"]
    assert loaded[1].must_contain == "text"


# ----------------------------------------------------- golden from corpus


def test_golden_from_corpus_uses_enrichment_questions(catalog):
    """Enrichment already asked which questions each document answers, so
    those questions paired with their document are a free evaluation set."""
    first = add(catalog, url="https://x/1", questions=["What is A?", "How does A work?"])
    add(catalog, url="https://x/2", questions=["What is B?"])

    cases = golden_from_corpus(catalog)
    assert len(cases) == 3
    assert all(case.doc_ids for case in cases)
    a_cases = [case for case in cases if case.doc_ids == [first.doc_id]]
    assert len(a_cases) == 2


def test_golden_from_corpus_respects_a_limit(catalog):
    add(catalog, url="https://x/1", questions=[f"q{index}" for index in range(10)])
    assert len(golden_from_corpus(catalog, limit=4)) == 4


def test_golden_from_corpus_on_unenriched_documents(catalog):
    add(catalog, url="https://x/1", questions=[])
    assert golden_from_corpus(catalog) == []


# ------------------------------------------------------------ evaluation


async def test_evaluation_scores_a_perfect_ranking():
    cases = [GoldenCase(question="q1", doc_ids=["a"]), GoldenCase(question="q2", doc_ids=["b"])]
    retriever = FakeRetriever([["a", "z"], ["b", "z"]])
    report = await run_evaluation(cases, retriever=retriever, top_k=5)

    assert report.recall == 1.0
    assert report.hit_at_1 == 1.0
    assert retriever.calls == 2


async def test_evaluation_records_the_rank():
    cases = [GoldenCase(question="q", doc_ids=["target"])]
    report = await run_evaluation(
        cases, retriever=FakeRetriever([["x", "y", "target"]]), top_k=5
    )
    assert report.results[0].hit_rank == 3
    assert report.recall == 1.0
    assert report.hit_at_1 == 0.0


async def test_evaluation_records_a_miss():
    cases = [GoldenCase(question="q", doc_ids=["target"])]
    report = await run_evaluation(cases, retriever=FakeRetriever([["x", "y"]]), top_k=5)
    assert report.results[0].found is False
    assert report.misses[0].case.question == "q"


async def test_a_failing_case_is_a_miss_not_a_crash():
    class Exploding(FakeRetriever):
        async def search(self, question, **kwargs):
            raise RuntimeError("index is down")

    report = await run_evaluation(
        [GoldenCase(question="q", doc_ids=["a"])], retriever=Exploding([]), top_k=5
    )
    assert report.total == 1
    assert report.recall == 0.0


async def test_evaluation_passes_flags_through():
    retriever = FakeRetriever([["a"]])
    await run_evaluation(
        [GoldenCase(question="q", doc_ids=["a"])],
        retriever=retriever,
        top_k=3,
        expand=True,
        rerank=False,
    )
    assert retriever.kwargs[0] == {"top_k": 3, "expand": True, "rerank": False}


async def test_settings_note_records_the_configuration():
    report = await run_evaluation(
        [GoldenCase(question="q", doc_ids=["a"])],
        retriever=FakeRetriever([["a"]]),
        expand=True,
        rerank=False,
    )
    assert "expand" in report.settings_note
    assert "no-rerank" in report.settings_note


# ------------------------------------------------------------------- cli


def test_cli_eval_requires_a_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    with pytest.raises(SystemExit):
        cli_main(["eval"])


def test_cli_eval_from_corpus_without_enrichment(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    assert cli_main(["eval", "--from-corpus"]) == 1
    assert "enrich" in capsys.readouterr().err


def test_cli_eval_reports_a_bad_golden_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    path = tmp_path / "bad.jsonl"
    path.write_text("{oops}\n", encoding="utf-8")
    assert cli_main(["eval", str(path)]) == 1
    assert "Error:" in capsys.readouterr().err


def test_cli_eval_json_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    path = tmp_path / "golden.jsonl"
    path.write_text('{"question": "q", "doc_ids": ["a"]}\n', encoding="utf-8")

    async def fake_run_evaluation(cases, **kwargs):
        return EvalReport(results=[case_result(1)], top_k=kwargs.get("top_k", 8))

    monkeypatch.setattr("mcp_fetch_server.rag.evaluate.run_evaluation", fake_run_evaluation)
    assert cli_main(["eval", str(path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cases"] == 1
