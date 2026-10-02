@echo off
REM ---------------------------------------------------------------------------
REM Local one-shot startup: Storyline first (:8001, the editing-node MCP
REM service), then the main service (:8000).
REM
REM NOTE 1 - comments in this file are ASCII ONLY, on purpose.
REM   This file is saved as UTF-8 without BOM, but cmd.exe reads .bat files
REM   using the system OEM codepage (936/GBK on this machine). Chinese text in
REM   a REM line therefore turns into mojibake and cmd tries to EXECUTE it:
REM       'REM ...' is not recognized as an internal or external command
REM   Those errors are harmless (the script still finishes) but they bury the
REM   real startup log. Keep every comment here in ASCII; the Chinese
REM   explanations live in README / docs instead.
REM
REM NOTE 2 - do NOT add --no-resume.
REM   With --no-resume the main service marks every running/failed run in the
REM   DB as failed on startup, i.e. it disables crash recovery: one restart and
REM   any interrupted edit is gone for good. Leaving it off lets the startup
REM   reconciliation claim those runs and resume them.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo Starting Storyline (:8001) ...
REM Use the in-package launcher: "python -m storyline_server.server" cannot
REM resolve relative imports and fails to start (the "No module named
REM storyline_server.__main__" line in storyline_err.log comes from that form).
start "Storyline" cmd /c "python run_storyline.py >> .storyline\storyline.log 2>&1"
timeout /t 3 /nobreak >nul

echo Starting main service (:8000) ...
python run_server.py

endlocal
