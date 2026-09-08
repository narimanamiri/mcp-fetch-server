"""Answering a question from the corpus, with citations.

The retriever returns passages; this turns them into an answer. The rules that
matter are all about not exceeding the evidence:

* the model is given only the retrieved passages and told to use nothing else;
* every claim must carry a ``[n]`` marker naming the passage it came from;
* markers pointing at passages that were not retrieved are stripped, because a
  citation that does not resolve is worse than none — it looks checked;
* if the passages do not answer the question, saying so is the correct answer.

The sources block is appended by this module rather than by the model, so the
URLs are always the real ones from the catalog.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from mcp_fetch_server.rag.llm import LLMError, LocalLLM, Message, get_llm
from mcp_fetch_server.rag.retrieve import RetrievalResult, Retriever
from mcp_fetch_server.rag.store import SearchHit

logger = logging.getLogger(__name__)

# Models group citations as [1, 3] or [1,3] at least as often as they write
# [1][3]. Matching only the single form silently reports a correctly grounded
# answer as uncited.
_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

SYSTEM = (
    "You answer questions using only the numbered passages you are given. "
    "You never use outside knowledge, and you never guess. Every factual "
    "sentence ends with the passage number it came from, like [1] or [2][3]. "
    "If the passages do not contain the answer, you say so plainly and state "
    "what is missing. You answer in the language of the question."
)


@dataclass(slots=True)
class Answer:
    question: str
    text: str
    hits: list[SearchHit] = field(default_factory=list)
    cited: list[int] = field(default_factory=list)
    grounded: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.text,
            "grounded": self.grounded,
            "sources": [
                {
                    "n": index,
                    "url": hit.citation_url,
                    "title": hit.title,
                    "cited": index in self.cited,
                }
                for index, hit in enumerate(self.hits, start=1)
            ],
        }

    def render(self) -> str:
        if not self.hits:
            return self.text
        lines = [self.text, "", "Sources:"]
        for index, hit in enumerate(self.hits, start=1):
            marker = "*" if index in self.cited else " "
            heading = f" — {hit.heading_trail}" if hit.heading_trail else ""
            lines.append(f" {marker} [{index}] {hit.title or hit.doc_id}{heading}")
            lines.append(f"       {hit.citation_url}")
        if not self.cited:
            lines.append("")
            lines.append(
                "Note: the answer cited no passage, so it may not be grounded in "
                "these sources."
            )
        return "\n".join(lines)


def build_context(hits: list[SearchHit]) -> str:
    blocks = []
    for index, hit in enumerate(hits, start=1):
        header = f"[{index}] {hit.title or hit.doc_id}"
        if hit.heading_trail:
            header += f" > {hit.heading_trail}"
        blocks.append(f"{header}\n{hit.text}")
    return "\n\n---\n\n".join(blocks)


def strip_unresolvable_citations(text: str, count: int) -> tuple[str, list[int]]:
    """Remove citation markers that point at passages that do not exist.

    A small model will occasionally cite [7] when it was given four passages.
    Leaving that in produces an answer that *looks* checked and is not.
    """
    cited: list[int] = []

    def replace(match: re.Match[str]) -> str:
        numbers = [int(part) for part in match.group(1).split(",")]
        kept = [number for number in numbers if 1 <= number <= count]
        for number in kept:
            if number not in cited:
                cited.append(number)
        if not kept:
            return ""
        return "[" + ", ".join(str(number) for number in kept) + "]"

    cleaned = _CITATION_RE.sub(replace, text)
    # Tidy up the spacing left behind by a removed marker.
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
    return cleaned.strip(), sorted(cited)


async def answer_question(
    question: str,
    *,
    retriever: Retriever | None = None,
    llm: LocalLLM | None = None,
    top_k: int = 6,
    expand: bool = True,
    context_window: int = 1,
    result: RetrievalResult | None = None,
) -> Answer:
    """Retrieve passages and answer from them alone."""
    cleaned = question.strip()
    if not cleaned:
        return Answer(question=question, text="Ask a question.", grounded=False)

    active_retriever = retriever or Retriever()
    owns_retriever = retriever is None
    client = llm or get_llm()

    try:
        if result is None:
            result = await active_retriever.search(
                cleaned,
                top_k=top_k,
                expand=expand,
                context_window=context_window,
            )
    finally:
        if owns_retriever:
            active_retriever.close()

    if not result.hits:
        return Answer(
            question=cleaned,
            text=(
                "The local corpus has no passage matching this question, so it "
                "cannot be answered from the archive."
            ),
            grounded=False,
        )

    try:
        raw = await client.chat(
            [
                Message(role="system", content=SYSTEM),
                Message(
                    role="user",
                    content=(
                        f"Passages:\n\n{build_context(result.hits)}\n\n"
                        f"Question: {cleaned}\n\n"
                        "Answer using only these passages, citing each claim."
                    ),
                ),
            ],
            temperature=0.1,
            max_tokens=700,
        )
    except LLMError as exc:
        raise LLMError(f"Could not generate an answer: {exc}") from exc

    text, cited = strip_unresolvable_citations(raw.strip(), len(result.hits))
    return Answer(
        question=cleaned,
        text=text,
        hits=result.hits,
        cited=cited,
        grounded=bool(cited),
    )
