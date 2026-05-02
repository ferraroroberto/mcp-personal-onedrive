"""Entrypoint: ``python -m onedrive_mcp [--stdio|--http|--self-test]``."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from .config import load_settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        # NEVER write logs to stdout — MCP stdio mode treats stdout as the JSON-RPC channel.
        stream=sys.stderr,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onedrive-mcp",
        description="Read-only MCP server for personal OneDrive (consumer Microsoft accounts).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true", help="Serve over stdio (default; for Claude Desktop / Claude Code).")
    mode.add_argument("--http", action="store_true", help="Serve over Streamable HTTP behind the OAuth gateway.")
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="Sign in to Microsoft interactively, then list the OneDrive root and exit.",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default INFO).")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if args.self_test:
        return _run_self_test()
    if args.http:
        return _run_http()
    return _run_stdio()


def _run_stdio() -> int:
    settings = load_settings(require_http=False)
    from .server import build_app

    app, graph = build_app(settings)
    try:
        app.run()  # default transport: stdio
    finally:
        graph.close()
    return 0


def _run_http() -> int:
    import uvicorn

    settings = load_settings(require_http=True)
    from .server import build_http_application

    asgi = build_http_application(settings)
    logger = logging.getLogger(__name__)
    logger.info("Starting HTTP MCP server on %s:%s (public URL %s)",
                settings.http_host, settings.http_port, settings.public_url)
    uvicorn.run(
        asgi,
        host=settings.http_host,
        port=settings.http_port,
        log_level="info",
        access_log=False,
    )
    return 0


def _run_self_test() -> int:
    from .auth_graph import GraphAuth
    from .graph_client import GraphClient

    settings = load_settings(require_http=False)
    auth = GraphAuth(
        client_id=settings.onedrive_client_id,
        token_cache_path=settings.token_cache_path,
    )
    print("Acquiring Microsoft Graph token (browser will open if needed)...", file=sys.stderr)
    auth.get_access_token(allow_interactive=True)
    print("Sign-in OK. Listing OneDrive root...", file=sys.stderr)
    graph = GraphClient(auth)
    try:
        result = graph.list_children(top=10)
    finally:
        graph.close()
    items = result.get("items") or []
    print(f"\nFound {len(items)} item(s) at the root:", file=sys.stderr)
    for item in items:
        kind = "DIR " if item.get("is_folder") else "FILE"
        size = item.get("size", 0)
        print(f"  {kind}  {size:>12}  {item.get('name')}", file=sys.stderr)
    print(f"\nToken cache saved to {settings.token_cache_path}", file=sys.stderr)
    print("You can now run the server in stdio or HTTP mode.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
