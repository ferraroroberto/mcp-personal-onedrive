# Personal OneDrive MCP

A minimal, **read-only** [Model Context Protocol](https://modelcontextprotocol.io/) server that lets Claude (Desktop, Code, web, mobile) browse and read files in a **personal Microsoft account's OneDrive** (`outlook.com`, `hotmail.com`, `live.com`).

The official Microsoft 365 connector for Claude only supports work/school tenants. This server exists to fill that gap for consumer accounts.

## What you get

Five read-only tools:

| Tool | What it does |
|---|---|
| `list_root` | List items at the root of your OneDrive |
| `list_folder` | List a folder by `path` or `item_id`, with pagination |
| `search_files` | Search across the whole drive |
| `get_item_metadata` | Full metadata for one item |
| `download_file_content` | Inline UTF-8 text for text-like files; base64 for binaries; size-capped |

Two transports:

- **stdio** — for Claude Desktop and Claude Code on the same machine. No public exposure, no OAuth.
- **Streamable HTTP behind a single-user OAuth 2.1 gateway** — for **claude.ai web and the Claude mobile app**. Exposed publicly via Tailscale Funnel.

## Requirements

- Windows 10/11 (instructions are PowerShell-flavoured)
- Python 3.10+
- A personal Microsoft account
- An Azure App Registration (free)
- For the mobile/web path: Tailscale (free) with Funnel enabled

## What it intentionally doesn't do

- No upload, rename, delete, or share-link creation. Read-only by design.
- No work/school account support — the authority is pinned to `/consumers`. Use the official Microsoft 365 connector for those.
- No support for items in OneDrive **Personal Vault** (they require step-up auth).
- No "Shared with me" listing — only your own drive.

## Project layout

```
mcp-personal-onedrive/
├── src/onedrive_mcp/
│   ├── __main__.py        # `python -m onedrive_mcp [--stdio|--http|--self-test]`
│   ├── server.py          # FastMCP app + tool registrations + transport wiring
│   ├── auth_graph.py      # MSAL /consumers flow + token cache
│   ├── auth_oauth.py      # Single-user OAuth 2.1 AS (gateway in front of /mcp)
│   ├── graph_client.py    # Microsoft Graph REST wrapper
│   └── config.py          # .env loader
├── run_stdio.bat
├── run_http.bat
├── run_self_test.bat
├── requirements.txt
├── pyproject.toml
└── .env.sample
```

---

# Configuration and testing

This is the step-by-step you (or Claude in a future session) will follow to go from an empty checkout to a working connector on your phone. Each phase is self-contained — you can stop at the end of any phase and still have something useful.

> Throughout, `<repo>` means `E:\automation\mcp-personal-onedrive` (or wherever you cloned this).

## Phase 0 — Install dependencies

```powershell
cd <repo>
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pip install -e .
```

The editable install is what makes `python -m onedrive_mcp` work.

Verify:
```powershell
& .\.venv\Scripts\python.exe -c "import onedrive_mcp; print(onedrive_mcp.__version__)"
# Expected: 0.1.0
```

---

## Phase 1 — Register the Azure app

This is the **app registration** Microsoft requires for any program that signs users into a personal MSA. It's free, takes ~5 minutes, and only has to be done once per server install.

### 1.1 Create the registration

1. Open <https://entra.microsoft.com> and sign in with the **personal Microsoft account** whose OneDrive you want to access.
2. Left sidebar → **Identity** → **Applications** → **App registrations** → **+ New registration**.
3. Fill in:
   - **Name:** `Claude Personal OneDrive MCP` (anything you like — only you see this)
   - **Supported account types:** **"Personal Microsoft accounts only"**
     > This is the critical setting. If you pick "any tenant" the `/consumers` authority will reject the token.
   - **Redirect URI:** leave blank for now — we'll add it in step 1.3.
4. Click **Register**.

### 1.2 Copy the client ID

On the app's **Overview** page, copy **Application (client) ID**. You'll paste it into `.env` in a moment.

### 1.3 Add redirect URI

Still on the app page → left sidebar → **Authentication** → **+ Add a platform** → **Mobile and desktop applications** → tick `http://localhost` → **Configure**.

Then back on the same Authentication page, scroll down to **Advanced settings** and confirm **"Allow public client flows"** is **Yes**. Save if you changed it.

> `http://localhost` (no port) is what MSAL uses for the desktop interactive sign-in (`--self-test`). It's a "public client / native" redirect.

### 1.4 Add API permissions

Left sidebar → **API permissions** → **+ Add a permission** → **Microsoft Graph** → **Delegated permissions** → tick:
- `Files.Read`
- `User.Read`
- `offline_access`

→ **Add permissions**. Personal MSA accounts grant consent at sign-in time, so you do **not** need to click "Grant admin consent".

### 1.5 Write the client ID to `.env`

```powershell
copy .env.sample .env
notepad .env
```

Set:
```env
ONEDRIVE_CLIENT_ID=<the GUID you copied in 1.2>
```

Leave the OAuth-gateway keys blank for now (we'll fill those in Phase 4).

---

## Phase 2 — One-time Microsoft sign-in (`--self-test`)

This is the only time MSAL will need to open a browser. The refresh token gets cached to `.token_cache.bin` so every subsequent run is silent.

```powershell
.\run_self_test.bat
```

What you should see:
1. A browser tab opens at `login.microsoftonline.com`.
2. Sign in with your personal Microsoft account, accept the permission prompt.
3. The browser tab closes itself.
4. The terminal prints something like:
   ```
   Acquiring Microsoft Graph token (browser will open if needed)...
   Sign-in OK. Listing OneDrive root...

   Found 7 item(s) at the root:
     DIR              0  Documents
     DIR              0  Pictures
     FILE       1234567  Some-file.pdf
     ...

   Token cache saved to E:\automation\mcp-personal-onedrive\.token_cache.bin
   You can now run the server in stdio or HTTP mode.
   ```

If you see the listing, the entire Microsoft side is working. If you get an `AADSTS` error, double-check Phase 1.1's "Personal Microsoft accounts only" setting and Phase 1.3's redirect URI.

---

## Phase 3 — Stdio mode (Claude Desktop / Claude Code)

Easiest path. Works only on this PC, but no public exposure and no extra moving parts.

### 3.1 Sanity-check the stdio binary

```powershell
& .\.venv\Scripts\python.exe -m onedrive_mcp --stdio
```

You should see nothing on stdout (the JSON-RPC channel) and a couple of MSAL log lines on stderr. Press Ctrl+C — that's a working stdio server, just nothing to talk to it yet.

### 3.2 Register with Claude Code (CLI)

```powershell
claude mcp add personal-onedrive `
  --scope user `
  -- "E:\automation\mcp-personal-onedrive\.venv\Scripts\python.exe" -m onedrive_mcp --stdio
```

Then in any Claude Code session:
```
/mcp
```
…should list `personal-onedrive` as connected.

### 3.3 Register with Claude Desktop (the Windows app)

Edit `%APPDATA%\Claude\claude_desktop_config.json` and add an entry inside `mcpServers`:

```json
{
  "mcpServers": {
    "personal-onedrive": {
      "command": "E:\\automation\\mcp-personal-onedrive\\.venv\\Scripts\\python.exe",
      "args": ["-m", "onedrive_mcp", "--stdio"],
      "env": {}
    }
  }
}
```

Restart Claude Desktop. The hammer icon at the bottom of a chat will list the OneDrive tools.

### 3.4 Try it

In Claude Desktop or Claude Code:
> List the contents of my OneDrive root, then read the first markdown file you find.

If that works, **Phase 3 is complete and stdio works**. Stop here if you only need this on the PC.

To go further (mobile / web), continue with Phase 4.

---

## Phase 4 — HTTP mode + OAuth gateway (for claude.ai web and mobile)

This is the part that lets your phone talk to your OneDrive. The flow is:

```
Claude mobile ──► claude.ai cloud ──► https://<host>.ts.net/mcp ──► your PC
                                       (Tailscale Funnel)
```

`claude.ai` does an OAuth 2.1 dance with the gateway built into this server before it's allowed to call any tool.

### 4.1 Pick a gateway password and write it to `.env`

This is what you'll type into the login form when claude.ai redirects you to your own server. Pick something long and unique — it's the only thing standing between the public internet and your OneDrive.

```env
MCP_OAUTH_PASSWORD=<a long random string of your choosing>
```

Leave `MCP_JWT_SECRET` blank — the server will generate a 64-char hex secret on first HTTP startup and persist it to `.oauth_state.json`.

### 4.2 Find your Tailscale MagicDNS hostname

```powershell
tailscale status --json | & .\.venv\Scripts\python.exe -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))"
```

Output looks like `your-pc.tail1234.ts.net`. Copy it.

> Prerequisites: MagicDNS and HTTPS Certificates must already be enabled on your tailnet. If you set up the `automation/launcher` previously, they already are. If not, follow [Tailscale's HTTPS docs](https://tailscale.com/kb/1153/enabling-https) — the steps are identical to the launcher README in the parent monorepo.

### 4.3 Set the public URL in `.env`

```env
MCP_PUBLIC_URL=https://your-pc.tail1234.ts.net
```

No trailing slash. No port — Funnel always uses 443 externally.

### 4.4 Enable Tailscale Funnel (one-time)

Funnel is the public-internet equivalent of `tailscale serve`. By default Tailscale is tailnet-only; Funnel makes a port reachable from anywhere.

1. **Allow your node to use Funnel** (admin policy):
   Open <https://login.tailscale.com/admin/acls> and ensure your tailnet's policy includes:
   ```jsonc
   {
     "nodeAttrs": [
       { "target": ["autogroup:member"], "attr": ["funnel"] }
     ]
   }
   ```
   (If you've never edited the ACL, the default tailnet policy already allows this for the owner.)

2. **Bring Funnel up against the local server port** (8080 by default):
   ```powershell
   tailscale funnel --bg 8080
   ```
   Output should show:
   ```
   Available on the internet:
   https://your-pc.tail1234.ts.net/  →  http://127.0.0.1:8080
   ```

3. **Verify the tunnel is up** from any machine off your tailnet (e.g. your phone on cellular):
   ```
   curl https://your-pc.tail1234.ts.net/.well-known/oauth-authorization-server
   ```
   You'll get connection refused for now — that's expected; the server isn't running yet. The next step starts it.

### 4.5 Start the HTTP server

```powershell
.\run_http.bat
```

You should see:
```
INFO uvicorn.error: Started server process
INFO uvicorn.error: Application startup complete.
INFO uvicorn.error: Uvicorn running on http://127.0.0.1:8080
```

Leave this terminal open. (For unattended use you'd wrap it in a service or the existing tray launcher; not in scope here.)

### 4.6 Smoke-test the gateway

In a second terminal:

```powershell
# Discovery — should return JSON with issuer, authorization_endpoint, token_endpoint
curl https://your-pc.tail1234.ts.net/.well-known/oauth-authorization-server

# Resource metadata — should point back at the AS
curl https://your-pc.tail1234.ts.net/.well-known/oauth-protected-resource

# MCP endpoint without a token — should return 401 with a WWW-Authenticate challenge
curl -i https://your-pc.tail1234.ts.net/mcp
```

The third request should respond with `HTTP/1.1 401 Unauthorized` and a header like:
```
WWW-Authenticate: Bearer realm="MCP", resource_metadata="https://your-pc.tail1234.ts.net/.well-known/oauth-protected-resource", error="invalid_token", ...
```

If you see all three, the gateway is doing its job.

### 4.7 Add as a Custom Connector on claude.ai

1. Open <https://claude.ai/settings/connectors> on a desktop browser (mobile works too but the form is fiddly).
2. **Add custom connector** → fill in:
   - **Name:** `Personal OneDrive`
   - **Description:** anything you like
   - **MCP Server URL:** `https://your-pc.tail1234.ts.net/mcp`
3. Click **Connect**. Claude.ai will:
   - Fetch your `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`.
   - POST to `/register` to dynamically register itself as a client.
   - Redirect you to `https://your-pc.tail1234.ts.net/authorize?...` — your gateway's login page (dark card on a black background).
   - You enter the **`MCP_OAUTH_PASSWORD`** you set in step 4.1.
   - You're redirected back to claude.ai with the connector marked as connected.
4. The connector should now show **5 tools available**.

### 4.8 Try it from your phone

Open the Claude mobile app, start a chat, attach the **Personal OneDrive** connector from the tools menu, and ask:

> List the files in my OneDrive root.

If that works end-to-end — congratulations, you have a phone-accessible personal OneDrive MCP.

---

## Operational notes

### Keep the server running
The HTTP server has to be up whenever you use the connector from your phone. Options:
- Run `run_http.bat` manually before you need it.
- Wrap in a Windows service via NSSM, Task Scheduler, or the parent `automation/launcher` tray pattern.

### Token rotation
- **Microsoft Graph refresh token:** valid for ~90 days of inactivity. If silent acquisition starts failing, re-run `run_self_test.bat`.
- **OAuth gateway refresh tokens** (issued to claude.ai): 30 days. Claude will re-auth automatically; you may be prompted for the gateway password again.
- **Gateway JWT secret:** rotating it (delete `.oauth_state.json`) invalidates every issued token, so claude.ai will re-auth.

### Logs
All logging goes to **stderr** (stdout is reserved for stdio JSON-RPC). Tokens, passwords, and JWT contents are never logged.

### Future hardening (intentionally not in v1)
- Wrap `.token_cache.bin` and `.oauth_state.json` with **Windows DPAPI** (`win32crypt.CryptProtectData`) so the files are unusable if copied off this PC.
- Replace the single-password gateway login with **passkeys** (WebAuthn).
- Add a separate `--http-write` mode behind `Files.ReadWrite` for upload/edit.
- Add **shared-with-me** listing as a sixth tool.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `--self-test` fails with `AADSTS50020` | App registration is not "Personal Microsoft accounts only" |
| `--self-test` fails with `AADSTS9002326` | Redirect URI in Phase 1.3 not configured as a public client |
| stdio works, HTTP fails to start | `MCP_OAUTH_PASSWORD` or `MCP_PUBLIC_URL` missing in `.env` |
| `/.well-known/...` reachable, `/mcp` returns 200 without auth | The bearer middleware isn't wrapping the right prefix — check `server.py` |
| claude.ai says "couldn't connect" | Funnel not up (run `tailscale funnel status`) or the server is bound to a port other than 8080 |
| Mobile app can see the connector but tools fail with 401 | The JWT secret was rotated since last sign-in; reconnect from `claude.ai/settings/connectors` |

## Why not use one of the existing servers on GitHub?
There are several Microsoft Graph MCP servers (`elyxlz/microsoft-mcp`, `microsoft/mcp`, `ftaricano/mcp-onedrive-sharepoint`, etc.). The closest to a personal-MSA-only, read-only target was `elyxlz/microsoft-mcp`, but it's a kitchen-sink server (Outlook, Calendar, Contacts, OneDrive read+write) and another repo's README claims a security audit found vulnerabilities in it. This server is intentionally narrower, read-only, and easier to audit (~1k LOC across six files).
