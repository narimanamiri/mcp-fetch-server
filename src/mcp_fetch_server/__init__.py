"""MCP web fetch server package."""

__version__ = "0.1.0"


def main() -> None:
    import sys

    from mcp_fetch_server.__main__ import main as cli_main

    sys.exit(cli_main())
