@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Prism - 棱镜

REM ---- 使用 Prism 自带的独立环境（不依赖外部 Python / 系统 numpy）----
set "PY=%~dp0.venv\Scripts\python.exe"

REM ---- 清掉外部继承来的 Python 路径注入（否则会去加载别人的 numpy，版本不匹配必崩）----
set "PYTHONPATH="
set "PYTHONHOME="

REM ---- timeout 用绝对路径：某些环境的 PATH 里有同名命令（如 Git Bash 的 timeout）会把它抢走 ----
set "SLEEP=%SystemRoot%\System32\timeout.exe"

if not exist "%PY%" (
  echo.
  echo   没找到 Prism 的独立环境:
  echo   %PY%
  echo   请先双击运行 安装.bat
  echo.
  echo   窗口将在 30 秒后自动关闭
  "%SLEEP%" /t 30 >nul
  exit /b 1
)

REM ---- 已经在运行? 那就别再开服务, 也别再开浏览器标签 ----
netstat -ano | findstr ":8848" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo.
  echo   Prism 已经在运行, 界面地址:
  echo.
  echo       http://127.0.0.1:8848
  echo.
  echo   为避免浏览器标签堆积, 这里不重复打开浏览器.
  echo   要看界面: 切回原来那个标签, 或双击 打开界面.bat
  echo.
  "%SLEEP%" /t 6 >nul
  exit /b 0
)

echo.
echo   正在启动 Prism...
echo   3 秒后会打开浏览器. 若已有界面标签, 切回去刷新即可
echo.

REM 3 秒后打开浏览器（给服务一点启动时间）
start "" /min cmd /c "%SLEEP% /t 3 >nul & start http://127.0.0.1:8848"

"%PY%" "%~dp0server\app.py"

echo.
echo   Prism 已停止. 窗口将在 15 秒后自动关闭, 按任意键可立即关闭
"%SLEEP%" /t 15
exit /b 0
