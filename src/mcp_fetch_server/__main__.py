"""Console entry point for the MCP fetch server."""

from __future__ import annotations

import argparse
from typing import Literal

from mcp_fetch_server.server import run_server


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCP web fetch server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    transport: Literal["stdio", "streamable-http"] = args.transport
    run_server(transport=transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
