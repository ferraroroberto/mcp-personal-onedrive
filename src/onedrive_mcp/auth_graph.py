"""Microsoft Graph authentication for personal MSA accounts.

Uses MSAL PublicClientApplication pinned to the /consumers authority so this
server only ever talks to consumer Microsoft accounts (outlook.com, hotmail.com,
live.com). That is exactly the gap the official Microsoft 365 connector has.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import msal

logger = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Files.Read", "User.Read"]


class GraphAuth:
    """Acquires Microsoft Graph access tokens for a single signed-in MSA user.

    Token cache is persisted to disk so the user only has to sign in once.
    All public methods are thread-safe.
    """

    def __init__(self, client_id: str, token_cache_path: Path) -> None:
        self._client_id = client_id
        self._cache_path = token_cache_path
        self._lock = threading.Lock()
        self._cache = msal.SerializableTokenCache()
        self._load_cache()
        self._app = msal.PublicClientApplication(
            client_id=client_id,
            authority=AUTHORITY,
            token_cache=self._cache,
        )

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._cache.deserialize(self._cache_path.read_text(encoding="utf-8"))
                logger.debug("Loaded token cache from %s", self._cache_path)
            except OSError as exc:
                logger.warning("Could not read token cache %s: %s", self._cache_path, exc)

    def _save_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(self._cache.serialize(), encoding="utf-8")
        logger.debug("Saved token cache to %s", self._cache_path)

    def get_access_token(self, *, allow_interactive: bool = False) -> str:
        """Return a valid Microsoft Graph access token. Tries silent acquisition
        first (refresh token); falls back to an interactive browser flow if
        ``allow_interactive`` is True. Raises RuntimeError otherwise."""
        with self._lock:
            accounts = self._app.get_accounts()
            if accounts:
                result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
                if result and "access_token" in result:
                    self._save_cache()
                    return result["access_token"]
                logger.info("Silent token acquisition failed; cache may be stale.")

            if not allow_interactive:
                raise RuntimeError(
                    "No cached Microsoft credentials. Run "
                    "'python -m onedrive_mcp --self-test' on this machine to sign in once."
                )

            logger.info("Opening browser for interactive Microsoft sign-in...")
            result = self._app.acquire_token_interactive(
                scopes=SCOPES,
                prompt="select_account",
            )
            if "access_token" not in result:
                raise RuntimeError(
                    f"Interactive sign-in failed: "
                    f"{result.get('error')} - {result.get('error_description')}"
                )
            self._save_cache()
            return result["access_token"]

