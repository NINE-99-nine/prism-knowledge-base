@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Prism - 安装环境
setlocal enabledelayedexpansion

echo.
echo   ============================================================
echo    Prism 棱镜 - 环境安装
echo   ============================================================
echo.
echo   这一步会在 prism 文件夹内创建一个独立 Python 环境
echo   (.venv)，并安装以下依赖：
echo.
echo     numpy      向量运算
echo     pywebview  原生桌面窗口（有了它才能把文件拖进窗口直接入库）
echo.
echo   所有东西都留在 prism 文件夹里，不往别处写。
echo.
pause

set "PY=%~dp0.venv\Scripts\python.exe"
set "BASE="
set "BASEARG="

REM ---- 找一个可用的基础 Python（用来创建环境）----
REM ---- 顺序：PRISM_PYTHON → py 启动器 → PATH 里的 python ----
if defined PRISM_PYTHON if exist "%PRISM_PYTHON%" set "BASE=%PRISM_PYTHON%"

REM py 启动器：直接把它当解释器用，绝不捕获它的输出。
REM 捕获要经过管道和控制台代码页，路径里一旦出现非 ASCII 字符（比如全角括号），
REM 拿回来的路径会被弄脏，导致把一个本来可用的 Python 误判成不可用。
if not defined BASE (
  py -3 -c "import sys,venv,ensurepip;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "BASE=py"
    set "BASEARG=-3"
  )
)

if not defined BASE (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined BASE call :accept "%%i"
  )
)

if not defined BASE (
  for /f "delims=" %%i in ('where python3 2^>nul') do (
    if not defined BASE call :accept "%%i"
  )
)

if not defined BASE (
  echo.
  echo   [错误] 没有找到可用的 Python。
  echo.
  echo   本脚本要求 Python 3.10 以上，且必须带 venv / ensurepip 模块。
  echo.
  echo   提示：如果敲 python 会弹出「应用商店」，那只是商店的占位程序，
  echo         不是 Python 本体；conda / miniconda 自带的 Python 也常常
  echo         缺少 venv 模块 —— 这两种都会被自动跳过。
  echo.
  echo   请到 https://www.python.org/downloads/ 下载安装 Python 3.10 以上版本，
  echo   安装时务必勾选 "Add python.exe to PATH"，
  echo   装完重新打开一个窗口再运行本脚本。
  echo.
  pause >nul
  exit /b 1
)

echo.
echo   使用基础 Python: %BASE% %BASEARG%
echo.

if not exist "%PY%" (
  echo   [1/2] 创建独立环境 .venv ...
  "%BASE%" %BASEARG% -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo   [失败] 创建环境出错（详细报错见上一行）。
    echo          如果出现 "No module named venv"，说明这个 Python 用不了 venv，
    echo          请改用官方 Python：https://www.python.org/downloads/
    pause >nul
    exit /b 1
  )
) else (
  echo   [1/2] 独立环境已存在，跳过创建。
)

echo.
echo   [2/2] 安装依赖 ...
set PYTHONPATH=
set PYTHONHOME=

REM ---- 选下载源：官方源响应太慢就自动改用国内镜像 ----
set "PIPIDX=-i https://pypi.org/simple"
set "PIPALT=-i https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIPNAME=PyPI 官方源"
set "ALTNAME=清华镜像"
set "PIPMS="
for /f "delims=" %%t in ('powershell -NoProfile -Command "[int](Measure-Command { try { Invoke-WebRequest https://pypi.org/simple/ -UseBasicParsing -Method Head -TimeoutSec 6 | Out-Null } catch {} }).TotalMilliseconds" 2^>nul') do set "PIPMS=%%t"
if defined PIPMS if !PIPMS! GTR 3000 (
  set "PIPIDX=-i https://pypi.tuna.tsinghua.edu.cn/simple"
  set "PIPALT=-i https://pypi.org/simple"
  set "PIPNAME=清华镜像"
  set "ALTNAME=PyPI 官方源"
)
if defined PIPMS (
  echo   下载源: !PIPNAME!（官方源响应 !PIPMS! ms）
) else (
  echo   下载源: !PIPNAME!（探测不到，按默认）
)

echo.
echo   ---- pip ----
"%PY%" -m pip install --upgrade pip !PIPIDX!

echo.
echo   ---- numpy ----
"%PY%" -m pip install numpy !PIPIDX!
if errorlevel 1 (
  echo   [提示] 失败，改用 !ALTNAME! 重试 ...
  "%PY%" -m pip install numpy !PIPALT!
)

echo.
echo   ---- pywebview ----
"%PY%" -m pip install pywebview !PIPIDX!
if errorlevel 1 (
  echo   [提示] 失败，改用 !ALTNAME! 重试 ...
  "%PY%" -m pip install pywebview !PIPALT!
)

echo.
echo   ============================================================
echo    验证
echo   ============================================================
set "OK=1"
"%PY%" -c "import sys,sqlite3,numpy; print('   Python   :', sys.version.split()[0]); print('   numpy    :', numpy.__version__); print('   sqlite3  :', sqlite3.sqlite_version)" || set "OK=0"
"%PY%" -c "import webview; print('   pywebview: 已安装')" || set "OK=0"

echo.
if "%OK%"=="0" (
  echo   [警告] 有依赖没装上。请先确认网络，再重新运行本脚本。
  echo.
  pause >nul
  exit /b 1
)

echo   环境就绪，现在可以双击 prism.bat 启动了。
echo.
echo   窗口将在 30 秒后自动关闭
"%SystemRoot%\System32\timeout.exe" /t 30 >nul
exit /b 0

REM ============================================================
REM  子程序：校验候选 Python
REM  1) 跳过微软应用商店的占位程序（0 字节转发壳，跑不了 venv）
REM  2) 要求版本 >= 3.10 且带 venv / ensurepip —— conda 系常常缺 venv，
REM     不在这里拦掉，就要等到建环境那一步才失败
REM  3) 每跳过一个都打印原因，方便排查
REM ============================================================
:accept
set "CAND=%~1"
if not "!CAND:\WindowsApps\=!"=="!CAND!" (echo   跳过（应用商店占位）：!CAND! & exit /b)
"%~1" -c "import sys,venv,ensurepip;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul || (echo   跳过（不可用：版本低于 3.10 或缺少 venv 模块）：!CAND! & exit /b)
set "BASE=%~1"
exit /b
