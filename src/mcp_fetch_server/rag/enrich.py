"""Enrichment: what the local model adds to a document after ingestion.

One call per document produces a title, a summary, tags, entities, a
publication date and a set of hypothetical questions. The questions matter
more than they look: a user's phrasing usually resembles a question far more
than it resembles the document's prose, so indexing questions alongside the
text is one of the cheapest recall improvements available.

Categorisation is deliberately *not* done here. Free-form labelling of each
document in isolation produces hundreds of near-duplicate categories, so it
happens in a second pass against a fixed taxonomy; see ``rag.taxonomy``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator

from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord
from mcp_fetch_server.rag.llm import LLMError, LocalLLM, Message, get_llm

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_ISO_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

GENRES = (
    "paper",
    "manual",
    "specification",
    "report",
    "slides",
    "book",
    "article",
    "notes",
    "reference",
    "other",
)

SYSTEM_PROMPT = (
    "You extract structured metadata from documents. You describe only what "
    "the document actually contains. You never invent facts, dates, names or "
    "topics that are not present in the text. If a field is not supported by "
    "the document, leave it empty or null. Reply with JSON only."
)


class DocumentEnrichment(BaseModel):
    """Structured metadata the local model extracts from one document."""

    title: str = Field(description="The document's actual title, in its own language")
    summary: str = Field(description="Three to five sentences describing what the document covers")
    genre: str = Field(default="other", description=f"One of: {', '.join(GENRES)}")
    language: str = Field(default="", description="ISO 639-1 code, e.g. en, fa, ar")
    published_at: str | None = Field(
        default=None, description="Publication date as YYYY-MM-DD, or null if not stated"
    )
    tags: list[str] = Field(default_factory=list, description="Three to eight topical keywords")
    entities: list[str] = Field(
        default_factory=list,
        description="Named entities: people, organisations, products, standards",
    )
    questions: list[str] = Field(
        default_factory=list,
        description="Three to six questions this document answers, in the document's language",
    )

    @field_validator("genre")
    @classmethod
    def _known_genre(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        return cleaned if cleaned in GENRES else "other"

    @field_validator("language")
    @classmethod
    def _short_language(cls, value: str) -> str:
        return (value or "").strip().lower()[:5]

    @field_validator("published_at")
    @classmethod
    def _iso_date_or_none(cls, value: str | None) -> str | None:
        """Drop anything that is not an ISO date.

        A small model will happily answer "recent" or "2024?" here, and a
        malformed date is worse than no date because it silently breaks any
        later filter that compares on it.
        """
        if not value:
            return None
        cleaned = value.strip()
        if not _ISO_DATE_RE.match(cleaned):
            return None
        if len(cleaned) == 4:
            return f"{cleaned}-01-01"
        if len(cleaned) == 7:
            return f"{cleaned}-01"
        return cleaned

    @field_validator("tags", "entities", "questions")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            cleaned.append(text[:200])
        return cleaned[:12]


def enrichment_schema() -> dict[str, Any]:
    """JSON schema pushed down to the model, hand-built rather than derived.

    ``model_json_schema()`` leaves every field with a default out of
    ``required``, and a small model then simply omits them: genre, language,
    entities and published_at all came back missing, and Pydantic's defaults
    quietly filled in "other" and empty lists. So every field is required
    here, and genre is an enum rather than a sentence describing one. For a
    4B model, what is not in the schema does not happen.
    """
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "genre": {"type": "string", "enum": list(GENRES)},
            "language": {"type": "string"},
            "published_at": {"type": ["string", "null"]},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 8,
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
            "questions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 6,
            },
        },
        "required": [
            "title",
            "summary",
            "genre",
            "language",
            "published_at",
            "tags",
            "entities",
            "questions",
        ],
    }


def outline(text: str, *, max_headings: int = 60) -> list[str]:
    """Heading outline of a canonical document, as an indented list."""
    headings = []
    for match in _HEADING_RE.finditer(text):
        level = len(match.group(1))
        headings.append(f"{'  ' * (level - 1)}{match.group(2).strip()}")
        if len(headings) >= max_headings:
            break
    return headings


def build_digest(text: str, *, head_chars: int = 6000, tail_chars: int = 1500) -> str:
    """Condense a document into something that fits the model's context.

    Head, outline and tail together describe a long document better than the
    first N characters alone: the opening states the subject, the outline
    shows the scope, and the tail usually holds conclusions or references.
    """
    sections: list[str] = []

    headings = outline(text)
    if headings:
        sections.append("OUTLINE:\n" + "\n".join(headings))

    if len(text) <= head_chars + tail_chars:
        sections.append("DOCUMENT:\n" + text)
        return "\n\n".join(sections)

    sections.append("BEGINNING:\n" + text[:head_chars])
    sections.append("END:\n" + text[-tail_chars:])
    return "\n\n".join(sections)


def build_messages(record: DocumentRecord, text: str) -> list[Message]:
    digest = build_digest(text)
    facts = [
        f"Filename: {record.source_path or record.url}",
        f"Format: {record.doctype}",
    ]
    if record.page_count:
        facts.append(f"Pages: {record.page_count}")
    if record.language:
        facts.append(f"Detected script/language: {record.language}")

    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(
            role="user",
            content=(
                "Extract metadata for the following document.\n\n"
                "Write the title, summary, tags and questions in the same "
                "language as the document itself.\n\n"
                "Field guidance:\n"
                "- tags: topics the document is about, as general keywords.\n"
                "- entities: proper nouns actually named in the text, such as "
                "products, tools, organisations, people, standards or file "
                "formats. These are specific names, not topics, and they may "
                "repeat words used in tags.\n"
                "- questions: questions a reader would type that this document "
                "answers. Phrase them as real questions.\n"
                "- published_at: only if a date is stated in the text, "
                "otherwise null.\n\n"
                + "\n".join(facts)
                + "\n\n"
                + digest
            ),
        ),
    ]


async def enrich_document(
    record: DocumentRecord,
    text: str,
    *,
    llm: LocalLLM | None = None,
) -> DocumentEnrichment:
    """Ask the local model to describe one document."""
    client = llm or get_llm()
    return await client.chat_json(
        build_messages(record, text),
        DocumentEnrichment,
        temperature=0.0,
        max_tokens=900,
        json_schema=enrichment_schema(),
    )


def apply_enrichment(record: DocumentRecord, enrichment: DocumentEnrichment) -> DocumentRecord:
    """Fold model output into a record, keeping what the loader knows better."""
    if enrichment.title.strip():
        record.title = enrichment.title.strip()
    record.summary = enrichment.summary.strip() or None
    record.tags = enrichment.tags
    record.entities = enrichment.entities
    record.questions = enrichment.questions
    record.published_at = enrichment.published_at
    # The loader's script detection is mechanical and reliable; only take the
    # model's answer when the loader could not decide.
    if enrichment.language and record.language in (None, "", "und"):
        record.language = enrichment.language
    record.meta = {**record.meta, "genre": enrichment.genre}
    record.enriched_at = time.time()
    return record


@dataclass(slots=True)
class EnrichResult:
    doc_id: str
    title: str | None
    status: str  # "enriched" | "skipped" | "failed"
    error: str | None = None
    duration: float = 0.0


@dataclass(slots=True)
class EnrichSummary:
    results: list[EnrichResult] = field(default_factory=list)
    duration: float = 0.0

    @property
    def enriched(self) -> int:
        return sum(1 for result in self.results if result.status == "enriched")

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status == "failed")

    @property
    def failures(self) -> list[EnrichResult]:
        return [result for result in self.results if result.status == "failed"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "enriched": self.enriched,
            "skipped": self.skipped,
            "failed": self.failed,
            "duration_seconds": round(self.duration, 2),
            "documents": [
                {
                    "doc_id": result.doc_id,
                    "title": result.title,
                    "status": result.status,
                    "error": result.error,
                    "duration_seconds": round(result.duration, 2),
                }
                for result in self.results
            ],
        }

    def render(self) -> str:
        rate = ""
        if self.enriched:
            rate = f", {self.duration / self.enriched:.1f}s per document"
        lines = [
            f"Enriched {self.enriched}, skipped {self.skipped}, failed {self.failed} "
            f"in {self.duration:.1f}s{rate}"
        ]
        if self.failures:
            lines.append("")
            lines.append("Failures:")
            lines.extend(
                f"  {result.title or result.doc_id}: {result.error}" for result in self.failures
            )
        return "\n".join(lines)


async def run_enrichment(
    *,
    catalog: Catalog,
    llm: LocalLLM | None = None,
    reenrich: bool = False,
    limit: int | None = None,
    on_result: Callable[[EnrichResult], None] | None = None,
) -> EnrichSummary:
    """Enrich every document that does not have model-produced metadata yet."""
    started = time.perf_counter()
    client = llm or get_llm()
    summary = EnrichSummary()

    pending = [
        record
        for record in catalog.iter_documents()
        if reenrich or record.enriched_at is None
    ]
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        summary.duration = time.perf_counter() - started
        return summary

    results_lock = asyncio.Lock()

    async def process(record: DocumentRecord) -> None:
        document_started = time.perf_counter()
        result = EnrichResult(doc_id=record.doc_id, title=record.title, status="failed")
        try:
            text = catalog.read_blob(record.blob_path)
            enrichment = await enrich_document(record, text, llm=client)
            apply_enrichment(record, enrichment)
            catalog.upsert_document(record)
            result.status = "enriched"
            result.title = record.title
        except LLMError as exc:
            result.error = str(exc)
        except Exception as exc:
            logger.exception("Unexpected failure enriching %s", record.doc_id)
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.duration = time.perf_counter() - document_started
            async with results_lock:
                summary.results.append(result)
            if on_result is not None:
                on_result(result)

    # The LLM client already caps concurrency; gathering here just keeps the
    # queue full so the GPU is never idle waiting for the next request.
    await asyncio.gather(*(process(record) for record in pending))

    summary.results.sort(key=lambda item: item.doc_id)
    summary.duration = time.perf_counter() - started
    return summary
