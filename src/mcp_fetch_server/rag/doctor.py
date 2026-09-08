"""Preflight checks for the offline corpus stack.

Ingestion is long-running and mostly unattended, so it is worth failing fast
and legibly: a missing model or an unreachable Qdrant should be one line of
output, not a stack trace forty minutes into a run.

Exposed as ``mcp-fetch-server doctor``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.llm import LocalLLM

Status = Literal["ok", "warn", "fail"]

_SYMBOLS: dict[Status, str] = {"ok": "[ok]", "warn": "[warn]", "fail": "[FAIL]"}


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str
    hint: str | None = None


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str, hint: str | None = None) -> None:
        self.checks.append(Check(name=name, status=status, detail=detail, hint=hint))

    @property
    def failed(self) -> bool:
        return any(check.status == "fail" for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.failed,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "hint": check.hint,
                }
                for check in self.checks
            ],
        }

    def render(self) -> str:
        width = max((len(check.name) for check in self.checks), default=0)
        lines: list[str] = []
        for check in self.checks:
            lines.append(f"{_SYMBOLS[check.status]:<7} {check.name.ljust(width)}  {check.detail}")
            if check.hint and check.status != "ok":
                lines.append(f"{'':<7} {' ' * width}  -> {check.hint}")
        verdict = "Some checks failed." if self.failed else "All checks passed."
        return "\n".join([*lines, "", verdict])


async def check_llm(report: Report, *, llm: LocalLLM | None = None) -> None:
    client = llm or LocalLLM()
    owns_client = llm is None
    try:
        health = await client.health()
    except Exception as exc:  # defensive: doctor must never raise
        report.add("local model", "fail", f"{client.base_url}: {exc}")
        if owns_client:
            await client.aclose()
        return

    if not health["reachable"]:
        report.add(
            "local model",
            "fail",
            f"unreachable at {health['base_url']} ({health.get('error', 'no response')})",
            "Start it with `ollama serve`, or point FETCH_LLM_BASE_URL elsewhere.",
        )
        if owns_client:
            await client.aclose()
        return

    report.add(
        "local model",
        "ok",
        f"{health['backend']} at {health['base_url']} ({len(health['available_models'])} models)",
    )

    for role, key in (("chat", "chat_model"), ("embedding", "embed_model")):
        name = health[key]
        if health[f"{key}_available"]:
            report.add(f"{role} model", "ok", name)
        else:
            report.add(
                f"{role} model",
                "fail",
                f"{name} is not installed",
                f"Install it with `ollama pull {name}`.",
            )

    if health["embed_model_available"]:
        try:
            dimension = await client.embedding_dimension()
            report.add("embedding size", "ok", f"{dimension} dimensions")
        except Exception as exc:
            report.add("embedding size", "fail", f"probe failed: {exc}")

    if owns_client:
        await client.aclose()


async def check_qdrant(report: Report) -> None:
    """Probe Qdrant over plain HTTP so the optional client is not required."""
    url = settings.qdrant_url.strip().rstrip("/")

    if not url:
        # No server configured: Qdrant runs embedded on local disk, which is
        # a supported setup rather than a misconfiguration.
        report.add("qdrant", "ok", f"embedded, at {settings.corpus_dir / 'qdrant'}")
        return

    headers = {"api-key": settings.qdrant_api_key} if settings.qdrant_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
            response = await client.get(f"{url}/")
    except httpx.HTTPError as exc:
        report.add(
            "qdrant",
            "fail",
            f"unreachable at {url} ({exc})",
            "Start it with `docker compose up -d qdrant`, or set FETCH_QDRANT_URL "
            "empty to run Qdrant embedded on local disk instead.",
        )
        return

    if response.status_code >= 400:
        report.add("qdrant", "fail", f"{url} returned HTTP {response.status_code}")
        return

    version = ""
    try:
        version = str(response.json().get("version", ""))
    except ValueError:
        pass
    report.add("qdrant", "ok", f"{url}{f' (v{version})' if version else ''}")


def check_storage(report: Report) -> None:
    directory = settings.corpus_dir
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        report.add(
            "corpus storage",
            "fail",
            f"{directory} is not writable: {exc}",
            "Set FETCH_CORPUS_DATA_DIR to a writable directory.",
        )
        return
    report.add("corpus storage", "ok", str(directory.resolve()))


def check_rag_extra(report: Report) -> None:
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        report.add(
            "rag extra",
            "warn",
            "qdrant-client is not installed",
            "Install it with `uv sync --extra rag`.",
        )
        return
    report.add("rag extra", "ok", "installed")


def check_mode(report: Report) -> None:
    mode = settings.net_mode
    detail = f"net_mode={mode}, site={settings.site_base_url}"
    if mode == "online":
        report.add(
            "corpus mode",
            "warn",
            f"{detail} (the local corpus is not being served)",
            "Set FETCH_NET_MODE=hybrid or offline to serve ingested documents.",
        )
        return
    report.add("corpus mode", "ok", detail)


async def run_checks(*, llm: LocalLLM | None = None) -> Report:
    report = Report()
    check_mode(report)
    check_storage(report)
    check_rag_extra(report)
    await check_llm(report, llm=llm)
    await check_qdrant(report)
    return report


def main() -> int:
    """Entry point for ``mcp-fetch-server doctor``. Returns a shell exit code."""
    report = asyncio.run(run_checks())
    print(report.render())
    return 1 if report.failed else 0
