"""Tests for GraphClient.download() — focuses on the no-downloadUrl fallback.

The primary (pre-signed URL) path works via follow_redirects=True on an
unauthenticated URL. The secondary (fallback) path must:
  1. Call the authenticated /content endpoint with the bearer token.
  2. Follow the 302 redirect to receive real file bytes.
  3. Raise GraphError when the authenticated endpoint returns >= 400.

These tests use httpx.MockTransport to avoid any real network calls.
"""
from __future__ import annotations

import base64
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

from onedrive_mcp.graph_client import DEFAULT_DOWNLOAD_LIMIT, GraphClient, GraphError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auth(token: str = "test-token") -> MagicMock:
    auth = MagicMock()
    auth.get_access_token.return_value = token
    return auth


def _meta_response(
    *,
    item_id: str = "abc123",
    size: int = 12,
    download_url: Optional[str] = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": item_id,
        "name": "hello.txt",
        "size": size,
        "file": {"mimeType": "text/plain"},
        "parentReference": {"path": "/drive/root:/"},
    }
    if download_url is not None:
        meta["@microsoft.graph.downloadUrl"] = download_url
    return meta


def _build_mock_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """Return a MockTransport that replays *responses* in order."""
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        resp = next(iterator)
        # Attach the request so httpx is happy.
        resp.request = request
        return resp

    return httpx.MockTransport(handler)


def _json_response(body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def _bytes_response(data: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=data)


def _make_client(transport: httpx.MockTransport) -> GraphClient:
    client = GraphClient.__new__(GraphClient)
    client._auth = _make_auth()
    client._client = httpx.Client(transport=transport)
    return client


# ---------------------------------------------------------------------------
# Tests: fallback path (no @microsoft.graph.downloadUrl)
# ---------------------------------------------------------------------------

class TestDownloadFallbackPath:
    """Fallback: Graph omits downloadUrl, we hit /content which redirects."""

    def test_fallback_follows_redirect_and_returns_bytes(self):
        """When /content returns 302, the client follows it and decodes bytes."""
        file_bytes = b"Hello, OneDrive!"
        meta = _meta_response(size=len(file_bytes))  # no downloadUrl

        transport = _build_mock_transport([
            # 1. GET meta URL -> metadata JSON
            _json_response(meta),
            # 2. GET /content (with auth header) -> 200 with actual bytes
            #    (MockTransport handles follow_redirects; we simulate final dest)
            _bytes_response(file_bytes),
        ])
        gc = _make_client(transport)

        result = gc.download(item_id="abc123")

        assert result["encoding"] == "utf-8"
        assert result["content"] == "Hello, OneDrive!"

    def test_fallback_sends_auth_header(self):
        """The bearer token must appear in the /content request."""
        file_bytes = b"data"
        meta = _meta_response(size=len(file_bytes))

        received_headers: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.append(dict(request.headers))
            if "content" in str(request.url):
                r = httpx.Response(200, content=file_bytes)
            else:
                r = httpx.Response(200, json=meta)
            r.request = request
            return r

        transport = httpx.MockTransport(handler)
        gc = _make_client(transport)
        gc.download(item_id="abc123")

        content_req_headers = received_headers[1]  # second call is /content
        assert "authorization" in content_req_headers
        assert content_req_headers["authorization"] == "Bearer test-token"

    def test_fallback_raises_on_4xx(self):
        """GraphError must be raised when /content returns >= 400."""
        meta = _meta_response(size=5)

        transport = _build_mock_transport([
            _json_response(meta),
            _bytes_response(b"Forbidden", status=403),
        ])
        gc = _make_client(transport)

        with pytest.raises(GraphError) as exc_info:
            gc.download(item_id="abc123")

        assert exc_info.value.status == 403

    def test_fallback_raises_on_5xx(self):
        """GraphError must be raised when /content returns 500."""
        meta = _meta_response(size=5)

        transport = _build_mock_transport([
            _json_response(meta),
            _bytes_response(b"Internal Error", status=500),
        ])
        gc = _make_client(transport)

        with pytest.raises(GraphError) as exc_info:
            gc.download(item_id="abc123")

        assert exc_info.value.status == 500

    def test_fallback_binary_file_base64_encoded(self):
        """Non-text binary content is returned as base64."""
        file_bytes = bytes(range(16))
        meta = _meta_response(size=len(file_bytes))
        # Override mime to non-text
        meta["file"]["mimeType"] = "application/octet-stream"

        transport = _build_mock_transport([
            _json_response(meta),
            _bytes_response(file_bytes),
        ])
        gc = _make_client(transport)

        result = gc.download(item_id="abc123")

        assert result["encoding"] == "base64"
        assert base64.b64decode(result["content"]) == file_bytes


# ---------------------------------------------------------------------------
# Tests: primary path (pre-signed downloadUrl present) — regression guard
# ---------------------------------------------------------------------------

class TestDownloadPrimaryPath:
    """Primary path still works after the refactor."""

    def test_primary_path_follows_redirect_no_auth(self):
        """Pre-signed URL must NOT receive the bearer token."""
        file_bytes = b"primary path bytes"
        meta = _meta_response(
            size=len(file_bytes),
            download_url="https://storage.example.com/presigned?sig=abc",
        )

        received_headers: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.append(dict(request.headers))
            if "graph.microsoft.com" in str(request.url):
                r = httpx.Response(200, json=meta)
            else:
                r = httpx.Response(200, content=file_bytes)
            r.request = request
            return r

        transport = httpx.MockTransport(handler)
        gc = _make_client(transport)
        result = gc.download(item_id="abc123")

        assert result["content"] == file_bytes.decode()

        # The pre-signed URL request must NOT carry the bearer token.
        presigned_headers = received_headers[1]
        assert "authorization" not in presigned_headers

    def test_primary_path_raises_on_4xx(self):
        """GraphError on 4xx from the pre-signed URL."""
        meta = _meta_response(
            size=5,
            download_url="https://storage.example.com/presigned",
        )

        transport = _build_mock_transport([
            _json_response(meta),
            _bytes_response(b"Not Found", status=404),
        ])
        gc = _make_client(transport)

        with pytest.raises(GraphError) as exc_info:
            gc.download(item_id="abc123")

        assert exc_info.value.status == 404
