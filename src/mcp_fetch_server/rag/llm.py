"""Client for a locally hosted model (Ollama by default).

This is deliberately separate from MCP *sampling*: sampling borrows the
connected client's model, which is not available during offline work such as
ingestion. This module talks to a model the server itself owns, and is used
for four jobs:

* enrich     - title/summary/tags/entities extraction from an ingested document
* categorise - assign a document to a branch of the corpus taxonomy
* repair     - restructure badly extracted text (never rewrite it)
* expand     - turn a user query into better retrieval queries

Two backends are supported. ``ollama`` speaks Ollama's native API and is the
default; ``openai`` speaks the OpenAI-compatible surface exposed by llama.cpp,
vLLM, LM Studio and friends. Both are reached over plain HTTP, so no model
runtime is imported into this process and the packaged .exe stays small.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from mcp_fetch_server.config import settings

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

_RETRYABLE_ERRORS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class LLMError(Exception):
    """Raised when the local model cannot be reached or returns junk."""


class LLMUnavailableError(LLMError):
    """Raised when the model server is not running or not reachable."""


@dataclass(slots=True)
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


def _as_messages(messages: list[Message] | list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in messages:
        out.append(item.as_dict() if isinstance(item, Message) else dict(item))
    return out


def _strip_code_fence(text: str) -> str:
    """Remove a ```json ... ``` wrapper if the model added one anyway."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


