@echo off
REM Launch the MCP server over Streamable HTTP behind the OAuth gateway.
REM Uvicorn binds to 127.0.0.1 by default; Tailscale Funnel forwards from
REM the public *.ts.net hostname. See README "Configuration and testing".
"%~dp0.venv\Scripts\python.exe" -m onedrive_mcp --http
