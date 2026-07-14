"""Tests for sandboxed local file operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_fetch_server.files import (
    FileAccessError,
    list_directory,
    read_text_file,
    resolve_path,
    write_text_file,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested content", encoding="utf-8")
    (tmp_path / "top.txt").write_text("top content", encoding="utf-8")
    return tmp_path


def test_resolve_path_within_root(sandbox: Path) -> None:
    resolved = resolve_path("top.txt", [sandbox])
    assert resolved == (sandbox / "top.txt").resolve()


def test_resolve_path_rejects_parent_traversal(sandbox: Path) -> None:
    with pytest.raises(FileAccessError, match="outside all allowed"):
        resolve_path("../outside.txt", [sandbox])


def test_resolve_path_rejects_absolute_outside_root(
    sandbox: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = tmp_path_factory.mktemp("other")
    outside_file = other / "secret.txt"
    outside_file.write_text("nope", encoding="utf-8")

    with pytest.raises(FileAccessError, match="outside all allowed"):
        resolve_path(str(outside_file), [sandbox])


def test_resolve_path_no_roots_configured() -> None:
    with pytest.raises(FileAccessError, match="No local directories"):
        resolve_path("anything.txt", [])


def test_read_text_file_returns_content(sandbox: Path) -> None:
    content = read_text_file("sub/nested.txt", [sandbox])
    assert content == "nested content"


def test_read_text_file_missing_raises(sandbox: Path) -> None:
    with pytest.raises(FileAccessError, match="not found"):
        read_text_file("missing.txt", [sandbox])


def test_read_text_file_rejects_directory(sandbox: Path) -> None:
    with pytest.raises(FileAccessError, match="directory"):
        read_text_file("sub", [sandbox])


def test_write_text_file_creates_new_file(sandbox: Path) -> None:
    written = write_text_file("new/dir/file.txt", "hello", [sandbox])
    assert written.read_text(encoding="utf-8") == "hello"
    assert written.parent.exists()


def test_write_text_file_overwrites_existing(sandbox: Path) -> None:
    write_text_file("top.txt", "replaced", [sandbox])
    assert (sandbox / "top.txt").read_text(encoding="utf-8") == "replaced"


def test_list_directory_lists_files_and_dirs(sandbox: Path) -> None:
    entries = list_directory("", [sandbox])
    names = {entry.name for entry in entries}
    assert names == {"sub", "top.txt"}

    sub_entry = next(e for e in entries if e.name == "sub")
    assert sub_entry.is_dir is True

    top_entry = next(e for e in entries if e.name == "top.txt")
    assert top_entry.is_dir is False
    assert top_entry.size == len("top content")


def test_list_directory_missing_raises(sandbox: Path) -> None:
    with pytest.raises(FileAccessError, match="not found"):
        list_directory("does-not-exist", [sandbox])
