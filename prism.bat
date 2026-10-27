@echo off
chcp 936 >nul
cd /d "%~dp0"
title Prism - 棱镜

REM ---- 使用 Prism 自带的独立环境（不依赖外部 Python / 系统 numpy）----
set "PY=%~dp0.venv\Scripts\python.exe"

REM ---- 清掉外部继承来的 Python 路径注入（否则会去加载别人的 numpy，版本不匹配必崩）----
set "PYTHONPATH="
set "PYTHONHOME="

REM ---- timeout 用绝对路径：某些环境的 PATH 里有同名命令（如 Git Bash 的 timeout）会把它抢走 ----
set "SLEEP=%SystemRoot%\System32\timeout.exe"

if exist "%PY%" goto have_venv
echo.
echo   没找到 Prism 的独立环境:
echo   %PY%
echo   请先双击运行 安装.bat
echo.
echo   窗口将在 30 秒后自动关闭
"%SLEEP%" /t 30 >nul
exit /b 1
:have_venv

REM ---- 已经在运行? 那就别再开服务, 也别再开浏览器标签 ----
netstat -ano | findstr ":8848" | findstr "LISTENING" >nul
if errorlevel 1 goto launch
echo.
echo   Prism 已经在运行 —— 不再开第二个服务，只打开一个窗口接上去。
echo   以前这里会直接退出，你看到的是一个浏览器标签，在那里拖拽当然没反应。
echo.

:launch
echo  正在启动 Prism...
echo.
echo  窗口会自动打开。若已有窗口/标签，切回去刷新即可
echo.

REM ---- 用桌面窗口启动（原生窗口能拿到拖入文件的绝对路径 → 拖进来就能入库）----
REM 没装 pywebview 会自动退回「开系统浏览器」，行为与以前一致
"%PY%" "%~dp0server\desktop.py"

echo.
echo   Prism 已停止. 窗口将在 15 秒后自动关闭, 按任意键可立即关闭
"%SLEEP%" /t 15
exit /b 0
