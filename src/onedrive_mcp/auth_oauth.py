"""Single-user OAuth 2.1 authorization server for the MCP gateway.

Implements just enough of RFC 6749 / 7636 / 7591 / 8414 / 9728 for Anthropic's
custom-connector flow to work:

  - GET  /.well-known/oauth-authorization-server   (RFC 8414)
  - GET  /.well-known/oauth-protected-resource     (RFC 9728)
  - POST /register                                  (RFC 7591 — Dynamic Client Registration)
  - GET  /authorize                                 (login form)
  - POST /authorize                                 (verifies password, mints code)
  - POST /token                                     (code + refresh_token grants)

Single-user model: only one human (the .env password owner) can ever be the
``sub`` of an issued token. Authorization codes are PKCE-bound and live in
memory for 60 seconds. Refresh tokens persist to disk so a server restart
doesn't kick the connector out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

JWT_ALG = "HS256"
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
AUTH_CODE_TTL_SECONDS = 60
SUBJECT = "owner"  # single-user
DEFAULT_SCOPE = "mcp"
LOGIN_RATE_WINDOW = 60.0
LOGIN_RATE_MAX = 8


@dataclass
class _AuthCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    expires_at: float
    used: bool = False


@dataclass
class _RegisteredClient:
    client_id: str
    redirect_uris: list[str]
    client_name: str
    created_at: int


@dataclass
class _RefreshToken:
    client_id: str
    scope: str
    expires_at: float


class OAuthGateway:
    """Stateful holder for clients, codes, refresh tokens, JWT secret. Thread-safe."""

    def __init__(
        self,
        *,
        public_url: str,
        password: str,
        jwt_secret: str,
        state_path: Path,
    ) -> None:
        if not public_url.startswith("https://") and not public_url.startswith("http://localhost"):
            logger.warning("MCP_PUBLIC_URL is %s — claude.ai requires HTTPS.", public_url)
        self._public_url = public_url.rstrip("/")
        self._password = password
        self._jwt_secret = jwt_secret
        self._state_path = state_path
        self._lock = threading.Lock()

        self._clients: dict[str, _RegisteredClient] = {}
        self._refresh_tokens: dict[str, _RefreshToken] = {}
        self._auth_codes: dict[str, _AuthCode] = {}
        self._login_attempts: list[float] = []

        self._load_state()

    # ---------------------------------------------------------------- state
    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", self._state_path, exc)
            return
        for cid, raw in (data.get("clients") or {}).items():
            self._clients[cid] = _RegisteredClient(
                client_id=cid,
                redirect_uris=list(raw.get("redirect_uris") or []),
                client_name=raw.get("client_name", "unknown"),
                created_at=int(raw.get("created_at", 0)),
            )
        now = time.time()
        for token, raw in (data.get("refresh_tokens") or {}).items():
            expires_at = float(raw.get("expires_at", 0))
            if expires_at <= now:
                continue
            self._refresh_tokens[token] = _RefreshToken(
                client_id=raw.get("client_id", ""),
                scope=raw.get("scope", DEFAULT_SCOPE),
                expires_at=expires_at,
            )

    def _save_state(self) -> None:
        payload = {
            "jwt_secret": self._jwt_secret,
            "clients": {
                cid: {
                    "redirect_uris": c.redirect_uris,
                    "client_name": c.client_name,
                    "created_at": c.created_at,
                }
                for cid, c in self._clients.items()
            },
            "refresh_tokens": {
                t: {
                    "client_id": r.client_id,
                    "scope": r.scope,
                    "expires_at": r.expires_at,
                }
                for t, r in self._refresh_tokens.items()
            },
        }
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    # ---------------------------------------------------------------- helpers
    @property
    def public_url(self) -> str:
        return self._public_url

    @property
    def metadata_url(self) -> str:
        return f"{self._public_url}/.well-known/oauth-protected-resource"

    def _check_rate_limit(self) -> bool:
        now = time.time()
        cutoff = now - LOGIN_RATE_WINDOW
        self._login_attempts = [t for t in self._login_attempts if t > cutoff]
        if len(self._login_attempts) >= LOGIN_RATE_MAX:
            return False
        self._login_attempts.append(now)
        return True

    def _purge_codes(self) -> None:
        now = time.time()
        self._auth_codes = {c: v for c, v in self._auth_codes.items() if v.expires_at > now and not v.used}

    # ---------------------------------------------------------------- token verify
    def verify_access_token(self, token: str) -> dict[str, Any]:
        return jwt.decode(
            token,
            self._jwt_secret,
            algorithms=[JWT_ALG],
            audience=self._public_url,
            issuer=self._public_url,
        )

    def _mint_access_token(self, client_id: str, scope: str) -> str:
        now = int(time.time())
        payload = {
            "iss": self._public_url,
            "aud": self._public_url,
            "sub": SUBJECT,
            "client_id": client_id,
            "scope": scope,
            "iat": now,
            "exp": now + ACCESS_TOKEN_TTL_SECONDS,
            "jti": secrets.token_hex(8),
        }
        return jwt.encode(payload, self._jwt_secret, algorithm=JWT_ALG)

    def _mint_refresh_token(self, client_id: str, scope: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._refresh_tokens[token] = _RefreshToken(
                client_id=client_id,
                scope=scope,
                expires_at=time.time() + REFRESH_TOKEN_TTL_SECONDS,
            )
            self._save_state()
        return token

    def _consume_refresh_token(self, token: str) -> _RefreshToken:
        with self._lock:
            data = self._refresh_tokens.pop(token, None)
            if data is None or data.expires_at <= time.time():
                raise OAuthError("invalid_grant", "refresh_token is invalid or expired")
            self._save_state()
        return data

    # ---------------------------------------------------------------- routes
    def routes(self) -> list[Route]:
        return [
            Route("/.well-known/oauth-authorization-server", self._handle_as_metadata, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", self._handle_resource_metadata, methods=["GET"]),
            Route("/register", self._handle_register, methods=["POST"]),
            Route("/authorize", self._handle_authorize, methods=["GET", "POST"]),
            Route("/token", self._handle_token, methods=["POST"]),
        ]

    async def _handle_as_metadata(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": self._public_url,
                "authorization_endpoint": f"{self._public_url}/authorize",
                "token_endpoint": f"{self._public_url}/token",
                "registration_endpoint": f"{self._public_url}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [DEFAULT_SCOPE],
            }
        )

    async def _handle_resource_metadata(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "resource": f"{self._public_url}/mcp",
                "authorization_servers": [self._public_url],
                "scopes_supported": [DEFAULT_SCOPE],
                "bearer_methods_supported": ["header"],
            }
        )

    async def _handle_register(self, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _oauth_error_response("invalid_request", "body must be JSON", 400)
        redirect_uris = body.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _oauth_error_response("invalid_redirect_uri", "redirect_uris[] is required", 400)
        for uri in redirect_uris:
            if not isinstance(uri, str) or not (uri.startswith("https://") or uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1")):
                return _oauth_error_response("invalid_redirect_uri", f"redirect_uri rejected: {uri}", 400)

        client_id = secrets.token_urlsafe(16)
        client_name = body.get("client_name") or "mcp-client"
        record = _RegisteredClient(
            client_id=client_id,
            redirect_uris=list(redirect_uris),
            client_name=str(client_name)[:80],
            created_at=int(time.time()),
        )
        with self._lock:
            self._clients[client_id] = record
            self._save_state()
        logger.info("Registered OAuth client %s (%s)", client_name, client_id)
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": record.created_at,
                "redirect_uris": record.redirect_uris,
                "client_name": record.client_name,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
            status_code=201,
        )

    async def _handle_authorize(self, request: Request) -> Response:
        if request.method == "GET":
            params = dict(request.query_params)
            err = self._validate_authorize_params(params)
            if err:
                return _oauth_error_response(*err, 400)
            return HTMLResponse(_render_login(params))

        # POST
        form = await request.form()
        params = {k: form.get(k, "") for k in (
            "client_id", "redirect_uri", "code_challenge", "code_challenge_method",
            "state", "scope", "response_type",
        )}
        err = self._validate_authorize_params(params)
        if err:
            return _oauth_error_response(*err, 400)

        if not self._check_rate_limit():
            return HTMLResponse(_render_login(params, error="Too many attempts. Wait a minute and try again."), status_code=429)

        password = form.get("password", "")
        if not isinstance(password, str) or not hmac.compare_digest(password, self._password):
            return HTMLResponse(_render_login(params, error="Wrong password."), status_code=401)

        code = secrets.token_urlsafe(32)
        record = _AuthCode(
            client_id=params["client_id"],
            redirect_uri=params["redirect_uri"],
            code_challenge=params["code_challenge"],
            code_challenge_method=params["code_challenge_method"],
            scope=params.get("scope") or DEFAULT_SCOPE,
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
        )
        with self._lock:
            self._purge_codes()
            self._auth_codes[code] = record

        redirect_params = {"code": code}
        if params.get("state"):
            redirect_params["state"] = params["state"]
        sep = "&" if "?" in params["redirect_uri"] else "?"
        return RedirectResponse(
            f"{params['redirect_uri']}{sep}{urlencode(redirect_params)}",
            status_code=303,
        )

    def _validate_authorize_params(self, params: dict[str, str]) -> Optional[tuple[str, str]]:
        if params.get("response_type") != "code":
            return ("unsupported_response_type", "response_type must be 'code'")
        client_id = params.get("client_id") or ""
        client = self._clients.get(client_id)
        if client is None:
            return ("invalid_client", "unknown client_id; register the client first")
        redirect_uri = params.get("redirect_uri") or ""
        if redirect_uri not in client.redirect_uris:
            return ("invalid_redirect_uri", "redirect_uri not registered for this client")
        if params.get("code_challenge_method") != "S256":
            return ("invalid_request", "code_challenge_method must be S256 (PKCE required)")
        if not params.get("code_challenge"):
            return ("invalid_request", "code_challenge is required (PKCE)")
        return None

    async def _handle_token(self, request: Request) -> Response:
        form = await request.form()
        grant_type = form.get("grant_type", "")
        try:
            if grant_type == "authorization_code":
                return self._token_authorization_code(form)
            if grant_type == "refresh_token":
                return self._token_refresh_token(form)
        except OAuthError as exc:
            return _oauth_error_response(exc.error, exc.description, 400)
        return _oauth_error_response("unsupported_grant_type", f"grant_type={grant_type!r}", 400)

    def _token_authorization_code(self, form: Any) -> JSONResponse:
        code = form.get("code", "")
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        code_verifier = form.get("code_verifier", "")
        if not all((code, client_id, redirect_uri, code_verifier)):
            raise OAuthError("invalid_request", "code, client_id, redirect_uri and code_verifier are required")
        with self._lock:
            self._purge_codes()
            record = self._auth_codes.get(code)
            if record is None or record.used or record.expires_at <= time.time():
                raise OAuthError("invalid_grant", "code is invalid or expired")
            if record.client_id != client_id or record.redirect_uri != redirect_uri:
                raise OAuthError("invalid_grant", "code does not match client_id/redirect_uri")
            expected = _pkce_s256(code_verifier)
            if not hmac.compare_digest(expected, record.code_challenge):
                raise OAuthError("invalid_grant", "PKCE verifier mismatch")
            record.used = True
        access = self._mint_access_token(client_id, record.scope)
        refresh = self._mint_refresh_token(client_id, record.scope)
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "refresh_token": refresh,
                "scope": record.scope,
            }
        )

    def _token_refresh_token(self, form: Any) -> JSONResponse:
        token = form.get("refresh_token", "")
        client_id = form.get("client_id", "")
        if not token or not client_id:
            raise OAuthError("invalid_request", "refresh_token and client_id are required")
        record = self._consume_refresh_token(token)
        if record.client_id != client_id:
            raise OAuthError("invalid_grant", "client_id does not match this refresh_token")
        access = self._mint_access_token(client_id, record.scope)
        refresh = self._mint_refresh_token(client_id, record.scope)
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                "refresh_token": refresh,
                "scope": record.scope,
            }
        )


class OAuthError(Exception):
    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _oauth_error_response(error: str, description: str, status: int) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


_LOGIN_FORM = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sign in to Personal OneDrive MCP</title>
<style>
body { font-family: system-ui, sans-serif; background: #111; color: #eee;
  display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
.card { background: #1c1c1c; padding: 2.5rem; border-radius: 12px; max-width: 420px; width: 100%;
  box-shadow: 0 10px 40px rgba(0,0,0,0.4); }
h1 { margin-top: 0; font-size: 1.3rem; }
p { color: #aaa; font-size: 0.9rem; line-height: 1.45; }
input[type=password] { width: 100%; padding: 0.7rem; margin-top: 0.5rem;
  background: #2a2a2a; color: #eee; border: 1px solid #444; border-radius: 6px;
  font-size: 1rem; box-sizing: border-box; }
button { width: 100%; padding: 0.7rem; margin-top: 1rem; background: #4a90e2; color: #fff;
  border: 0; border-radius: 6px; font-size: 1rem; cursor: pointer; }
.error { color: #ff8080; margin-top: 0.7rem; font-size: 0.9rem; }
.client { color: #888; font-size: 0.8rem; margin-bottom: 1rem; }
</style>
</head>
<body>
<div class="card">
  <h1>Personal OneDrive MCP</h1>
  <p class="client">Client requesting access: <code>__CLIENT_NAME__</code></p>
  <p>Enter the gateway password from your <code>.env</code> to grant this client access to your personal OneDrive.</p>
  <form method="post" action="/authorize">
    __HIDDEN__
    <input type="password" name="password" autofocus required placeholder="Gateway password">
    <button type="submit">Authorize</button>
    __ERROR__
  </form>
</div>
</body>
</html>
"""


