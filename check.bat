@echo off
REM 全量体检（T0.2）。提交前那一步只跑 `selfcheck.py --fast`（见 tools/hooks/pre-commit）。
REM
REM 为什么单有一个 .bat：这个项目的开发机是 Windows，而 `make check` 那套在 Git Bash 里
REM 能跑、在 cmd/PowerShell 里不能。两个入口调的是同一条命令，不各写一套判据。
setlocal
cd /d "%~dp0"

set PY=%SELFCHECK_PYTHON%
if "%PY%"=="" set PY=python

echo [check] 全量体检：%PY% selfcheck.py
%PY% selfcheck.py
if errorlevel 1 goto :red

echo.
echo [check] 像素探针（容器不在会记 UNVERIFIED，不算红）
%PY% selfcheck.py --probe
if errorlevel 1 goto :red

echo.
echo [check] 全绿。
exit /b 0

:red
echo.
echo [check] 有红，见上面的清单。
exit /b 1
