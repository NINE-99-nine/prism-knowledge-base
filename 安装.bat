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

REM ---- 找一个可用的基础 Python（用来创建环境）----
REM ---- 顺序：PRISM_PYTHON 环境变量 → py 启动器 → PATH 里的 python ----
if defined PRISM_PYTHON if exist "%PRISM_PYTHON%" set "BASE=%PRISM_PYTHON%"

if not defined BASE (
  for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
    if not defined BASE call :accept "%%i"
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
  echo   提示：如果敲 python 会弹出「应用商店」，那只是商店的占位程序，
  echo         不是 Python 本体；它跑不了 venv，本脚本会自动跳过它。
  echo.
  echo   另外：conda / miniconda 自带的 Python 常常没有 venv 模块，上面会自动跳过，
  echo         那种情况请另外装一个官方 Python。
  echo.
  echo   请到 https://www.python.org/downloads/ 下载安装 Python 3.10 以上版本，
  echo   安装时务必勾选 "Add python.exe to PATH"，
  echo   装完重新打开一个窗口再运行本脚本。
  echo.
  pause >nul
  exit /b 1
)

echo.
echo   使用基础 Python: %BASE%
echo.

if not exist "%PY%" (
  echo   [1/2] 创建独立环境 .venv ...
  "%BASE%" -m venv "%~dp0.venv"
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
"%PY%" -m pip install --upgrade pip -i https://pypi.org/simple

echo.
echo   ---- numpy ----
"%PY%" -m pip install numpy -i https://pypi.org/simple
if errorlevel 1 (
  echo   [提示] 官方源失败，改用清华源重试 ...
  "%PY%" -m pip install numpy -i https://pypi.tuna.tsinghua.edu.cn/simple
)

echo.
echo   ---- pywebview ----
"%PY%" -m pip install pywebview -i https://pypi.org/simple
if errorlevel 1 (
  echo   [提示] 官方源失败，改用清华源重试 ...
  "%PY%" -m pip install pywebview -i https://pypi.tuna.tsinghua.edu.cn/simple
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
REM  2) 跳过没有 venv / ensurepip 模块的 Python（conda 系常见），
REM     否则要等到建环境那一步才失败
REM  3) 每跳过一个都打印原因，方便排查
REM ============================================================
:accept
set "CAND=%~1"
if not "!CAND:\WindowsApps\=!"=="!CAND!" (echo   跳过（应用商店占位）：!CAND! & exit /b)
"%~1" -c "import venv, ensurepip" >nul 2>nul || (echo   跳过（没有 venv 模块，多为 conda）：!CAND! & exit /b)
set "BASE=%~1"
exit /b