def _render_login(params: dict[str, str], *, error: Optional[str] = None) -> str:
    hidden_keys = ("response_type", "client_id", "redirect_uri", "code_challenge",
                   "code_challenge_method", "scope", "state")
    hidden = "\n    ".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(params.get(k, ""))}">'
        for k in hidden_keys
    )
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    client_name = html.escape(params.get("client_id", "(unknown)"))
    return _LOGIN_FORM.replace("__CLIENT_NAME__", client_name).replace("__HIDDEN__", hidden).replace("__ERROR__", err)


# ---------------------------------------------------------------- middleware
class BearerAuthMiddleware:
    """ASGI middleware: requires a valid Bearer JWT on /mcp* routes; emits the
    RFC 9728 ``WWW-Authenticate`` challenge on 401 so clients can discover the
    authorization server."""

    def __init__(self, app: Any, gateway: OAuthGateway, protected_prefix: str = "/mcp") -> None:
        self.app = app
        self.gateway = gateway
        self.protected_prefix = protected_prefix

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith(self.protected_prefix):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

        if not token:
            await _send_401(send, self.gateway.metadata_url, "missing bearer token")
            return
        try:
            self.gateway.verify_access_token(token)
        except jwt.ExpiredSignatureError:
            await _send_401(send, self.gateway.metadata_url, "token expired")
            return
        except jwt.InvalidTokenError as exc:
            logger.info("Rejected bearer token: %s", exc)
            await _send_401(send, self.gateway.metadata_url, "invalid token")
            return

        await self.app(scope, receive, send)


async def _send_401(send: Any, metadata_url: str, reason: str) -> None:
    challenge = (
        f'Bearer realm="MCP", '
        f'resource_metadata="{metadata_url}", '
        f'error="invalid_token", '
        f'error_description="{reason}"'
    )
    body = json.dumps({"error": "invalid_token", "error_description": reason}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", challenge.encode("latin-1")),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})
