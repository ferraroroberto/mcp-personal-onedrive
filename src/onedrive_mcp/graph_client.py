"""Thin Microsoft Graph REST client for OneDrive read-only operations.

Path lookups: /me/drive/root:/Path/To/Item:
Item lookups: /me/drive/items/{id}
Pagination via @odata.nextLink (we expose it as opaque ``next_page_token``).
"""

from __future__ import annotations

import base64
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .auth_graph import GraphAuth

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_DOWNLOAD_LIMIT = 10 * 1024 * 1024  # 10 MB
INLINE_TEXT_MIME_PREFIXES = ("text/",)
INLINE_TEXT_MIME_EXACT = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-ndjson",
}


class GraphError(RuntimeError):
    def __init__(self, status: int, message: str, body: Optional[dict] = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}


@dataclass
class NormalizedItem:
    id: str
    name: str
    path: str
    size: int
    mime_type: Optional[str]
    last_modified: Optional[str]
    is_folder: bool
    web_url: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "mime_type": self.mime_type,
            "last_modified": self.last_modified,
            "is_folder": self.is_folder,
            "web_url": self.web_url,
        }


def _normalize(item: dict[str, Any]) -> NormalizedItem:
    parent_path = ""
    parent_ref = item.get("parentReference") or {}
    if isinstance(parent_ref.get("path"), str):
        # path looks like "/drive/root:/Documents/Sub" — we want everything after the first colon.
        raw = parent_ref["path"]
        idx = raw.find(":")
        if idx >= 0:
            parent_path = raw[idx + 1 :]
    full_path = (parent_path.rstrip("/") + "/" + item.get("name", "")).lstrip("/")
    is_folder = "folder" in item
    file_section = item.get("file") or {}
    return NormalizedItem(
        id=item.get("id", ""),
        name=item.get("name", ""),
        path="/" + full_path if full_path else "/",
        size=int(item.get("size", 0)),
        mime_type=file_section.get("mimeType"),
        last_modified=item.get("lastModifiedDateTime"),
        is_folder=is_folder,
        web_url=item.get("webUrl"),
    )


def _quote_path(path: str) -> str:
    """Percent-encode a OneDrive path segment for the /root:/{path}: syntax."""
    p = path.strip()
    if not p or p == "/":
        return ""
    # Strip leading slash; Graph syntax is /root:/Folder/File:
    if p.startswith("/"):
        p = p[1:]
    # Quote each segment but preserve forward slashes.
    return urllib.parse.quote(p, safe="/")


