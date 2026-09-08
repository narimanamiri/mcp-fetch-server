"""Console entry point for the MCP fetch server."""

from __future__ import annotations

import argparse
import sys
from typing import Literal

from mcp_fetch_server.server import run_server

# Subcommands are opt-in: anything else is treated as `serve` arguments so
# existing launchers (`mcp-fetch-server --transport stdio`) keep working.
SUBCOMMANDS = {"serve", "doctor"}


def _parse_serve_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-fetch-server serve",
        description="Run the MCP web fetch server",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    return parser.parse_args(argv)


def _serve(argv: list[str]) -> int:
    args = _parse_serve_args(argv)
    transport: Literal["stdio", "streamable-http"] = args.transport
    run_server(transport=transport, host=args.host, port=args.port)
    return 0


def _doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-fetch-server doctor",
        description="Check the offline corpus stack: local model, Qdrant, storage",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    import asyncio
    import json

    from mcp_fetch_server.rag.doctor import run_checks

    report = asyncio.run(run_checks())
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    if arguments and arguments[0] in SUBCOMMANDS:
        command, rest = arguments[0], arguments[1:]
    else:
        command, rest = "serve", arguments

    if command == "doctor":
        return _doctor(rest)
    return _serve(rest)


if __name__ == "__main__":
    raise SystemExit(main())
