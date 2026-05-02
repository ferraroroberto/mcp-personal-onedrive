"""FastMCP server: tool registrations, transport selection, OAuth wiring."""

from __future__ import annotations

import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .auth_graph import GraphAuth
from .config import Settings
from .graph_client import DEFAULT_DOWNLOAD_LIMIT, GraphClient

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> tuple[FastMCP, GraphClient]:
    """Build the FastMCP app and register all read-only OneDrive tools.

    The Graph client is constructed eagerly so the token cache is loaded once;
    individual tool calls reuse it. Returned alongside the app so callers can
    close it on shutdown if they care.
    """
    auth = GraphAuth(
        client_id=settings.onedrive_client_id,
        token_cache_path=settings.token_cache_path,
    )
    graph = GraphClient(auth)

    app = FastMCP(
        name="personal-onedrive",
        instructions=(
            "Read-only access to the signed-in user's personal OneDrive. "
            "Paths are case-insensitive, start with '/', and use forward slashes "
            "(e.g. '/Documents/Notes.md'). Pass either 'path' or 'item_id', not both."
        ),
    )

    @app.tool(description="List the items at the root of the user's personal OneDrive.")
    def list_root(top: int = 50) -> dict[str, Any]:
        return graph.list_children(top=top)

    @app.tool(
        description=(
            "List the items inside a OneDrive folder. Pass either 'path' "
            "(e.g. '/Documents') or 'item_id'. Use 'page_token' to fetch the "
            "next page returned by a previous call."
        )
    )
    def list_folder(
        path: Optional[str] = None,
        item_id: Optional[str] = None,
        top: int = 50,
        page_token: Optional[str] = None,
    ) -> dict[str, Any]:
        return graph.list_children(path=path, item_id=item_id, top=top, page_token=page_token)

    @app.tool(
        description=(
            "Search across the user's personal OneDrive for items whose name or "
            "content matches the query. Returns up to 'top' items."
        )
    )
    def search_files(query: str, top: int = 25) -> dict[str, Any]:
        return graph.search(query, top=top)

    @app.tool(
        description=(
            "Return full metadata for one OneDrive item, identified by 'path' "
            "or 'item_id'. Includes timestamps, hashes, parent folder, and web URL."
        )
    )
    def get_item_metadata(
        path: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return graph.get_item(path=path, item_id=item_id)

    @app.tool(
        description=(
            "Download a OneDrive file's content. Text-like files (text/*, JSON, "
            "XML, YAML) are returned inline as UTF-8 text. Binary files are "
            "returned base64-encoded. Files larger than 'max_bytes' are rejected."
        )
    )
    def download_file_content(
        path: Optional[str] = None,
        item_id: Optional[str] = None,
        max_bytes: int = DEFAULT_DOWNLOAD_LIMIT,
    ) -> dict[str, Any]:
        return graph.download(path=path, item_id=item_id, max_bytes=max_bytes)

    return app, graph


def build_http_application(settings: Settings):
    """Build a Starlette ASGI app that serves the MCP endpoint at /mcp behind
    a single-user OAuth 2.1 gateway. Lazy-imports starlette so stdio mode does
    not pay for it."""
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.routing import Mount

    from .auth_oauth import BearerAuthMiddleware, OAuthGateway

    if not settings.has_http_config:
        raise RuntimeError(
            "HTTP mode requires MCP_OAUTH_PASSWORD, MCP_PUBLIC_URL and a JWT secret. "
            "See README 'Configuration and testing' -> Step 4."
        )

    mcp_app, graph = build_app(settings)
    gateway = OAuthGateway(
        public_url=settings.public_url,  # type: ignore[arg-type]
        password=settings.oauth_password,  # type: ignore[arg-type]
        jwt_secret=settings.jwt_secret,  # type: ignore[arg-type]
        state_path=settings.oauth_state_path,
    )

    inner = mcp_app.streamable_http_app()

    # The MCP streamable_http_app has its own lifespan (it owns the session
    # manager). Compose it with our own so graph.close() runs on shutdown.
    inner_lifespan = inner.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with inner_lifespan(app):
            try:
                yield
            finally:
                graph.close()

    routes = list(gateway.routes()) + [Mount("/", app=inner)]
    parent = Starlette(routes=routes, lifespan=lifespan)
    parent.add_middleware(BearerAuthMiddleware, gateway=gateway, protected_prefix="/mcp")

    parent.state.gateway = gateway
    parent.state.graph = graph
    return parent
