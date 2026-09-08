"""Corpus taxonomy: propose it once, then classify against it.

Asking a model to categorise each document on its own produces a mess.
"Retrieval", "Information Retrieval", "retrieval-systems" and "IR" all appear,
none of them group anything, and filtering by category becomes useless. So
categorisation happens in two stages:

1. **Bootstrap** (once): the model reads a sample of document titles and
   summaries and proposes a small set of categories, written to an editable
   YAML file.
2. **Classify** (per document): the model must choose from that fixed set.
   The choices are pushed down as a JSON schema enum, and anything outside the
   taxonomy is dropped rather than silently accepted.

The taxonomy file is meant to be edited by hand. Growing it is a deliberate
act, not a side effect of ingesting one unusual document.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord
from mcp_fetch_server.rag.llm import LLMError, LocalLLM, Message, get_llm

logger = logging.getLogger(__name__)

UNCATEGORISED = "uncategorised"

TAXONOMY_FILENAME = "taxonomy.yaml"

_PATH_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*(?:/[a-z0-9]+(?:[-_][a-z0-9]+)*){0,2}$")

BOOTSTRAP_SYSTEM = (
    "You design document taxonomies. You produce a small, non-overlapping set "
    "of categories that covers the documents you are shown. You prefer broad, "
    "durable categories over narrow ones, and you never create two categories "
    "that mean the same thing. Reply with JSON only."
)

CLASSIFY_SYSTEM = (
    "You assign documents to categories from a fixed taxonomy. You may only "
    "use the category paths given to you. If none of them fit, return an "
    "empty list rather than inventing a category. Reply with JSON only."
)


class TaxonomyError(Exception):
    """Raised when a taxonomy cannot be read, written or proposed."""


@dataclass(slots=True)
class Category:
    path: str
    label: str
    description: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "label": self.label, "description": self.description}


@dataclass(slots=True)
class Taxonomy:
    categories: list[Category] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def paths(self) -> list[str]:
        return [category.path for category in self.categories]

    def __len__(self) -> int:
        return len(self.categories)

    def __bool__(self) -> bool:
        return bool(self.categories)

    def get(self, path: str) -> Category | None:
        for category in self.categories:
            if category.path == path:
                return category
        return None

    def filter_known(self, paths: Sequence[str]) -> list[str]:
        """Keep only paths that exist in this taxonomy, preserving order."""
        known = set(self.paths)
        seen: set[str] = set()
        kept: list[str] = []
        for path in paths:
            cleaned = str(path).strip().lower()
            if cleaned in known and cleaned not in seen:
                seen.add(cleaned)
                kept.append(cleaned)
        return kept

    def children_of(self, prefix: str) -> list[Category]:
        return [
            category
            for category in self.categories
            if category.path.startswith(f"{prefix}/") and category.path != prefix
        ]

    def as_prompt_block(self) -> str:
        lines = []
        for category in self.categories:
            suffix = f" - {category.description}" if category.description else ""
            lines.append(f"{category.path}: {category.label}{suffix}")
        return "\n".join(lines)

    # -- persistence ------------------------------------------------------

    @staticmethod
    def default_path(data_dir: Path | str | None = None) -> Path:
        directory = Path(data_dir) if data_dir is not None else settings.corpus_dir
        return directory / TAXONOMY_FILENAME

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path is not None else self.default_path()
        self.updated_at = time.time()
        payload = {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "categories": [category.as_dict() for category in self.categories],
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            raise TaxonomyError(f"Could not write taxonomy to {target}: {exc}") from exc
        return target

    @classmethod
    def load(cls, path: Path | str | None = None) -> Taxonomy:
        target = Path(path) if path is not None else cls.default_path()
        if not target.exists():
            return cls(categories=[])
        try:
            payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise TaxonomyError(f"Could not read taxonomy at {target}: {exc}") from exc

        if not isinstance(payload, dict):
            raise TaxonomyError(f"Taxonomy at {target} is not a mapping")

        categories: list[Category] = []
        for entry in payload.get("categories") or []:
            if not isinstance(entry, dict):
                continue
            path_value = str(entry.get("path", "")).strip().lower()
            if not path_value:
                continue
            categories.append(
                Category(
                    path=path_value,
                    label=str(entry.get("label") or path_value),
                    description=str(entry.get("description") or ""),
                )
            )
        return cls(
            categories=categories,
            created_at=float(payload.get("created_at") or time.time()),
            updated_at=float(payload.get("updated_at") or time.time()),
        )


# ------------------------------------------------------------- bootstrap


class ProposedCategory(BaseModel):
    path: str = Field(description="Lower-case slug path, one or two levels, e.g. ml/retrieval")
    label: str = Field(description="Short human-readable name")
    description: str = Field(default="", description="One sentence on what belongs here")

    @field_validator("path")
    @classmethod
    def _slug_path(cls, value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9/_-]+", "-", str(value).strip().lower())
        cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-/")
        if not _PATH_RE.match(cleaned):
            raise ValueError(f"invalid category path: {value!r}")
        return cleaned


class ProposedTaxonomy(BaseModel):
    categories: list[ProposedCategory]


def _sample_documents(records: list[DocumentRecord], sample_size: int) -> list[DocumentRecord]:
    """Spread the sample across the corpus rather than taking the first N."""
    enriched = [record for record in records if record.summary]
    if len(enriched) <= sample_size:
        return enriched
    step = len(enriched) / sample_size
    return [enriched[int(index * step)] for index in range(sample_size)]


def _describe(record: DocumentRecord) -> str:
    parts = [f"- {record.title or record.doc_id}"]
    if record.summary:
        parts.append(f"  {record.summary[:400]}")
    if record.tags:
        parts.append(f"  tags: {', '.join(record.tags[:8])}")
    return "\n".join(parts)


async def bootstrap_taxonomy(
    *,
    catalog: Catalog,
    llm: LocalLLM | None = None,
    sample_size: int = 60,
    max_categories: int = 20,
) -> Taxonomy:
    """Propose a taxonomy from the enriched documents already in the catalog."""
    client = llm or get_llm()
    records = list(catalog.iter_documents())
    sample = _sample_documents(records, sample_size)

    if not sample:
        raise TaxonomyError(
            "No enriched documents to learn from. Run `mcp-fetch-server enrich` first."
        )

    listing = "\n".join(_describe(record) for record in sample)
    messages = [
        Message(role="system", content=BOOTSTRAP_SYSTEM),
        Message(
            role="user",
            content=(
                f"Here are {len(sample)} documents from a corpus of {len(records)}.\n\n"
                f"{listing}\n\n"
                f"Propose between 5 and {max_categories} categories that together cover "
                "this corpus. Use lower-case slug paths with at most two levels, such as "
                "'ml/retrieval' or 'operations'. Give each a short label and a one-sentence "
                "description. Do not create two categories that mean the same thing."
            ),
        ),
    ]

    proposal = await client.chat_json(
        messages, ProposedTaxonomy, temperature=0.2, max_tokens=1500
    )

    seen: set[str] = set()
    categories: list[Category] = []
    for item in proposal.categories:
        if item.path in seen:
            continue
        seen.add(item.path)
        categories.append(
            Category(
                path=item.path,
                label=item.label.strip() or item.path,
                description=item.description.strip(),
            )
        )
        if len(categories) >= max_categories:
            break

    if not categories:
        raise TaxonomyError("The model proposed no usable categories")

    return Taxonomy(categories=categories)


# -------------------------------------------------------------- classify


class Classification(BaseModel):
    categories: list[str] = Field(default_factory=list)


def classification_schema(taxonomy: Taxonomy) -> dict[str, Any]:
    """JSON schema constraining categories to this taxonomy's paths.

    Pydantic cannot express this statically because the taxonomy is loaded at
    runtime, so the enum is built here and pushed down to the model. Without
    it a small model invents plausible-looking category names.
    """
    return {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": taxonomy.paths},
                "maxItems": 3,
            }
        },
        "required": ["categories"],
    }


async def classify_document(
    record: DocumentRecord,
    taxonomy: Taxonomy,
    *,
    llm: LocalLLM | None = None,
) -> list[str]:
    """Assign a document to at most three categories from the taxonomy."""
    if not taxonomy:
        raise TaxonomyError("The taxonomy is empty; bootstrap it first")

    client = llm or get_llm()
    description = _describe(record)
    messages = [
        Message(role="system", content=CLASSIFY_SYSTEM),
        Message(
            role="user",
            content=(
                "Available categories:\n"
                f"{taxonomy.as_prompt_block()}\n\n"
                "Document:\n"
                f"{description}\n\n"
                "Return the one to three category paths that best fit this document. "
                "Use only paths from the list above."
            ),
        ),
    ]

    result = await client.chat_json(
        messages,
        Classification,
        temperature=0.0,
        max_tokens=200,
        json_schema=classification_schema(taxonomy),
    )
    return taxonomy.filter_known(result.categories)


@dataclass(slots=True)
class ClassifyResult:
    doc_id: str
    title: str | None
    categories: list[str]
    status: str  # "classified" | "uncategorised" | "failed"
    error: str | None = None


@dataclass(slots=True)
class ClassifySummary:
    results: list[ClassifyResult] = field(default_factory=list)
    duration: float = 0.0

    @property
    def classified(self) -> int:
        return sum(1 for result in self.results if result.status == "classified")

    @property
    def uncategorised(self) -> int:
        return sum(1 for result in self.results if result.status == "uncategorised")

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status == "failed")

    def counts_by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            for path in result.categories:
                counts[path] = counts.get(path, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    def as_dict(self) -> dict[str, Any]:
        return {
            "classified": self.classified,
            "uncategorised": self.uncategorised,
            "failed": self.failed,
            "duration_seconds": round(self.duration, 2),
            "by_category": self.counts_by_category(),
        }

    def render(self) -> str:
        lines = [
            f"Classified {self.classified}, uncategorised {self.uncategorised}, "
            f"failed {self.failed} in {self.duration:.1f}s"
        ]
        counts = self.counts_by_category()
        if counts:
            lines.append("")
            width = max(len(path) for path in counts)
            lines.extend(f"  {path.ljust(width)}  {count}" for path, count in counts.items())
        if self.uncategorised:
            lines.append("")
            lines.append(
                f"{self.uncategorised} document(s) fit no category. Review them and "
                "extend the taxonomy by hand if there is a real gap."
            )
        return "\n".join(lines)


async def run_classification(
    *,
    catalog: Catalog,
    taxonomy: Taxonomy,
    llm: LocalLLM | None = None,
    reclassify: bool = False,
    limit: int | None = None,
    on_result: Callable[[ClassifyResult], None] | None = None,
) -> ClassifySummary:
    """Classify every enriched document against the taxonomy."""
    import asyncio

    started = time.perf_counter()
    client = llm or get_llm()
    summary = ClassifySummary()

    pending = [
        record
        for record in catalog.iter_documents()
        if record.summary and (reclassify or not record.categories)
    ]
    if limit is not None:
        pending = pending[:limit]

    if not pending:
        summary.duration = time.perf_counter() - started
        return summary

    results_lock = asyncio.Lock()

    async def process(record: DocumentRecord) -> None:
        result = ClassifyResult(
            doc_id=record.doc_id, title=record.title, categories=[], status="failed"
        )
        try:
            categories = await classify_document(record, taxonomy, llm=client)
            record.categories = categories or [UNCATEGORISED]
            catalog.upsert_document(record)
            result.categories = record.categories
            result.status = "classified" if categories else "uncategorised"
        except LLMError as exc:
            result.error = str(exc)
        except Exception as exc:
            logger.exception("Unexpected failure classifying %s", record.doc_id)
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            async with results_lock:
                summary.results.append(result)
            if on_result is not None:
                on_result(result)

    await asyncio.gather(*(process(record) for record in pending))

    summary.results.sort(key=lambda item: item.doc_id)
    summary.duration = time.perf_counter() - started
    return summary
