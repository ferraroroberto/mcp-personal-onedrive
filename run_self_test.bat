@echo off
REM One-time interactive sign-in to Microsoft. Run this once on the host PC
REM after the Azure App Registration is in place; thereafter the token cache
REM in .token_cache.bin is reused silently.
"%~dp0.venv\Scripts\python.exe" -m onedrive_mcp --self-test
