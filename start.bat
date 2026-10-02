@echo off
REM ---------------------------------------------------------------------------
REM Local one-shot startup: Storyline first (:8001, the editing-node MCP
REM service), then the main service (:8000).
REM
REM BOTH SERVICES RUN WITHOUT A CONSOLE WINDOW (pythonw.exe) AND LOG TO FILES.
REM   Earlier versions opened two cmd windows per launch and left them on the
REM   desktop; with a few restarts the desktop filled up. pythonw.exe is the
REM   windowless interpreter, so launching these starts no visible window at all.
REM   To watch the logs instead:
REM       type .storyline\storyline.log
REM       type .main_server.log
REM   (findstr /v "^$" if you want blank lines filtered out)
REM
REM NOTE 1 - comments in this file are ASCII ONLY, on purpose.
REM   This file is saved as UTF-8 without BOM, but cmd.exe reads .bat files
REM   using the system OEM codepage (936/GBK on this machine). Chinese text in
REM   a REM line therefore turns into mojibake and cmd tries to EXECUTE it:
REM       'REM ...' is not recognized as an internal or external command
REM   Those errors are harmless but they bury the real startup log.
REM
REM NOTE 2 - do NOT add --no-resume.
REM   With --no-resume the main service marks every running/failed run in the
REM   DB as failed on startup, i.e. it disables crash recovery: one restart and
REM   any interrupted edit is gone for good. Leaving it off lets the startup
REM   reconciliation claim those runs and resume them.
REM
REM NOTE 3 - pythonw.exe, not python.exe.
REM   python.exe attaches to a console; started from a .bat that means a window.
REM   pythonw.exe has no console at all, so the service runs invisibly and
REM   writes only to the log files. If a startup fails, the log says why
REM   (run_server.py validates storage and exits non-zero on failure).
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo Starting Storyline (:8001) ...
start "" /b pythonw.exe run_storyline.py >> .storyline\storyline.log 2>&1
REM Give it a moment to bind :8001 before the main service connects to it.
REM The main service also probes and degrades gracefully, so this is a nicety.
timeout /t 3 /nobreak >nul

echo Starting main service (:8000) ...
start "" /b pythonw.exe run_server.py >> .main_server.log 2>&1
REM Give the main service a moment so the probe below reports a real answer.
timeout /t 6 /nobreak >nul

echo.
echo Checking health ...
powershell -NoProfile -Command "try { $r = Invoke-RestMethod http://127.0.0.1:8000/health -TimeoutSec 8; Write-Host ('  main service :8000 -> ' + $r.status) } catch { Write-Host '  main service :8000 -> NOT RESPONDING (see .main_server.log)' }"

echo.
echo Both services were started in the background (no console window).
echo   Storyline   :8001   log: .storyline\storyline.log
echo   Main        :8000   log: .main_server.log
echo To stop them:  taskkill /F /IM pythonw.exe
echo This window can be closed; the services keep running.
endlocal
