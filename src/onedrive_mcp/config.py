"""Configuration loader. Reads .env once at startup, exposes a frozen Settings object."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Settings:
    onedrive_client_id: str
    token_cache_path: Path

    oauth_password: Optional[str]
    jwt_secret: Optional[str]
    public_url: Optional[str]
    http_host: str
    http_port: int
    oauth_state_path: Path

    @property
    def has_http_config(self) -> bool:
        return bool(self.oauth_password and self.public_url and self.jwt_secret)


def _resolve_path(value: str, default_name: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def load_settings(*, require_http: bool = False) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    client_id = os.getenv("ONEDRIVE_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError(
            "ONEDRIVE_CLIENT_ID is not set. Copy .env.sample to .env and fill it in. "
            "See README 'Configuration and testing' -> Step 1."
        )

    token_cache_path = _resolve_path(
        os.getenv("ONEDRIVE_TOKEN_CACHE_PATH", ".token_cache.bin"),
        ".token_cache.bin",
    )
    oauth_state_path = _resolve_path(
        os.getenv("MCP_OAUTH_STATE_PATH", ".oauth_state.json"),
        ".oauth_state.json",
    )

    oauth_password = os.getenv("MCP_OAUTH_PASSWORD") or None
    public_url = (os.getenv("MCP_PUBLIC_URL") or "").rstrip("/") or None
    http_host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
    http_port = int(os.getenv("MCP_HTTP_PORT", "8080"))

    jwt_secret = os.getenv("MCP_JWT_SECRET") or _load_or_create_jwt_secret(
        oauth_state_path, persist=require_http
    )

    settings = Settings(
        onedrive_client_id=client_id,
        token_cache_path=token_cache_path,
        oauth_password=oauth_password,
        jwt_secret=jwt_secret,
        public_url=public_url,
        http_host=http_host,
        http_port=http_port,
        oauth_state_path=oauth_state_path,
    )

    if require_http:
        missing = []
        if not oauth_password:
            missing.append("MCP_OAUTH_PASSWORD")
        if not public_url:
            missing.append("MCP_PUBLIC_URL")
        if missing:
            raise RuntimeError(
                f"HTTP mode requires these .env keys: {', '.join(missing)}. "
                "See README 'Configuration and testing' -> Step 4."
            )

    return settings


def _load_or_create_jwt_secret(state_path: Path, *, persist: bool) -> Optional[str]:
    """Read or generate a JWT secret. We persist alongside oauth state so that a
    server restart does not invalidate every issued token."""
    import json

    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            existing = data.get("jwt_secret")
            if isinstance(existing, str) and len(existing) >= 32:
                return existing
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", state_path, exc)

    if not persist:
        return None

    secret = secrets.token_hex(32)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"jwt_secret": secret, "clients": {}, "refresh_tokens": {}}
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Generated new JWT secret -> %s", state_path)
    return secret
