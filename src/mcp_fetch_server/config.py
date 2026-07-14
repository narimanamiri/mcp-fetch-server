"""Application configuration loaded from environment variables."""

from __future__ import annotations

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

    @property
    def allowed_domain_set(self) -> set[str]:
        if not self.allowed_domains.strip():
            return set()
        return {
            domain.strip().lower()
            for domain in self.allowed_domains.split(",")
            if domain.strip()
        }


settings = Settings()
