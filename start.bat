@echo off
REM 本地一键启动：先 Storyline（:8001，剪辑节点 MCP 服务），再主服务（:8000）。
REM
REM 注意 --no-resume：
REM   带上它，主服务启动时会把库里所有 running/failed 的 run **直接标成 failed**，
REM   等于关掉崩溃恢复——重启一次，被打断的剪辑就彻底接不回来了。
REM   这里刻意不加，让启动对账正常认领并续跑未完成的 run。
REM   只有临时起一个「干净实例」做隔离实验时才需要临时加上。
setlocal
cd /d "%~dp0"

echo 启动剪辑服务 (:8001)...
REM 用包内启动器：直接 python -m storyline_server.server 会因为相对导入
REM 拿不到包而上不了（storyline_err.log 里那句 "No module named
REM storyline_server.__main__" 就是这类写法留下的）。
start "Storyline" cmd /c "python run_storyline.py >> .storyline\storyline.log 2>&1"
timeout /t 3 /nobreak >nul

echo 启动主服务 (:8000)...
python run_server.py

endlocal