class GraphClient:
    """Synchronous Graph client. One per process is fine."""

    def __init__(self, auth: GraphAuth, *, timeout: float = 30.0) -> None:
        self._auth = auth
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ requests
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._auth.get_access_token()}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        max_retries: int = 3,
    ) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                response = self._client.request(
                    method, url, params=params, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == max_retries:
                    raise
                time.sleep(min(2**attempt, 8))
                continue

            if response.status_code in (429, 503):
                retry_after = float(response.headers.get("Retry-After", "1"))
                logger.warning("Graph throttled (HTTP %s); sleeping %.1fs", response.status_code, retry_after)
                if attempt == max_retries:
                    raise GraphError(response.status_code, "Throttled", _safe_json(response))
                time.sleep(min(retry_after, 30))
                continue

            if response.status_code >= 400:
                raise GraphError(
                    response.status_code,
                    f"Graph error {response.status_code}: {response.text[:300]}",
                    _safe_json(response),
                )
            return response

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ helpers
    def _resolve_item_url(self, *, path: Optional[str], item_id: Optional[str]) -> str:
        if item_id and path:
            raise ValueError("Pass exactly one of path or item_id, not both.")
        if item_id:
            return f"{GRAPH_BASE}/me/drive/items/{urllib.parse.quote(item_id, safe='')}"
        if path is None or path.strip() in ("", "/"):
            return f"{GRAPH_BASE}/me/drive/root"
        return f"{GRAPH_BASE}/me/drive/root:/{_quote_path(path)}"

    def _children_url(self, *, path: Optional[str], item_id: Optional[str]) -> str:
        if item_id and path:
            raise ValueError("Pass exactly one of path or item_id, not both.")
        if item_id:
            return f"{GRAPH_BASE}/me/drive/items/{urllib.parse.quote(item_id, safe='')}/children"
        if path is None or path.strip() in ("", "/"):
            return f"{GRAPH_BASE}/me/drive/root/children"
        return f"{GRAPH_BASE}/me/drive/root:/{_quote_path(path)}:/children"

    # ------------------------------------------------------------------ tools
    def list_children(
        self,
        *,
        path: Optional[str] = None,
        item_id: Optional[str] = None,
        top: int = 50,
        page_token: Optional[str] = None,
    ) -> dict[str, Any]:
        if page_token:
            url = _decode_page_token(page_token)
            params = None
        else:
            url = self._children_url(path=path, item_id=item_id)
            params = {"$top": max(1, min(top, 200))}
        response = self._request("GET", url, params=params)
        body = response.json()
        items = [_normalize(it).to_dict() for it in body.get("value", [])]
        next_link = body.get("@odata.nextLink")
        return {
            "items": items,
            "next_page_token": _encode_page_token(next_link) if next_link else None,
        }

    def search(self, query: str, *, top: int = 25) -> dict[str, Any]:
        if not query.strip():
            raise ValueError("query must not be empty")
        url = f"{GRAPH_BASE}/me/drive/root/search(q='{urllib.parse.quote(query)}')"
        response = self._request("GET", url, params={"$top": max(1, min(top, 200))})
        body = response.json()
        return {"items": [_normalize(it).to_dict() for it in body.get("value", [])]}

    def get_item(
        self,
        *,
        path: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> dict[str, Any]:
        url = self._resolve_item_url(path=path, item_id=item_id)
        response = self._request("GET", url)
        body = response.json()
        normalized = _normalize(body).to_dict()
        # Add a curated set of extra fields that the metadata tool exposes.
        normalized["created_date_time"] = body.get("createdDateTime")
        normalized["created_by"] = _person_summary(body.get("createdBy"))
        normalized["last_modified_by"] = _person_summary(body.get("lastModifiedBy"))
        normalized["e_tag"] = body.get("eTag")
        normalized["c_tag"] = body.get("cTag")
        normalized["parent_id"] = (body.get("parentReference") or {}).get("id")
        file_section = body.get("file") or {}
        normalized["file_hashes"] = file_section.get("hashes")
        return normalized

    def download(
        self,
        *,
        path: Optional[str] = None,
        item_id: Optional[str] = None,
        max_bytes: int = DEFAULT_DOWNLOAD_LIMIT,
    ) -> dict[str, Any]:
        meta_url = self._resolve_item_url(path=path, item_id=item_id)
        meta = self._request("GET", meta_url).json()
        if "folder" in meta:
            raise ValueError("Cannot download a folder.")
        size = int(meta.get("size", 0))
        if size > max_bytes:
            raise ValueError(
                f"File is {size} bytes which exceeds max_bytes={max_bytes}. "
                "Pass a larger max_bytes if you really want to download it."
            )
        download_url = meta.get("@microsoft.graph.downloadUrl")
        if not download_url:
            # Fall back to /content (follows redirect to a pre-signed URL).
            download_url = f"{meta_url}/content"
            response = self._request("GET", download_url)
        else:
            # The pre-signed URL is unauthenticated; do not send our bearer.
            response = self._client.get(download_url, follow_redirects=True)
            if response.status_code >= 400:
                raise GraphError(
                    response.status_code,
                    f"Download failed: HTTP {response.status_code}",
                )
        data = response.content
        mime = (meta.get("file") or {}).get("mimeType") or "application/octet-stream"
        normalized = _normalize(meta).to_dict()
        if _is_inline_text(mime):
            try:
                text = data.decode("utf-8")
                return {**normalized, "encoding": "utf-8", "content": text}
            except UnicodeDecodeError:
                pass  # fall through to base64
        return {
            **normalized,
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }


def _is_inline_text(mime: str) -> bool:
    if mime in INLINE_TEXT_MIME_EXACT:
        return True
    return any(mime.startswith(prefix) for prefix in INLINE_TEXT_MIME_PREFIXES)


def _person_summary(value: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    if not isinstance(value, dict):
        return None
    user = value.get("user") or {}
    return {k: v for k, v in user.items() if k in ("displayName", "email", "id")}


def _safe_json(response: httpx.Response) -> Optional[dict]:
    try:
        return response.json()
    except ValueError:
        return None


def _encode_page_token(next_link: str) -> str:
    return base64.urlsafe_b64encode(next_link.encode("utf-8")).decode("ascii")


def _decode_page_token(token: str) -> str:
    try:
        url = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid page_token") from exc
    if not url.startswith("https://graph.microsoft.com/"):
        raise ValueError("Invalid page_token")
    return url
