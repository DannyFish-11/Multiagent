@echo off
rem ============================================================
rem  memory-agent 一键运行(Windows)—— 双击本文件即可。
rem  首次会自动装好运行环境(uv),然后起服务并打开浏览器聊天页。
rem  零配置即 demo 档(零 key / 零 GPU);想用真实大模型请先放一个 .env。
rem ============================================================
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo [1/2] 正在安装运行环境 uv ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo [2/2] 启动 memory-agent（首次会自动装依赖，稍候）...
uv run python -m core.launch %*

echo.
echo 服务已停止。按任意键关闭本窗口。
pause >nul
