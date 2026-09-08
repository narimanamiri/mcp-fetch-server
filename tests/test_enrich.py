"""Tests for enrichment and taxonomy classification."""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_fetch_server.__main__ import main as cli_main
from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord, make_doc_id
from mcp_fetch_server.rag.enrich import (
    GENRES,
    DocumentEnrichment,
    apply_enrichment,
    build_digest,
    build_messages,
    enrichment_schema,
    outline,
    run_enrichment,
)
from mcp_fetch_server.rag.llm import LocalLLM
from mcp_fetch_server.rag.taxonomy import (
    Category,
    Taxonomy,
    TaxonomyError,
    bootstrap_taxonomy,
    classification_schema,
    classify_document,
    run_classification,
)

OLLAMA = "http://localhost:11434"


def llm() -> LocalLLM:
    return LocalLLM(
        backend="ollama",
        base_url=OLLAMA,
        chat_model="gemma3:4b",
        embed_model="bge-m3",
        retries=0,
        timeout=5.0,
    )


def reply(payload: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": payload}})


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "corpus") as instance:
        yield instance


def add_document(catalog: Catalog, *, url: str, text: str, **kwargs) -> DocumentRecord:
    digest, blob_path = catalog.store_blob(text)
    defaults = dict(
        doc_id=make_doc_id(url),
        url=url,
        content_hash=digest,
        doctype="markdown",
        blob_path=blob_path,
        source_path=f"/corpus/{url.rsplit('/', 1)[-1]}.md",
        title="Untitled",
        language="en",
        char_count=len(text),
    )
    defaults.update(kwargs)
    record = DocumentRecord(**defaults)
    catalog.upsert_document(record)
    return record


ENRICHED_JSON = (
    '{"title": "Retrieval Guide", "summary": "Explains dense and sparse retrieval.", '
    '"genre": "manual", "language": "en", "published_at": "2024-03-01", '
    '"tags": ["retrieval", "bm25"], "entities": ["BM25"], '
    '"questions": ["What is dense retrieval?"]}'
)


# ------------------------------------------------------------- digesting


def test_outline_lists_headings_with_indentation():
    text = "# Top\n\nBody.\n\n## Sub\n\nMore.\n\n### Deep\n\nEven more."
    assert outline(text) == ["Top", "  Sub", "    Deep"]


def test_outline_is_capped():
    text = "\n\n".join(f"# Heading {index}" for index in range(200))
    assert len(outline(text, max_headings=10)) == 10


def test_build_digest_includes_short_documents_whole():
    text = "# Title\n\nShort body."
    digest = build_digest(text)
    assert "DOCUMENT:" in digest
    assert "Short body." in digest


def test_build_digest_takes_head_and_tail_of_long_documents():
    text = "# Title\n\n" + ("filler sentence. " * 3000) + "FINAL MARKER"
    digest = build_digest(text, head_chars=500, tail_chars=200)
    assert "BEGINNING:" in digest
    assert "END:" in digest
    assert "FINAL MARKER" in digest
    assert len(digest) < len(text)


def test_build_messages_mentions_the_document_language_instruction(catalog):
    record = add_document(catalog, url="https://x/1", text="# T\n\nBody.")
    messages = build_messages(record, "# T\n\nBody.")
    assert messages[0].role == "system"
    assert "same language as the document" in messages[1].content


# --------------------------------------------------------------- schema


def test_enrichment_schema_requires_every_field():
    """Regression: fields with Pydantic defaults land outside `required`, and
    gemma3:4b then omitted genre, language, entities and published_at
    entirely. The defaults silently filled in "other" and empty lists, so
    every document came back with genre="other" and no entities."""
    schema = enrichment_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert "entities" in schema["required"]
    assert "genre" in schema["required"]
    assert "published_at" in schema["required"]


def test_enrichment_schema_constrains_genre_to_an_enum():
    """A description listing the genres is not enough for a small model; the
    allowed values have to be an enum in the schema."""
    schema = enrichment_schema()
    assert schema["properties"]["genre"]["enum"] == list(GENRES)


def test_enrichment_schema_allows_a_null_date():
    assert schema_types(enrichment_schema(), "published_at") == ["string", "null"]


def schema_types(schema: dict, field: str) -> list[str]:
    value = schema["properties"][field]["type"]
    return value if isinstance(value, list) else [value]


@respx.mock
async def test_enrichment_pushes_the_hand_built_schema_to_the_model(catalog):
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=reply(ENRICHED_JSON))
    add_document(catalog, url="https://x/1", text="# Guide\n\nBody.")

    client = llm()
    try:
        await run_enrichment(catalog=catalog, llm=client)
    finally:
        await client.aclose()

    import json

    body = json.loads(route.calls[0].request.read())
    assert body["format"]["properties"]["genre"]["enum"] == list(GENRES)
    assert "entities" in body["format"]["required"]


