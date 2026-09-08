"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Browser-like default; many sites (e.g. Wikipedia) block bot-style User-Agent strings.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    user_agent: str = Field(default=DEFAULT_USER_AGENT, alias="FETCH_USER_AGENT")
    allowed_domains: str = Field(default="", alias="FETCH_ALLOWED_DOMAINS")
    max_response_bytes: int = Field(default=5_242_880, alias="FETCH_MAX_RESPONSE_BYTES")
    request_timeout_seconds: float = Field(default=30.0, alias="FETCH_REQUEST_TIMEOUT_SECONDS")
    max_redirects: int = Field(default=5, alias="FETCH_MAX_REDIRECTS")
    request_retries: int = Field(default=3, alias="FETCH_REQUEST_RETRIES")
    retry_backoff_seconds: float = Field(default=0.5, alias="FETCH_RETRY_BACKOFF_SECONDS")
    default_max_length: int = Field(default=5000, alias="FETCH_DEFAULT_MAX_LENGTH")
    mcp_auth_token: str | None = Field(default=None, alias="MCP_AUTH_TOKEN")
    rate_limit_per_minute: int = Field(default=60, alias="MCP_RATE_LIMIT_PER_MINUTE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Fetch history / cache (backs the history:// and fetch-cache:// resources)
    max_history_entries: int = Field(default=50, alias="FETCH_MAX_HISTORY_ENTRIES")
    max_cache_bytes: int = Field(default=2_000_000, alias="FETCH_MAX_CACHE_BYTES")

    # batch_fetch tool
    max_batch_urls: int = Field(default=10, alias="FETCH_MAX_BATCH_URLS")
    max_batch_concurrency: int = Field(default=5, alias="FETCH_MAX_BATCH_CONCURRENCY")

    # web_search tool (DuckDuckGo HTML endpoint, no API key required)
    search_max_results: int = Field(default=5, alias="FETCH_SEARCH_MAX_RESULTS")
    search_timeout_seconds: float = Field(default=15.0, alias="FETCH_SEARCH_TIMEOUT_SECONDS")

    # Optional SearXNG fallback for web_search, used only if the DuckDuckGo
    # scrape fails (e.g. its HTML layout changes). Empty = fallback disabled.
    # See docker-compose.yml / searxng/settings.yml to run one locally.
    searxng_url: str = Field(default="http://localhost:8080", alias="FETCH_SEARXNG_URL")
    searxng_timeout_seconds: float = Field(default=10.0, alias="FETCH_SEARXNG_TIMEOUT_SECONDS")

    # Local file tools (read_file/write_file/list_dir). Empty = disabled.
    local_files_root: str = Field(default="", alias="FETCH_LOCAL_FILES_ROOT")
    max_file_read_bytes: int = Field(default=2_000_000, alias="FETCH_MAX_FILE_READ_BYTES")
    max_file_write_bytes: int = Field(default=2_000_000, alias="FETCH_MAX_FILE_WRITE_BYTES")

    # Management web GUI (dashboard at /admin). In stdio mode it runs as a
    # background HTTP server on admin_host:admin_port; in streamable-http mode
    # it is served on the same port under /admin.
    admin_enabled: bool = Field(default=True, alias="FETCH_ADMIN_ENABLED")
    admin_host: str = Field(default="127.0.0.1", alias="FETCH_ADMIN_HOST")
    admin_port: int = Field(default=8001, alias="FETCH_ADMIN_PORT")

    # ----------------------------------------------------------------------
    # Offline corpus / RAG
    # ----------------------------------------------------------------------
    # How fetch_url / web_search / extract_links resolve requests:
    #   online  - always the real network (current behaviour, default)
    #   offline - only the local corpus; the network is never touched
    #   hybrid  - local corpus first, real network as a fallback
    net_mode: Literal["online", "offline", "hybrid"] = Field(
        default="online", alias="FETCH_NET_MODE"
    )

    # Local model used server-side for enrichment, categorisation and answers.
    # This is separate from MCP sampling, which uses the *client's* model.
    llm_backend: Literal["ollama", "openai"] = Field(default="ollama", alias="FETCH_LLM_BACKEND")
    llm_base_url: str = Field(default="http://localhost:11434", alias="FETCH_LLM_BASE_URL")
    llm_chat_model: str = Field(default="gemma3:4b", alias="FETCH_LLM_CHAT_MODEL")
    llm_embed_model: str = Field(default="bge-m3", alias="FETCH_LLM_EMBED_MODEL")
    llm_api_key: str | None = Field(default=None, alias="FETCH_LLM_API_KEY")
    llm_timeout_seconds: float = Field(default=120.0, alias="FETCH_LLM_TIMEOUT_SECONDS")
    llm_max_concurrency: int = Field(default=2, alias="FETCH_LLM_MAX_CONCURRENCY")
    llm_retries: int = Field(default=2, alias="FETCH_LLM_RETRIES")
    # Ollama keep_alive: how long a model stays resident in VRAM after a call.
    llm_keep_alive: str = Field(default="5m", alias="FETCH_LLM_KEEP_ALIVE")
    llm_num_ctx: int = Field(default=8192, alias="FETCH_LLM_NUM_CTX")
    llm_embed_batch_size: int = Field(default=16, alias="FETCH_LLM_EMBED_BATCH_SIZE")

    # Qdrant vector store
    qdrant_url: str = Field(default="http://localhost:6333", alias="FETCH_QDRANT_URL")
    qdrant_api_key: str | None = Field(default=None, alias="FETCH_QDRANT_API_KEY")
    qdrant_collection: str = Field(default="corpus_chunks", alias="FETCH_QDRANT_COLLECTION")

    # Corpus storage (SQLite catalog, blob store, rendered page cache, taxonomy)
    corpus_data_dir: str = Field(default="./data", alias="FETCH_CORPUS_DATA_DIR")

    # Chunking
    chunk_target_tokens: int = Field(default=600, alias="FETCH_CHUNK_TARGET_TOKENS")
    chunk_overlap_ratio: float = Field(default=0.15, alias="FETCH_CHUNK_OVERLAP_RATIO")
    # Headings at or above this level start a new chunk. Deeper subsections
    # pack together until the token budget is reached, so a document with many
    # short "###" sections does not turn into a pile of 30-token chunks.
    chunk_section_break_level: int = Field(default=2, alias="FETCH_CHUNK_SECTION_BREAK_LEVEL")

    # Retrieval
    rag_top_k: int = Field(default=8, alias="FETCH_RAG_TOP_K")
    rag_candidates: int = Field(default=50, alias="FETCH_RAG_CANDIDATES")
    # Off by default, on evidence rather than principle. Measured on the
    # development corpus, cross-encoder reranking *lowered* recall@5 from
    # 100% to 92% and nDCG from 0.818 to 0.779 while raising median query
    # latency from 37 ms to 1.5 s. Whether it helps depends on the corpus, so
    # measure with `mcp-fetch-server eval` before turning it on.
    rag_rerank_enabled: bool = Field(default=False, alias="FETCH_RAG_RERANK_ENABLED")
    # Multilingual by design: an English-only reranker would push every
    # Persian passage down the list. Runs on CPU via ONNX, keeping the GPU
    # free for the embedding and chat models.
    rag_reranker_model: str = Field(
        default="jinaai/jina-reranker-v2-base-multilingual",
        alias="FETCH_RAG_RERANKER_MODEL",
    )

    # Base URL minted for locally ingested documents, and the host that the
    # offline resolver answers for. Never DNS-resolved.
    site_base_url: str = Field(default="https://local.archive", alias="FETCH_SITE_BASE_URL")

    @property
    def allowed_domain_set(self) -> set[str]:
        if not self.allowed_domains.strip():
            return set()
        return {
            domain.strip().lower()
            for domain in self.allowed_domains.split(",")
            if domain.strip()
        }

    @property
    def site_host(self) -> str:
        """Hostname portion of site_base_url (e.g. 'local.archive')."""
        return (urlparse(self.site_base_url).hostname or "").lower()

    @property
    def corpus_dir(self) -> Path:
        """Root directory for the SQLite catalog, blob store and page cache."""
        return Path(self.corpus_data_dir).expanduser()

    @property
    def offline_enabled(self) -> bool:
        return self.net_mode in ("offline", "hybrid")


settings = Settings()
