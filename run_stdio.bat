@echo off
REM Launch the MCP server over stdio (for Claude Desktop / Claude Code).
REM Claude Desktop config will invoke python.exe directly with these args;
REM this .bat is mostly for manual testing.
"%~dp0.venv\Scripts\python.exe" -m onedrive_mcp --stdio