# ----------------------------------------------------------- validation


def test_enrichment_normalises_partial_dates():
    assert DocumentEnrichment(title="t", summary="s", published_at="2024").published_at == (
        "2024-01-01"
    )
    assert DocumentEnrichment(title="t", summary="s", published_at="2024-03").published_at == (
        "2024-03-01"
    )


def test_enrichment_drops_unparseable_dates():
    """A malformed date is worse than none: it silently breaks date filters."""
    for value in ("recent", "2024?", "March 2024", ""):
        assert DocumentEnrichment(title="t", summary="s", published_at=value).published_at is None


def test_enrichment_falls_back_to_other_genre():
    assert DocumentEnrichment(title="t", summary="s", genre="thesis").genre == "other"
    assert DocumentEnrichment(title="t", summary="s", genre="Manual").genre == "manual"


def test_enrichment_deduplicates_and_caps_lists():
    result = DocumentEnrichment(
        title="t",
        summary="s",
        tags=["RAG", "rag", " rag ", *[f"tag{index}" for index in range(20)]],
    )
    assert result.tags[0] == "RAG"
    assert len(result.tags) == 12
    assert sum(1 for tag in result.tags if tag.lower() == "rag") == 1


def test_apply_enrichment_keeps_loader_language(catalog):
    record = add_document(catalog, url="https://x/1", text="# T\n\nBody.", language="fa")
    apply_enrichment(record, DocumentEnrichment(title="X", summary="S", language="en"))
    assert record.language == "fa"


def test_apply_enrichment_fills_unknown_language(catalog):
    record = add_document(catalog, url="https://x/1", text="# T\n\nBody.", language="und")
    apply_enrichment(record, DocumentEnrichment(title="X", summary="S", language="en"))
    assert record.language == "en"


def test_apply_enrichment_sets_fields_and_timestamp(catalog):
    record = add_document(catalog, url="https://x/1", text="# T\n\nBody.")
    apply_enrichment(
        record,
        DocumentEnrichment(
            title="Real Title", summary="A summary.", genre="paper", tags=["a"], questions=["q?"]
        ),
    )
    assert record.title == "Real Title"
    assert record.summary == "A summary."
    assert record.meta["genre"] == "paper"
    assert record.enriched_at is not None


# ------------------------------------------------------------- pipeline


@respx.mock
async def test_run_enrichment_updates_the_catalog(catalog):
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=reply(ENRICHED_JSON))
    add_document(catalog, url="https://x/1", text="# Guide\n\nDense retrieval explained.")

    client = llm()
    try:
        summary = await run_enrichment(catalog=catalog, llm=client)
    finally:
        await client.aclose()

    assert summary.enriched == 1
    stored = catalog.list_documents()[0]
    assert stored.title == "Retrieval Guide"
    assert stored.summary == "Explains dense and sparse retrieval."
    assert stored.tags == ["retrieval", "bm25"]
    assert stored.questions == ["What is dense retrieval?"]
    assert stored.published_at == "2024-03-01"
    assert stored.enriched_at is not None


@respx.mock
async def test_run_enrichment_skips_already_enriched(catalog):
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=reply(ENRICHED_JSON))
    add_document(catalog, url="https://x/1", text="# Guide\n\nBody.")

    client = llm()
    try:
        await run_enrichment(catalog=catalog, llm=client)
        second = await run_enrichment(catalog=catalog, llm=client)
    finally:
        await client.aclose()

    assert second.enriched == 0
    assert second.results == []
    assert route.call_count == 1


@respx.mock
async def test_reenrich_forces_another_pass(catalog):
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=reply(ENRICHED_JSON))
    add_document(catalog, url="https://x/1", text="# Guide\n\nBody.")

    client = llm()
    try:
        await run_enrichment(catalog=catalog, llm=client)
        await run_enrichment(catalog=catalog, llm=client, reenrich=True)
    finally:
        await client.aclose()
    assert route.call_count == 2


@respx.mock
async def test_one_failure_does_not_stop_the_pass(catalog):
    respx.post(f"{OLLAMA}/api/chat").mock(
        side_effect=[
            httpx.Response(500, text="model exploded"),
            httpx.Response(500, text="model exploded"),
            reply(ENRICHED_JSON),
        ]
    )
    add_document(catalog, url="https://x/1", text="# One\n\nBody.")
    add_document(catalog, url="https://x/2", text="# Two\n\nBody.", source_path="/corpus/2.md")

    client = llm()
    try:
        summary = await run_enrichment(catalog=catalog, llm=client)
    finally:
        await client.aclose()

    assert summary.enriched + summary.failed == 2
    assert summary.failed >= 1
    assert "Enriched" in summary.render()


