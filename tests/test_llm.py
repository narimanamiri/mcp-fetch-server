"""Tests for the local model client (rag.llm)."""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import BaseModel, Field

from mcp_fetch_server.rag.llm import (
    LLMError,
    LLMUnavailableError,
    LocalLLM,
    Message,
    _strip_code_fence,
)

OLLAMA = "http://localhost:11434"
OPENAI = "http://localhost:8000"


def ollama_llm(**kwargs) -> LocalLLM:
    defaults = dict(
        backend="ollama",
        base_url=OLLAMA,
        chat_model="gemma3:4b",
        embed_model="bge-m3",
        retries=0,
        timeout=5.0,
    )
    defaults.update(kwargs)
    return LocalLLM(**defaults)


def openai_llm(**kwargs) -> LocalLLM:
    defaults = dict(
        backend="openai",
        base_url=OPENAI,
        chat_model="local-model",
        embed_model="local-embed",
        retries=0,
        timeout=5.0,
    )
    defaults.update(kwargs)
    return LocalLLM(**defaults)


# ---------------------------------------------------------------- chat


@respx.mock
async def test_chat_ollama_returns_message_content():
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"role": "assistant", "content": "hi"}})
    )
    llm = ollama_llm()
    try:
        assert await llm.chat([Message(role="user", content="hello")]) == "hi"
    finally:
        await llm.aclose()

    body = route.calls[0].request.read().decode()
    assert '"stream":false' in body.replace(" ", "")
    assert "gemma3:4b" in body


@respx.mock
async def test_chat_accepts_plain_dicts_and_sets_options():
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "ok"}})
    )
    llm = ollama_llm(num_ctx=4096)
    try:
        await llm.chat([{"role": "user", "content": "q"}], temperature=0.3, max_tokens=42)
    finally:
        await llm.aclose()

    payload = route.calls[0].request.read().decode()
    assert '"num_ctx": 4096' in payload or '"num_ctx":4096' in payload
    assert "42" in payload


@respx.mock
async def test_chat_openai_backend_reads_choices():
    respx.post(f"{OPENAI}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "answer"}}]}
        )
    )
    llm = openai_llm()
    try:
        assert await llm.chat([Message(role="user", content="q")]) == "answer"
    finally:
        await llm.aclose()


@respx.mock
async def test_chat_raises_on_http_error():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(500, text="boom"))
    llm = ollama_llm()
    try:
        with pytest.raises(LLMError, match="HTTP 500"):
            await llm.chat([Message(role="user", content="q")])
    finally:
        await llm.aclose()


@respx.mock
async def test_unreachable_server_raises_unavailable():
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    llm = ollama_llm()
    try:
        with pytest.raises(LLMUnavailableError, match="ollama serve"):
            await llm.chat([Message(role="user", content="q")])
    finally:
        await llm.aclose()


@respx.mock
async def test_retries_then_succeeds():
    respx.post(f"{OLLAMA}/api/chat").mock(
        side_effect=[
            httpx.Response(503, text="loading model"),
            httpx.Response(200, json={"message": {"content": "ready"}}),
        ]
    )
    llm = ollama_llm(retries=1)
    try:
        assert await llm.chat([Message(role="user", content="q")]) == "ready"
    finally:
        await llm.aclose()


# ------------------------------------------------------- structured output


class Enrichment(BaseModel):
    title: str
    tags: list[str] = Field(default_factory=list)


@respx.mock
async def test_chat_json_validates_into_model():
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={"message": {"content": '{"title": "Doc", "tags": ["a", "b"]}'}},
        )
    )
    llm = ollama_llm()
    try:
        result = await llm.chat_json([Message(role="user", content="x")], Enrichment)
    finally:
        await llm.aclose()

    assert result.title == "Doc"
    assert result.tags == ["a", "b"]
    # The JSON schema must be pushed down to the model, not just hoped for.
    assert "format" in route.calls[0].request.read().decode()


@respx.mock
async def test_chat_json_strips_code_fence():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={"message": {"content": '```json\n{"title": "Fenced", "tags": []}\n```'}},
        )
    )
    llm = ollama_llm()
    try:
        result = await llm.chat_json([Message(role="user", content="x")], Enrichment)
    finally:
        await llm.aclose()
    assert result.title == "Fenced"


