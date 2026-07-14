"""Sandboxed local file operations backing read_file/write_file/list_dir.

All paths are resolved against a fixed set of allowed root directories
(configured via FETCH_LOCAL_FILES_ROOT, optionally extended by MCP client
"roots") and rejected if they would resolve outside every allowed root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp_fetch_server.config import settings


class FileAccessError(Exception):
    """Raised when a file path fails sandbox validation or an I/O op fails."""


@dataclass(slots=True)
class DirEntry:
    name: str
    is_dir: bool
    size: int | None


def configured_root() -> Path | None:
    root = settings.local_files_root.strip()
    if not root:
        return None
    path = Path(root).expanduser()
    if not path.exists() or not path.is_dir():
        raise FileAccessError(f"Configured FETCH_LOCAL_FILES_ROOT does not exist: {root}")
    return path.resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return path == root


def resolve_path(relative_path: str, roots: list[Path]) -> Path:
    if not roots:
        raise FileAccessError(
            "No local directories are configured. Set FETCH_LOCAL_FILES_ROOT to enable "
            "local file tools."
        )

    cleaned = relative_path.strip()
    candidate_input = Path(cleaned) if cleaned else Path(".")

    if candidate_input.is_absolute():
        try:
            candidate = candidate_input.resolve()
        except OSError as exc:
            raise FileAccessError(f"Invalid path: {relative_path}") from exc
        for root in roots:
            if _is_within(candidate, root):
                return candidate
        raise FileAccessError(f"Path is outside all allowed directories: {relative_path}")

    for root in roots:
        try:
            candidate = (root / candidate_input).resolve()
        except OSError:
            continue
        if _is_within(candidate, root):
            return candidate

    raise FileAccessError(f"Path is outside all allowed directories: {relative_path}")


def read_text_file(relative_path: str, roots: list[Path]) -> str:
    path = resolve_path(relative_path, roots)
    if not path.exists():
        raise FileAccessError(f"File not found: {relative_path}")
    if path.is_dir():
        raise FileAccessError(f"Path is a directory, not a file: {relative_path}")

    size = path.stat().st_size
    if size > settings.max_file_read_bytes:
        raise FileAccessError(
            f"File too large to read ({size} bytes, limit {settings.max_file_read_bytes})"
        )

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise FileAccessError(f"Could not read file: {exc}") from exc


def write_text_file(relative_path: str, content: str, roots: list[Path]) -> Path:
    path = resolve_path(relative_path, roots)
    if path.is_dir():
        raise FileAccessError(f"Path is a directory, not a file: {relative_path}")

    encoded = content.encode("utf-8")
    if len(encoded) > settings.max_file_write_bytes:
        raise FileAccessError(
            f"Content too large to write ({len(encoded)} bytes, "
            f"limit {settings.max_file_write_bytes})"
        )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise FileAccessError(f"Could not write file: {exc}") from exc
    return path


def list_directory(relative_path: str, roots: list[Path]) -> list[DirEntry]:
    path = resolve_path(relative_path, roots) if relative_path.strip() else roots[0]
    if not path.exists():
        raise FileAccessError(f"Directory not found: {relative_path}")
    if not path.is_dir():
        raise FileAccessError(f"Path is not a directory: {relative_path}")

    entries: list[DirEntry] = []
    try:
        children = sorted(path.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
    except OSError as exc:
        raise FileAccessError(f"Could not list directory: {exc}") from exc

    for child in children:
        try:
            size = None if child.is_dir() else child.stat().st_size
        except OSError:
            size = None
        entries.append(DirEntry(name=child.name, is_dir=child.is_dir(), size=size))
    return entries


def format_entries(path_label: str, entries: list[DirEntry]) -> str:
    if not entries:
        return f"[Empty directory] {path_label}"
    lines = [f"Directory: {path_label}"]
    for entry in entries:
        if entry.is_dir:
            lines.append(f"  [DIR]  {entry.name}")
        else:
            lines.append(f"  [FILE] {entry.name} ({entry.size} bytes)")
    return "\n".join(lines)