async def test_enrichment_on_empty_catalog(catalog):
    client = llm()
    try:
        summary = await run_enrichment(catalog=catalog, llm=client)
    finally:
        await client.aclose()
    assert summary.results == []
    assert summary.as_dict()["enriched"] == 0


# ------------------------------------------------------------- taxonomy


def test_taxonomy_round_trips_through_yaml(tmp_path):
    taxonomy = Taxonomy(
        categories=[
            Category(path="ml/retrieval", label="Retrieval", description="Search and ranking"),
            Category(path="operations", label="Operations"),
        ]
    )
    target = taxonomy.save(tmp_path / "taxonomy.yaml")
    loaded = Taxonomy.load(target)

    assert loaded.paths == ["ml/retrieval", "operations"]
    assert loaded.get("ml/retrieval").description == "Search and ranking"
    assert loaded.get("missing") is None


def test_taxonomy_preserves_unicode_labels(tmp_path):
    taxonomy = Taxonomy(categories=[Category(path="fa/research", label="پژوهش")])
    loaded = Taxonomy.load(taxonomy.save(tmp_path / "t.yaml"))
    assert loaded.get("fa/research").label == "پژوهش"


def test_missing_taxonomy_loads_empty(tmp_path):
    taxonomy = Taxonomy.load(tmp_path / "absent.yaml")
    assert not taxonomy
    assert len(taxonomy) == 0


def test_malformed_taxonomy_raises(tmp_path):
    target = tmp_path / "bad.yaml"
    target.write_text("just a string", encoding="utf-8")
    with pytest.raises(TaxonomyError, match="not a mapping"):
        Taxonomy.load(target)


def test_taxonomy_skips_entries_without_a_path(tmp_path):
    target = tmp_path / "partial.yaml"
    target.write_text(
        "categories:\n  - label: No path\n  - path: ok\n    label: Fine\n", encoding="utf-8"
    )
    assert Taxonomy.load(target).paths == ["ok"]


def test_filter_known_drops_invented_categories():
    taxonomy = Taxonomy(categories=[Category(path="a", label="A"), Category(path="b", label="B")])
    assert taxonomy.filter_known(["a", "invented", "B", "a"]) == ["a", "b"]


def test_children_of():
    taxonomy = Taxonomy(
        categories=[
            Category(path="ml", label="ML"),
            Category(path="ml/retrieval", label="Retrieval"),
            Category(path="ops", label="Ops"),
        ]
    )
    assert [c.path for c in taxonomy.children_of("ml")] == ["ml/retrieval"]


def test_classification_schema_constrains_to_taxonomy_paths():
    taxonomy = Taxonomy(categories=[Category(path="ml/retrieval", label="R")])
    schema = classification_schema(taxonomy)
    assert schema["properties"]["categories"]["items"]["enum"] == ["ml/retrieval"]
    assert schema["properties"]["categories"]["maxItems"] == 3


# ------------------------------------------------------------ bootstrap


@respx.mock
async def test_bootstrap_proposes_a_taxonomy(catalog):
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply(
            '{"categories": ['
            '{"path": "ml/retrieval", "label": "Retrieval", "description": "Search"},'
            '{"path": "Operations", "label": "Ops", "description": ""}]}'
        )
    )
    add_document(catalog, url="https://x/1", text="# A\n\nBody.", summary="About retrieval.")

    client = llm()
    try:
        taxonomy = await bootstrap_taxonomy(catalog=catalog, llm=client)
    finally:
        await client.aclose()

    # Paths are slugified, so "Operations" becomes "operations".
    assert taxonomy.paths == ["ml/retrieval", "operations"]


@respx.mock
async def test_bootstrap_deduplicates_and_caps(catalog):
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply(
            '{"categories": ['
            '{"path": "a", "label": "A"}, {"path": "a", "label": "A again"},'
            '{"path": "b", "label": "B"}, {"path": "c", "label": "C"}]}'
        )
    )
    add_document(catalog, url="https://x/1", text="# A\n\nBody.", summary="Something.")

    client = llm()
    try:
        taxonomy = await bootstrap_taxonomy(catalog=catalog, llm=client, max_categories=2)
    finally:
        await client.aclose()

    assert taxonomy.paths == ["a", "b"]


async def test_bootstrap_without_enriched_documents_is_refused(catalog):
    add_document(catalog, url="https://x/1", text="# A\n\nBody.")  # no summary
    client = llm()
    try:
        with pytest.raises(TaxonomyError, match="enrich"):
            await bootstrap_taxonomy(catalog=catalog, llm=client)
    finally:
        await client.aclose()


# ------------------------------------------------------------- classify


