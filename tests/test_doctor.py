"""Tests for the offline-stack preflight checks (rag.doctor)."""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_fetch_server.__main__ import main as cli_main
from mcp_fetch_server.config import settings
from mcp_fetch_server.rag import doctor
from mcp_fetch_server.rag.llm import LocalLLM

OLLAMA = "http://localhost:11434"
QDRANT = "http://localhost:6333"


def _llm() -> LocalLLM:
    return LocalLLM(
        backend="ollama",
        base_url=OLLAMA,
        chat_model="gemma3:4b",
        embed_model="bge-m3",
        retries=0,
        timeout=5.0,
    )


def _status(report: doctor.Report, name: str) -> str:
    for check in report.checks:
        if check.name == name:
            return check.status
    raise AssertionError(f"no check named {name!r} in {[c.name for c in report.checks]}")


# --------------------------------------------------------------- storage


def test_check_storage_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "corpus"))
    report = doctor.Report()
    doctor.check_storage(report)
    assert _status(report, "corpus storage") == "ok"
    assert (tmp_path / "corpus").is_dir()


def test_check_storage_reports_failure(tmp_path, monkeypatch):
    # A file where the directory should be makes mkdir fail on every platform.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(settings, "corpus_data_dir", str(blocker / "corpus"))
    report = doctor.Report()
    doctor.check_storage(report)
    assert _status(report, "corpus storage") == "fail"
    assert report.failed


# ------------------------------------------------------------------ mode


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("online", "warn"), ("hybrid", "ok"), ("offline", "ok")],
)
def test_check_mode(monkeypatch, mode, expected):
    monkeypatch.setattr(settings, "net_mode", mode)
    report = doctor.Report()
    doctor.check_mode(report)
    assert _status(report, "corpus mode") == expected
    # A warning must never fail the run; only a genuine breakage does.
    assert report.failed is False


# ------------------------------------------------------------------- llm


@respx.mock
async def test_check_llm_all_present():
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(
            200, json={"models": [{"model": "gemma3:4b"}, {"model": "bge-m3:latest"}]}
        )
    )
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.0] * 1024]})
    )
    report = doctor.Report()
    llm = _llm()
    try:
        await doctor.check_llm(report, llm=llm)
    finally:
        await llm.aclose()

    assert _status(report, "local model") == "ok"
    assert _status(report, "chat model") == "ok"
    assert _status(report, "embedding model") == "ok"
    assert _status(report, "embedding size") == "ok"
    assert report.failed is False


@respx.mock
async def test_check_llm_missing_chat_model():
    respx.get(f"{OLLAMA}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"model": "bge-m3:latest"}]})
    )
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.0] * 8]})
    )
    report = doctor.Report()
    llm = _llm()
    try:
        await doctor.check_llm(report, llm=llm)
    finally:
        await llm.aclose()

    assert _status(report, "chat model") == "fail"
    assert _status(report, "embedding model") == "ok"
    assert "ollama pull gemma3:4b" in report.render()


@respx.mock
async def test_check_llm_server_down():
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    report = doctor.Report()
    llm = _llm()
    try:
        await doctor.check_llm(report, llm=llm)
    finally:
        await llm.aclose()

    assert _status(report, "local model") == "fail"
    assert "ollama serve" in report.render()
    # Model checks are pointless once the server is unreachable.
    assert [c.name for c in report.checks] == ["local model"]


# ---------------------------------------------------------------- qdrant


@respx.mock
async def test_check_qdrant_ok(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", QDRANT)
    respx.get(f"{QDRANT}/").mock(return_value=httpx.Response(200, json={"version": "1.12.0"}))
    report = doctor.Report()
    await doctor.check_qdrant(report)
    assert _status(report, "qdrant") == "ok"
    assert "1.12.0" in report.render()


@respx.mock
async def test_check_qdrant_down(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", QDRANT)
    respx.get(f"{QDRANT}/").mock(side_effect=httpx.ConnectError("refused"))
    report = doctor.Report()
    await doctor.check_qdrant(report)
    assert _status(report, "qdrant") == "fail"
    assert "docker compose up -d qdrant" in report.render()


# ---------------------------------------------------------------- report


def test_report_render_shows_hints_only_for_problems():
    report = doctor.Report()
    report.add("fine", "ok", "all good", hint="never shown")
    report.add("broken", "fail", "bad", hint="do this")
    rendered = report.render()
    assert "never shown" not in rendered
    assert "do this" in rendered
    assert "Some checks failed." in rendered


def test_report_as_dict_round_trip():
    report = doctor.Report()
    report.add("a", "ok", "detail")
    payload = report.as_dict()
    assert payload["ok"] is True
    assert payload["checks"][0] == {
        "name": "a",
        "status": "ok",
        "detail": "detail",
        "hint": None,
    }


# ------------------------------------------------------------------- cli


def test_cli_doctor_returns_exit_code(monkeypatch, capsys):
    async def fake_run_checks(**_kwargs):
        report = doctor.Report()
        report.add("local model", "fail", "unreachable")
        return report

    monkeypatch.setattr(doctor, "run_checks", fake_run_checks)
    assert cli_main(["doctor"]) == 1
    assert "unreachable" in capsys.readouterr().out


def test_cli_doctor_json_output(monkeypatch, capsys):
    async def fake_run_checks(**_kwargs):
        report = doctor.Report()
        report.add("qdrant", "ok", "up")
        return report

    monkeypatch.setattr(doctor, "run_checks", fake_run_checks)
    assert cli_main(["doctor", "--json"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out


def test_cli_defaults_to_serve(monkeypatch):
    """Bare flags must still reach the server, for existing launchers."""
    captured: dict[str, object] = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("mcp_fetch_server.__main__.run_server", fake_run_server)
    assert cli_main(["--transport", "stdio"]) == 0
    assert captured == {"transport": "stdio", "host": "127.0.0.1", "port": 8000}


def test_cli_explicit_serve_subcommand(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("mcp_fetch_server.__main__.run_server", fake_run_server)
    assert cli_main(["serve", "--transport", "streamable-http", "--port", "9000"]) == 0
    assert captured["transport"] == "streamable-http"
    assert captured["port"] == 9000
