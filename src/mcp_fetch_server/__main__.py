"""Console entry point for the MCP fetch server."""

from __future__ import annotations

import argparse
import sys
from typing import Literal

from mcp_fetch_server.server import run_server

# Subcommands are opt-in: anything else is treated as `serve` arguments so
# existing launchers (`mcp-fetch-server --transport stdio`) keep working.
SUBCOMMANDS = {"serve", "doctor", "ingest", "enrich", "taxonomy", "classify"}


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


def _ingest(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-fetch-server ingest",
        description="Load documents into the offline corpus",
    )
    parser.add_argument("paths", nargs="+", help="Files or directories to ingest")
    parser.add_argument(
        "--reingest",
        action="store_true",
        help="Re-parse files even when their contents have not changed",
    )
    parser.add_argument("--workers", type=int, default=None, help="Parallel parse workers")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N files")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the files that would be ingested and exit",
    )
    parser.add_argument("--json", action="store_true", help="Emit the summary as JSON")
    parser.add_argument("--quiet", action="store_true", help="Only print the final summary")
    args = parser.parse_args(argv)

    import asyncio
    import json

    from mcp_fetch_server.rag.ingest import IngestResult, discover_files, ingest_paths

    if args.dry_run:
        files = discover_files(args.paths)
        if args.limit is not None:
            files = files[: args.limit]
        for path in files:
            print(path)
        print(f"\n{len(files)} file(s) would be ingested.")
        return 0

    def report(result: IngestResult) -> None:
        if args.quiet or args.json:
            return
        marker = {"ingested": "+", "skipped": "=", "failed": "!"}[result.status]
        detail = (
            f"{result.chunks} chunks"
            if result.status != "failed"
            else (result.error or "failed")
        )
        print(f"{marker} {result.path.name}: {detail}")

    summary = asyncio.run(
        ingest_paths(
            args.paths,
            reingest=args.reingest,
            workers=args.workers,
            limit=args.limit,
            on_result=report,
        )
    )

    if args.json:
        print(json.dumps(summary.as_dict(), indent=2, ensure_ascii=False))
    else:
        if not args.quiet:
            print()
        print(summary.render())

    return 1 if summary.failed else 0


def _enrich(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-fetch-server enrich",
        description="Have the local model describe ingested documents",
    )
    parser.add_argument(
        "--reenrich", action="store_true", help="Re-describe documents that already have metadata"
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N documents")
    parser.add_argument("--json", action="store_true", help="Emit the summary as JSON")
    parser.add_argument("--quiet", action="store_true", help="Only print the final summary")
    args = parser.parse_args(argv)

    import asyncio
    import json

    from mcp_fetch_server.rag.catalog import Catalog
    from mcp_fetch_server.rag.enrich import EnrichResult, run_enrichment

    def report(result: EnrichResult) -> None:
        if args.quiet or args.json:
            return
        marker = {"enriched": "+", "skipped": "=", "failed": "!"}[result.status]
        detail = result.error if result.status == "failed" else (result.title or result.doc_id)
        print(f"{marker} {detail}")

    async def run() -> object:
        with Catalog() as catalog:
            return await run_enrichment(
                catalog=catalog,
                reenrich=args.reenrich,
                limit=args.limit,
                on_result=report,
            )

    summary = asyncio.run(run())

    if args.json:
        print(json.dumps(summary.as_dict(), indent=2, ensure_ascii=False))
    else:
        if not args.quiet:
            print()
        print(summary.render())
    return 1 if summary.failed else 0


def _classify(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-fetch-server classify",
        description="Assign documents to categories from the taxonomy",
    )
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-classify documents that already have categories",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N documents")
    parser.add_argument("--json", action="store_true", help="Emit the summary as JSON")
    args = parser.parse_args(argv)

    import asyncio
    import json

    from mcp_fetch_server.rag.catalog import Catalog
    from mcp_fetch_server.rag.taxonomy import Taxonomy, TaxonomyError, run_classification

    taxonomy = Taxonomy.load()
    if not taxonomy:
        print(
            "No taxonomy yet. Create one with:\n"
            "  mcp-fetch-server taxonomy bootstrap",
            file=sys.stderr,
        )
        return 1

    async def run() -> object:
        with Catalog() as catalog:
            return await run_classification(
                catalog=catalog,
                taxonomy=taxonomy,
                reclassify=args.reclassify,
                limit=args.limit,
            )

    try:
        summary = asyncio.run(run())
    except TaxonomyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(summary.render())
    return 1 if summary.failed else 0


def _taxonomy(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-fetch-server taxonomy",
        description="Inspect or create the corpus taxonomy",
    )
    parser.add_argument(
        "action",
        choices=["show", "bootstrap", "path"],
        help="show: print it; bootstrap: propose one from the corpus; path: print its location",
    )
    parser.add_argument("--sample", type=int, default=60, help="Documents to sample when proposing")
    parser.add_argument("--max-categories", type=int, default=20, help="Upper bound on categories")
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing taxonomy file"
    )
    args = parser.parse_args(argv)

    import asyncio

    from mcp_fetch_server.rag.catalog import Catalog
    from mcp_fetch_server.rag.taxonomy import Taxonomy, TaxonomyError, bootstrap_taxonomy

    target = Taxonomy.default_path()

    if args.action == "path":
        print(target)
        return 0

    if args.action == "show":
        taxonomy = Taxonomy.load()
        if not taxonomy:
            print(f"No taxonomy at {target}. Create one with: taxonomy bootstrap")
            return 1
        print(taxonomy.as_prompt_block())
        print(f"\n{len(taxonomy)} categories in {target}")
        return 0

    if target.exists() and not args.force:
        print(
            f"A taxonomy already exists at {target}.\n"
            "Categories are meant to be stable, so re-proposing them would orphan "
            "existing classifications. Edit the file by hand, or pass --force.",
            file=sys.stderr,
        )
        return 1

    async def run() -> object:
        with Catalog() as catalog:
            return await bootstrap_taxonomy(
                catalog=catalog,
                sample_size=args.sample,
                max_categories=args.max_categories,
            )

    try:
        taxonomy = asyncio.run(run())
    except TaxonomyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    written = taxonomy.save()
    print(taxonomy.as_prompt_block())
    print(f"\nWrote {len(taxonomy)} categories to {written}")
    print("Review and edit that file, then run: mcp-fetch-server classify")
    return 0


def _force_utf8_output() -> None:
    """Make stdout/stderr UTF-8 safe.

    A Windows console defaults to cp1252, so printing a Persian document title
    or an em dash raises UnicodeEncodeError and takes the whole command with
    it. Corpus output is routinely non-Latin, so this is not an edge case.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    arguments = list(sys.argv[1:] if argv is None else argv)

    if arguments and arguments[0] in SUBCOMMANDS:
        command, rest = arguments[0], arguments[1:]
    else:
        command, rest = "serve", arguments

    if command == "doctor":
        return _doctor(rest)
    if command == "ingest":
        return _ingest(rest)
    if command == "enrich":
        return _enrich(rest)
    if command == "classify":
        return _classify(rest)
    if command == "taxonomy":
        return _taxonomy(rest)
    return _serve(rest)


if __name__ == "__main__":
    raise SystemExit(main())
