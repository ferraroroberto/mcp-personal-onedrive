# Project instructions — `mcp-personal-onedrive`

Canonical instructions for AI coding agents working in this repository. Claude Code reads this file directly as project memory. Other agents (Cursor, Codex, etc.) reach it via the one-line `AGENTS.md` pointer.

This repo is a **standalone Python project** — it intentionally does not share a `.venv` or `requirements.txt` with the parent `automation` monorepo.

## What this project is
A minimal, read-only **Model Context Protocol (MCP) server** that lets Claude (Desktop, Code, web, mobile) browse and read files in a **personal Microsoft account's OneDrive**. The official Microsoft 365 connector for Claude only supports work/school tenants — this server fills the gap for consumer (`outlook.com`, `hotmail.com`, `live.com`) accounts.

It can run two ways:
- **stdio** — for Claude Desktop and Claude Code on the same machine. Simplest.
- **Streamable HTTP behind a single-user OAuth 2.1 gateway** — for claude.ai web and mobile, exposed via Tailscale Funnel.

The actual setup is described in [`README.md`](README.md) → "Configuration and testing".

## Plan mode is the default
Every non-trivial request starts in plan mode. Non-trivial = anything beyond a one-line fix, a typo, or a question I can answer without touching code.

In plan mode:
- Do NOT edit files, run destructive commands, or commit anything.
- Investigate the codebase as needed (read files, search, run read-only commands).
- Resolve ambiguity through questions before proposing a plan.
- Present the plan only when you're confident it reflects what I actually want.
- Stay in plan mode across rejections — if I push back, revise and re-present.

Recommended setting in `.claude/settings.json`:
```json
{ "permissions": { "defaultMode": "plan" } }
```

Exit plan mode only after I explicitly approve.

## Asking questions
Ask whenever a decision would be expensive to undo or genuinely ambiguous. One sharp question beats three filler ones. Use multi-choice (2–4 options) when the choice space is bounded.

**Always ask before assuming** any of these:
- Whether to widen scopes (this server is read-only by design).
- Where new tools should live (graph_client.py vs server.py vs a new module).
- Anything that touches the OAuth gateway — the security-critical core.
- Whether to add tests, and at what level.

**Don't ask about** things you can determine by reading the code, things I've already specified, or process meta-questions like "is the plan ready?".

## Before editing
- Re-read any file before modifying it.
- For files >500 LOC, read in chunks; don't assume you've seen the whole file.
- When renaming a symbol, search separately for: direct calls, type references, string literals, docs in `README.md`, and config keys in `.env.sample`.

## Conventions
- **Layout:** `src/onedrive_mcp/` is the only package. No mirrored top-level package.
- **Config & secrets:** `.env` at repo root, never committed. Schema in `.env.sample`. `.token_cache.bin` and `.oauth_state.json` are runtime files — gitignored.
- **Logging:** Python `logging`. Never log access tokens, refresh tokens, JWTs, or the gateway password. Logs go to **stderr** — stdout is reserved for MCP stdio JSON-RPC traffic.
- **Naming:** snake_case for files/functions, PascalCase for classes, UPPER_CASE for constants.
- **Imports:** stdlib → third-party → local.
- **Versioning:** `requirements.txt` uses lower bounds (`>=`); keep that style.
- **Virtual environment:** the local `.venv` only. Never create `venv`. Invoke directly with `& .\.venv\Scripts\python.exe ...`.
- **No hardcoded paths or credentials.**
- **Type hints** on all public functions. Use `Optional[T]`, never bare `None` returns.
- Implement only what was asked. No nice-to-haves.

## Read-only scope
This server intentionally does NOT support upload, rename, delete, share-link creation, or any other write operation. Microsoft Graph scopes are pinned to `Files.Read User.Read offline_access`. Adding write capability requires:
1. A separate scope set (`Files.ReadWrite`).
2. A separate launcher / mode (`--http-write`?), so the read-only deployment isn't silently widened.
3. Explicit user approval — do not infer this from the brief.

If a request implies a write operation, **stop and ask**.

## Security expectations
- The OAuth gateway is the only thing standing between the public internet and the user's OneDrive when run via Funnel. Treat any change in `auth_oauth.py` with extra care.
- PKCE (S256) is mandatory; do not weaken it.
- Bearer middleware MUST emit RFC 9728 `WWW-Authenticate: Bearer resource_metadata=...` on 401.
- JWT signing key lives in `.oauth_state.json` (HS256). If you ever need to rotate it, document the user-facing impact (all connectors must re-auth).
- Rate-limit failed login attempts. Don't add log lines that print the password or the JWT.

## Phased execution
Multi-file refactors don't go in a single response. Break into phases of ≤5 files each. Complete phase 1, run `py_compile`, wait for approval, then phase 2.

## Verification (before declaring a task done)
Windows / PowerShell:
- Syntax: `& .\.venv\Scripts\python.exe -m py_compile <files>`
- Imports: `& .\.venv\Scripts\python.exe -c "import onedrive_mcp.server, onedrive_mcp.auth_oauth, onedrive_mcp.graph_client"`
- HTTP smoke (no MS account needed): start `--http`, curl `/.well-known/oauth-authorization-server`, curl `/mcp` (expect 401), curl `/.well-known/oauth-protected-resource`.

If you change graph_client behavior, also run `--self-test` and verify the root listing still works (requires the user's signed-in cache).

## Documentation discipline
For feature work and refactors:
- Update `README.md` if usage, config, or transport changed.
- This repo does not maintain a `docs/` folder for changelogs — the README is the single source of truth for the user-facing setup. Don't create `docs/` for one-off notes.

For one-line fixes and typos: skip the README update.

## Git
Never auto-commit or push. Never stage files without being asked. When a task is done, ask: "Shall I prepare the commit message?" When asked, provide a ready-to-copy block; the user runs it.

## Senior-dev check
Before finishing, ask: "What would a senior, perfectionist dev reject in review?" If the answer points at duplicated state, inconsistent patterns, or broken architecture *within the file you're already editing*, fix it. Don't expand scope to unrelated files.