@respx.mock
async def test_classify_document_returns_known_paths(catalog):
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply('{"categories": ["ml/retrieval"]}')
    )
    taxonomy = Taxonomy(categories=[Category(path="ml/retrieval", label="Retrieval")])
    record = add_document(
        catalog, url="https://x/1", text="# A\n\nBody.", summary="About retrieval."
    )

    client = llm()
    try:
        assert await classify_document(record, taxonomy, llm=client) == ["ml/retrieval"]
    finally:
        await client.aclose()


@respx.mock
async def test_classify_drops_invented_categories(catalog):
    """The enum is pushed down, but a model can still ignore it; the result
    must be filtered rather than trusted."""
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply('{"categories": ["ml/retrieval", "made/up"]}')
    )
    taxonomy = Taxonomy(categories=[Category(path="ml/retrieval", label="Retrieval")])
    record = add_document(catalog, url="https://x/1", text="# A\n\nB.", summary="s")

    client = llm()
    try:
        assert await classify_document(record, taxonomy, llm=client) == ["ml/retrieval"]
    finally:
        await client.aclose()


async def test_classify_with_empty_taxonomy_is_refused(catalog):
    record = add_document(catalog, url="https://x/1", text="# A\n\nB.", summary="s")
    client = llm()
    try:
        with pytest.raises(TaxonomyError, match="empty"):
            await classify_document(record, Taxonomy(), llm=client)
    finally:
        await client.aclose()


@respx.mock
async def test_run_classification_marks_unmatched_as_uncategorised(catalog):
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=reply('{"categories": []}'))
    taxonomy = Taxonomy(categories=[Category(path="ml/retrieval", label="Retrieval")])
    add_document(catalog, url="https://x/1", text="# A\n\nB.", summary="Unrelated topic.")

    client = llm()
    try:
        summary = await run_classification(catalog=catalog, taxonomy=taxonomy, llm=client)
    finally:
        await client.aclose()

    assert summary.uncategorised == 1
    assert catalog.list_documents()[0].categories == ["uncategorised"]
    assert "fit no category" in summary.render()


@respx.mock
async def test_run_classification_skips_already_classified(catalog):
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply('{"categories": ["ml/retrieval"]}')
    )
    taxonomy = Taxonomy(categories=[Category(path="ml/retrieval", label="Retrieval")])
    add_document(catalog, url="https://x/1", text="# A\n\nB.", summary="s")

    client = llm()
    try:
        await run_classification(catalog=catalog, taxonomy=taxonomy, llm=client)
        second = await run_classification(catalog=catalog, taxonomy=taxonomy, llm=client)
    finally:
        await client.aclose()

    assert second.results == []
    assert route.call_count == 1


@respx.mock
async def test_classification_summary_counts_by_category(catalog):
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply('{"categories": ["ml/retrieval"]}')
    )
    taxonomy = Taxonomy(categories=[Category(path="ml/retrieval", label="Retrieval")])
    for index in range(3):
        add_document(
            catalog,
            url=f"https://x/{index}",
            text="# A\n\nB.",
            summary="s",
            source_path=f"/corpus/{index}.md",
        )

    client = llm()
    try:
        summary = await run_classification(catalog=catalog, taxonomy=taxonomy, llm=client)
    finally:
        await client.aclose()

    assert summary.counts_by_category() == {"ml/retrieval": 3}
    assert summary.as_dict()["classified"] == 3


def test_classification_skips_unenriched_documents(catalog):
    add_document(catalog, url="https://x/1", text="# A\n\nB.")  # no summary
    import asyncio

    taxonomy = Taxonomy(categories=[Category(path="a", label="A")])

    async def run():
        client = llm()
        try:
            return await run_classification(catalog=catalog, taxonomy=taxonomy, llm=client)
        finally:
            await client.aclose()

    assert asyncio.run(run()).results == []


# ------------------------------------------------------------------ cli


def test_cli_taxonomy_show_without_a_file(tmp_path, monkeypatch, capsys):
    from mcp_fetch_server.config import settings

    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    assert cli_main(["taxonomy", "show"]) == 1
    assert "bootstrap" in capsys.readouterr().out


def test_cli_taxonomy_path(tmp_path, monkeypatch, capsys):
    from mcp_fetch_server.config import settings

    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    assert cli_main(["taxonomy", "path"]) == 0
    assert "taxonomy.yaml" in capsys.readouterr().out


def test_cli_taxonomy_bootstrap_refuses_to_clobber(tmp_path, monkeypatch, capsys):
    from mcp_fetch_server.config import settings

    data_dir = tmp_path / "data"
    monkeypatch.setattr(settings, "corpus_data_dir", str(data_dir))
    Taxonomy(categories=[Category(path="a", label="A")]).save()

    assert cli_main(["taxonomy", "bootstrap"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_classify_without_a_taxonomy(tmp_path, monkeypatch, capsys):
    from mcp_fetch_server.config import settings

    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    assert cli_main(["classify"]) == 1
    assert "taxonomy bootstrap" in capsys.readouterr().err