class LocalLLM:
    """Async client for a local chat + embedding model server."""

    def __init__(
        self,
        *,
        backend: str | None = None,
        base_url: str | None = None,
        chat_model: str | None = None,
        embed_model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_concurrency: int | None = None,
        retries: int | None = None,
        keep_alive: str | None = None,
        num_ctx: int | None = None,
    ) -> None:
        self.backend = backend or settings.llm_backend
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.chat_model = chat_model or settings.llm_chat_model
        self.embed_model = embed_model or settings.llm_embed_model
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.timeout = timeout if timeout is not None else settings.llm_timeout_seconds
        self.retries = retries if retries is not None else settings.llm_retries
        self.keep_alive = keep_alive if keep_alive is not None else settings.llm_keep_alive
        self.num_ctx = num_ctx if num_ctx is not None else settings.llm_num_ctx

        limit = max_concurrency if max_concurrency is not None else settings.llm_max_concurrency
        self._semaphore = asyncio.Semaphore(max(1, limit))
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    # -- plumbing ---------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    headers = {"Accept": "application/json"}
                    if self.api_key:
                        headers["Authorization"] = f"Bearer {self.api_key}"
                    self._client = httpx.AsyncClient(
                        base_url=self.base_url,
                        timeout=httpx.Timeout(self.timeout),
                        headers=headers,
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        attempts = max(1, self.retries + 1)
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                async with self._semaphore:
                    response = await client.post(path, json=payload)
            except _RETRYABLE_ERRORS as exc:
                last_error = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise LLMUnavailableError(
                    f"Cannot reach the local model at {self.base_url}{path}: {exc}. "
                    "Is the model server running (e.g. `ollama serve`)?"
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Local model request failed: {exc}") from exc

            if response.status_code >= 400:
                detail = response.text.strip()[:400]
                # A cold model can answer 503 while it loads; that is worth retrying.
                if response.status_code in (429, 503) and attempt < attempts - 1:
                    last_error = LLMError(detail)
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise LLMError(f"Local model returned HTTP {response.status_code}: {detail}")

            try:
                return response.json()
            except ValueError as exc:
                raise LLMError("Local model did not return JSON") from exc

        raise LLMError(f"Local model request failed: {last_error}")

    # -- health -----------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Report reachability plus whether the configured models exist."""
        path = "/api/tags" if self.backend == "ollama" else "/v1/models"
        client = await self._get_client()
        try:
            response = await client.get(path)
        except httpx.HTTPError as exc:
            return {
                "reachable": False,
                "backend": self.backend,
                "base_url": self.base_url,
                "chat_model": self.chat_model,
                "chat_model_available": False,
                "embed_model": self.embed_model,
                "embed_model_available": False,
                "available_models": [],
                "error": str(exc),
            }

        available: list[str] = []
        if response.status_code < 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if self.backend == "ollama":
                available = [
                    str(entry.get("model") or entry.get("name"))
                    for entry in payload.get("models", [])
                ]
            else:
                available = [str(entry.get("id")) for entry in payload.get("data", [])]

        def _present(name: str) -> bool:
            base = name.split(":")[0]
            return any(entry == name or entry.split(":")[0] == base for entry in available)

        return {
            "reachable": response.status_code < 400,
            "backend": self.backend,
            "base_url": self.base_url,
            "chat_model": self.chat_model,
            "chat_model_available": _present(self.chat_model),
            "embed_model": self.embed_model,
            "embed_model_available": _present(self.embed_model),
            "available_models": available,
        }

    # -- chat -------------------------------------------------------------

    async def chat(
        self,
        messages: list[Message] | list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        model: str | None = None,
        json_schema: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> str:
        payload_messages = _as_messages(messages)
        target_model = model or self.chat_model

        if self.backend == "ollama":
            options: dict[str, Any] = {"temperature": temperature, "num_ctx": self.num_ctx}
            if max_tokens is not None:
                options["num_predict"] = max_tokens
            payload: dict[str, Any] = {
                "model": target_model,
                "messages": payload_messages,
                "stream": False,
                "options": options,
                "keep_alive": keep_alive if keep_alive is not None else self.keep_alive,
            }
            if json_schema is not None:
                payload["format"] = json_schema
            data = await self._post("/api/chat", payload)
            content = (data.get("message") or {}).get("content")
            if not isinstance(content, str):
                raise LLMError("Local model response had no message content")
            return content

        payload = {
            "model": target_model,
            "messages": payload_messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        data = await self._post("/v1/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("Local model returned no choices")
        content = (choices[0].get("message") or {}).get("content")
        if not isinstance(content, str):
            raise LLMError("Local model response had no message content")
        return content

    async def chat_json(
        self,
        messages: list[Message] | list[dict[str, str]],
        schema: type[ModelT],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        model: str | None = None,
        keep_alive: str | None = None,
    ) -> ModelT:
        """Chat with a constrained JSON schema, validated into ``schema``.

        A small model will occasionally emit JSON that parses but does not
        validate. One repair round-trip showing it the validation error is far
        cheaper than discarding the document, so we do exactly one.
        """
        json_schema = schema.model_json_schema()
        attempt_messages = _as_messages(messages)

        for attempt in range(2):
            raw = await self.chat(
                attempt_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
                json_schema=json_schema,
                keep_alive=keep_alive,
            )
            try:
                return schema.model_validate_json(_strip_code_fence(raw))
            except (ValidationError, ValueError) as exc:
                if attempt == 1:
                    raise LLMError(
                        f"Local model did not produce valid {schema.__name__} JSON: {exc}"
                    ) from exc
                logger.debug("Structured output failed validation, retrying once: %s", exc)
                attempt_messages = [
                    *attempt_messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "That response failed schema validation with:\n"
                            f"{exc}\n\nReturn corrected JSON only, matching the schema exactly."
                        ),
                    },
                ]

        raise LLMError("unreachable")

    # -- embeddings -------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        batch_size: int | None = None,
        keep_alive: str | None = None,
    ) -> list[list[float]]:
        """Embed texts, preserving input order. Empty input returns []."""
        if not texts:
            return []

        target_model = model or self.embed_model
        size = max(1, batch_size or settings.llm_embed_batch_size)

        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            batch = texts[start : start + size]
            vectors.extend(await self._embed_batch(batch, target_model, keep_alive))

        if len(vectors) != len(texts):
            raise LLMError(
                f"Embedding count mismatch: asked for {len(texts)}, got {len(vectors)}"
            )
        return vectors

    async def _embed_batch(
        self, batch: list[str], model: str, keep_alive: str | None
    ) -> list[list[float]]:
        if self.backend == "ollama":
            payload = {
                "model": model,
                "input": batch,
                "truncate": True,
                "keep_alive": keep_alive if keep_alive is not None else self.keep_alive,
            }
            data = await self._post("/api/embed", payload)
            embeddings = data.get("embeddings")
            if not isinstance(embeddings, list):
                raise LLMError("Ollama embed response had no 'embeddings' array")
            return [[float(value) for value in vector] for vector in embeddings]

        data = await self._post("/v1/embeddings", {"model": model, "input": batch})
        items = data.get("data")
        if not isinstance(items, list):
            raise LLMError("Embeddings response had no 'data' array")
        # OpenAI guarantees an index field; sort by it rather than trusting order.
        ordered = sorted(items, key=lambda item: int(item.get("index", 0)))
        return [[float(value) for value in item["embedding"]] for item in ordered]

    async def embedding_dimension(self, *, model: str | None = None) -> int:
        """Probe the embedding size, so collections are created to match."""
        vectors = await self.embed(["dimension probe"], model=model)
        if not vectors or not vectors[0]:
            raise LLMError("Could not determine embedding dimension")
        return len(vectors[0])

    async def unload(self, model: str) -> None:
        """Ask Ollama to evict a model from VRAM (keep_alive=0).

        Ingestion runs an embedding pass and an enrichment pass back to back;
        on an 8 GB card they do not comfortably co-reside, so the pipeline
        evicts one before loading the other instead of letting the runtime
        thrash.
        """
        if self.backend != "ollama":
            return
        try:
            await self._post("/api/chat", {"model": model, "messages": [], "keep_alive": 0})
        except LLMError as exc:
            logger.debug("Unload of %s failed (harmless): %s", model, exc)


_llm: LocalLLM | None = None


def get_llm() -> LocalLLM:
    """Return the process-wide LocalLLM, created on first use."""
    global _llm
    if _llm is None:
        _llm = LocalLLM()
    return _llm


async def reset_llm() -> None:
    """Drop the shared client (used by tests and config reloads)."""
    global _llm
    if _llm is not None:
        await _llm.aclose()
    _llm = None
