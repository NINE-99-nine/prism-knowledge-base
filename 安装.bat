@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Prism - 安装环境

echo.
echo   ============================================================
echo    Prism 棱镜 - 环境安装
echo   ============================================================
echo.
echo   这一步会在 prism 文件夹内创建一个独立 Python 环境
echo   (.venv)，并安装唯一的第三方依赖 numpy。
echo.
echo   所有东西都留在 prism 文件夹里，不往别处写。
echo.
pause

set "PY=%~dp0.venv\Scripts\python.exe"
set "BASE="

REM ---- 找一个可用的基础 Python（用来创建环境）----
REM ---- 优先用 PRISM_PYTHON 环境变量，其次 PATH 里的 python ----
if defined PRISM_PYTHON if exist "%PRISM_PYTHON%" set "BASE=%PRISM_PYTHON%"
if not defined BASE (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined BASE set "BASE=%%i"
  )
)
if not defined BASE (
  for /f "delims=" %%i in ('where python3 2^>nul') do (
    if not defined BASE set "BASE=%%i"
  )
)

if not defined BASE (
  echo.
  echo   [错误] 没有找到任何 Python，无法创建环境。
  echo   请先安装 Python 3.10 以上版本。
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
    echo   [失败] 创建环境出错。
    pause >nul
    exit /b 1
  )
) else (
  echo   [1/2] 独立环境已存在，跳过创建。
)

echo.
echo   [2/2] 安装 numpy ...
set PYTHONPATH=
set PYTHONHOME=
"%PY%" -m pip install --upgrade pip -i https://pypi.org/simple
"%PY%" -m pip install numpy -i https://pypi.org/simple
if errorlevel 1 (
  echo.
  echo   [提示] 官方源失败，改用清华源重试 ...
  "%PY%" -m pip install numpy -i https://pypi.tuna.tsinghua.edu.cn/simple
)

echo.
echo   ============================================================
echo    验证
echo   ============================================================
"%PY%" -c "import sys,sqlite3,numpy; print('   Python   :', sys.version.split()[0]); print('   numpy    :', numpy.__version__); print('   sqlite3  :', sqlite3.sqlite_version); print(); print('   环境就绪，现在可以双击 prism.bat 启动了。')"

echo.
echo   窗口将在 30 秒后自动关闭
"%SystemRoot%\System32\timeout.exe" /t 30 >nul
exit /b 0
