"""Redacted server configuration for admin UI and MCP resources."""

from __future__ import annotations

from typing import Any

from mcp_fetch_server import __version__
from mcp_fetch_server.config import settings


def public_settings() -> dict[str, Any]:
    """Return a JSON-serializable snapshot of settings with secrets removed."""
    return {
        "version": __version__,
        "user_agent": settings.user_agent,
        "allowed_domains": sorted(settings.allowed_domain_set) or "any",
        "max_response_bytes": settings.max_response_bytes,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "max_redirects": settings.max_redirects,
        "default_max_length": settings.default_max_length,
        "max_history_entries": settings.max_history_entries,
        "max_cache_bytes": settings.max_cache_bytes,
        "max_batch_urls": settings.max_batch_urls,
        "max_batch_concurrency": settings.max_batch_concurrency,
        "search_max_results": settings.search_max_results,
        "search_timeout_seconds": settings.search_timeout_seconds,
        "searxng_url": settings.searxng_url or None,
        "searxng_timeout_seconds": settings.searxng_timeout_seconds,
        "local_files_root": settings.local_files_root or None,
        "max_file_read_bytes": settings.max_file_read_bytes,
        "max_file_write_bytes": settings.max_file_write_bytes,
        "admin_enabled": settings.admin_enabled,
        "admin_host": settings.admin_host,
        "admin_port": settings.admin_port,
        "auth_enabled": bool(settings.mcp_auth_token),
        "rate_limit_per_minute": settings.rate_limit_per_minute,
        "log_level": settings.log_level,
        # Offline corpus / RAG. Secrets (llm_api_key, qdrant_api_key) are
        # reported as booleans only.
        "net_mode": settings.net_mode,
        "site_base_url": settings.site_base_url,
        "corpus_data_dir": str(settings.corpus_dir),
        "llm_backend": settings.llm_backend,
        "llm_base_url": settings.llm_base_url,
        "llm_chat_model": settings.llm_chat_model,
        "llm_embed_model": settings.llm_embed_model,
        "llm_api_key_set": bool(settings.llm_api_key),
        "llm_max_concurrency": settings.llm_max_concurrency,
        "qdrant_url": settings.qdrant_url,
        "qdrant_collection": settings.qdrant_collection,
        "qdrant_api_key_set": bool(settings.qdrant_api_key),
        "chunk_target_tokens": settings.chunk_target_tokens,
        "chunk_overlap_ratio": settings.chunk_overlap_ratio,
        "rag_top_k": settings.rag_top_k,
        "rag_candidates": settings.rag_candidates,
        "rag_rerank_enabled": settings.rag_rerank_enabled,
    }
