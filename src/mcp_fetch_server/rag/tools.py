"""MCP tools and resources backed by the offline corpus.

Registered only when the ``rag`` extra is installed, so the base server keeps
working without Qdrant. Failures are reported as tool errors that say what to
run next: an agent that is told "the corpus is empty, run ingest" behaves far
better than one handed a stack trace.
"""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.catalog import Catalog
from mcp_fetch_server.rag.llm import LLMError, LLMUnavailableError
from mcp_fetch_server.rag.retrieve import Retriever
from mcp_fetch_server.rag.store import StoreError, VectorStore
from mcp_fetch_server.rag.taxonomy import Taxonomy

logger = logging.getLogger(__name__)


def _split_csv(value: str | None) -> list[str] | None:
    """Accept comma-separated filters, which models produce more reliably
    than JSON arrays in a tool argument."""
    if not value or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def register_rag_tools(mcp: FastMCP) -> None:
    read_only = ToolAnnotations(readOnlyHint=True)

    @mcp.tool(
        annotations=read_only,
        description=(
            "Search the local offline document corpus and return the most relevant "
            "passages, each with a source URL you can fetch for the full document. "
            "Combines semantic and exact-term matching, so it handles both topical "
            "questions and specific names, codes or identifiers. Optionally filter "
            "by category, language (ISO code such as en or fa) or document type."
        ),
    )
    async def rag_search(
        query: str,
        top_k: int = settings.rag_top_k,
        categories: str = "",
        languages: str = "",
        doctypes: str = "",
        expand_query: bool = False,
        context_window: int = 0,
    ) -> str:
        """Search the local corpus for passages relevant to a query."""
        retriever = Retriever()
        try:
            result = await retriever.search(
                query,
                top_k=max(1, min(top_k, 30)),
                categories=_split_csv(categories),
                languages=_split_csv(languages),
                doctypes=_split_csv(doctypes),
                expand=expand_query,
                context_window=max(0, min(context_window, 3)),
            )
        except LLMUnavailableError as exc:
            raise ToolError(
                f"The local embedding model is unreachable: {exc}"
            ) from exc
        except LLMError as exc:
            raise ToolError(f"Embedding the query failed: {exc}") from exc
        except StoreError as exc:
            raise ToolError(
                f"The corpus index is unavailable: {exc}. "
                "It may not have been built yet: run `mcp-fetch-server embed`."
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected rag_search failure")
            raise ToolError(f"Corpus search failed: {exc}") from exc
        finally:
            retriever.close()

        return result.render()

    @mcp.tool(
        annotations=read_only,
        description=(
            "Answer a question from the local document corpus, using only what the "
            "corpus contains. Returns a written answer with numbered citations and "
            "the source URL behind each one, or says plainly that the corpus does "
            "not cover the question. Prefer rag_search when you want to read the "
            "passages yourself."
        ),
    )
    async def rag_answer(
        question: str,
        top_k: int = 6,
        expand_query: bool = True,
    ) -> str:
        """Answer a question from the corpus, with citations."""
        from mcp_fetch_server.rag.answer import answer_question

        try:
            answer = await answer_question(
                question,
                top_k=max(1, min(top_k, 20)),
                expand=expand_query,
            )
        except LLMUnavailableError as exc:
            raise ToolError(f"The local model is unreachable: {exc}") from exc
        except LLMError as exc:
            raise ToolError(str(exc)) from exc
        except StoreError as exc:
            raise ToolError(
                f"The corpus index is unavailable: {exc}. "
                "It may not have been built yet: run `mcp-fetch-server embed`."
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected rag_answer failure")
            raise ToolError(f"Answering from the corpus failed: {exc}") from exc

        return answer.render()

    @mcp.tool(
        annotations=read_only,
        description=(
            "Describe the local document corpus: how many documents and passages it "
            "holds, which languages and formats they are in, and which categories "
            "exist. Use this to find out what the corpus can answer before searching."
        ),
    )
    async def corpus_stats() -> str:
        """Report the size and shape of the local corpus."""
        try:
            with Catalog() as catalog:
                stats = catalog.stats()
                categories: dict[str, int] = {}
                for record in catalog.iter_documents():
                    for path in record.categories:
                        categories[path] = categories.get(path, 0) + 1
        except Exception as exc:
            raise ToolError(f"Could not read the corpus catalog: {exc}") from exc

        try:
            with VectorStore() as store:
                index = store.stats()
        except StoreError as exc:
            index = {"exists": False, "error": str(exc)}

        taxonomy = Taxonomy.load()

        if not stats["documents"]:
            return (
                "The local corpus is empty. Add documents with "
                "`mcp-fetch-server ingest <path>`."
            )

        lines = [
            f"Documents: {stats['documents']}",
            f"Passages:  {stats['chunks']}",
            f"Characters: {stats['characters']:,}",
            f"Formats:   {', '.join(f'{k} ({v})' for k, v in stats['by_doctype'].items())}",
            f"Languages: {', '.join(f'{k} ({v})' for k, v in stats['by_language'].items())}",
        ]
        if categories:
            ranked = sorted(categories.items(), key=lambda item: -item[1])
            lines.append(
                "Categories: " + ", ".join(f"{path} ({count})" for path, count in ranked)
            )
        elif taxonomy:
            lines.append(
                f"Categories: {len(taxonomy)} defined, none assigned yet "
                "(run `mcp-fetch-server classify`)"
            )

        if index.get("exists"):
            lines.append(f"Search index: {index['points']} vectors")
        else:
            lines.append(
                "Search index: not built. rag_search will not work until "
                "`mcp-fetch-server embed` has run."
            )
        return "\n".join(lines)

    @mcp.resource(
        "corpus://stats",
        name="corpus-stats",
        title="Local corpus statistics",
        description="Size and composition of the offline document corpus.",
        mime_type="application/json",
    )
    def read_corpus_stats() -> str:
        try:
            with Catalog() as catalog:
                payload = catalog.stats()
        except Exception as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        try:
            with VectorStore() as store:
                payload["index"] = store.stats()
        except StoreError as exc:
            payload["index"] = {"exists": False, "error": str(exc)}
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @mcp.resource(
        "corpus://taxonomy",
        name="corpus-taxonomy",
        title="Corpus taxonomy",
        description="The categories documents in the local corpus are filed under.",
        mime_type="application/json",
    )
    def read_taxonomy() -> str:
        taxonomy = Taxonomy.load()
        return json.dumps(
            {
                "categories": [category.as_dict() for category in taxonomy.categories],
                "count": len(taxonomy),
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.resource(
        "corpus://doc/{doc_id}",
        name="corpus-document",
        title="Corpus document",
        description=(
            "Full canonical text of one document in the local corpus, by document id. "
            "Document ids appear in corpus://stats and in rag_search results."
        ),
        mime_type="text/markdown",
    )
    def read_corpus_document(doc_id: str) -> str:
        try:
            with Catalog() as catalog:
                record = catalog.get_document(doc_id)
                if record is None:
                    return f"[Not found] No document with id {doc_id} in the local corpus."
                return catalog.read_blob(record.blob_path)
        except Exception as exc:
            return f"[Error] Could not read document {doc_id}: {exc}"