@respx.mock
async def test_chat_json_repairs_once_then_succeeds():
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        side_effect=[
            httpx.Response(200, json={"message": {"content": '{"tags": []}'}}),
            httpx.Response(200, json={"message": {"content": '{"title": "Fixed", "tags": []}'}}),
        ]
    )
    llm = ollama_llm()
    try:
        result = await llm.chat_json([Message(role="user", content="x")], Enrichment)
    finally:
        await llm.aclose()

    assert result.title == "Fixed"
    assert route.call_count == 2
    # The repair turn must show the model what went wrong.
    assert "schema validation" in route.calls[1].request.read().decode()


@respx.mock
async def test_chat_json_gives_up_after_one_repair():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "{}"}})
    )
    llm = ollama_llm()
    try:
        with pytest.raises(LLMError, match="valid Enrichment JSON"):
            await llm.chat_json([Message(role="user", content="x")], Enrichment)
    finally:
        await llm.aclose()


# ----------------------------------------------------------- embeddings


@respx.mock
async def test_embed_ollama_returns_vectors():
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    )
    llm = ollama_llm()
    try:
        vectors = await llm.embed(["a", "b"])
    finally:
        await llm.aclose()
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


@respx.mock
async def test_embed_batches_and_preserves_order():
    respx.post(f"{OLLAMA}/api/embed").mock(
        side_effect=[
            httpx.Response(200, json={"embeddings": [[1.0], [2.0]]}),
            httpx.Response(200, json={"embeddings": [[3.0]]}),
        ]
    )
    llm = ollama_llm()
    try:
        vectors = await llm.embed(["a", "b", "c"], batch_size=2)
    finally:
        await llm.aclose()
    assert vectors == [[1.0], [2.0], [3.0]]


@respx.mock
async def test_embed_empty_input_makes_no_request():
    route = respx.post(f"{OLLAMA}/api/embed")
    llm = ollama_llm()
    try:
        assert await llm.embed([]) == []
    finally:
        await llm.aclose()
    assert route.call_count == 0


@respx.mock
async def test_embed_count_mismatch_raises():
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0]]})
    )
    llm = ollama_llm()
    try:
        with pytest.raises(LLMError, match="count mismatch"):
            await llm.embed(["a", "b"])
    finally:
        await llm.aclose()


@respx.mock
async def test_embed_openai_sorts_by_index():
    respx.post(f"{OPENAI}/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [9.0]},
                    {"index": 0, "embedding": [8.0]},
                ]
            },
        )
    )
    llm = openai_llm()
    try:
        assert await llm.embed(["a", "b"]) == [[8.0], [9.0]]
    finally:
        await llm.aclose()


@respx.mock
async def test_embedding_dimension_probe():
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.0] * 1024]})
    )
    llm = ollama_llm()
    try:
        assert await llm.embedding_dimension() == 1024
    finally:
        await llm.aclose()


# --------------------------------------------------------------- health


@respx.mock
async def test_health_reports_available_models():
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"model": "bge-m3:latest"}, {"model": "gemma3:4b"}]}
        )
    )
    llm = ollama_llm()
    try:
        health = await llm.health()
    finally:
        await llm.aclose()

    assert health["reachable"] is True
    assert health["chat_model_available"] is True
    assert health["embed_model_available"] is True


@respx.mock
async def test_health_flags_missing_model():
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"model": "llava:latest"}]})
    )
    llm = ollama_llm()
    try:
        health = await llm.health()
    finally:
        await llm.aclose()

    assert health["reachable"] is True
    assert health["chat_model_available"] is False
    assert health["embed_model_available"] is False


@respx.mock
async def test_health_when_server_down():
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    llm = ollama_llm()
    try:
        health = await llm.health()
    finally:
        await llm.aclose()

    assert health["reachable"] is False
    assert "error" in health


# ---------------------------------------------------------------- misc


def test_strip_code_fence_handles_plain_and_fenced():
    assert _strip_code_fence('{"a": 1}') == '{"a": 1}'
    assert _strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'


@respx.mock
async def test_unload_is_noop_for_openai_backend():
    route = respx.post(f"{OPENAI}/api/chat")
    llm = openai_llm()
    try:
        await llm.unload("anything")
    finally:
        await llm.aclose()
    assert route.call_count == 0


@respx.mock
async def test_unload_swallows_errors():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(500, text="nope"))
    llm = ollama_llm()
    try:
        await llm.unload("gemma3:4b")  # must not raise
    finally:
        await llm.aclose()
