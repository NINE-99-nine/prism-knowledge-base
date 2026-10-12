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
echo   (.venv)，并安装依赖。
echo.
echo   numpy      （必需）—— 向量运算
echo   pywebview  （可选）—— 原生桌面窗口；不装则用系统浏览器
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
  echo   [1/3] 创建独立环境 .venv ...
  "%BASE%" -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo   [失败] 创建环境出错。
    pause >nul
    exit /b 1
  )
) else (
  echo   [1/3] 独立环境已存在，跳过创建。
)

echo.
echo   [2/3] 安装 numpy ...
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
echo   [3/3] 安装 pywebview（可选，原生桌面窗口）...
REM ---- 失败不算错：没它 Prism 照样跑，只是改用系统浏览器打开界面 ----
"%PY%" -m pip install pywebview -i https://pypi.org/simple
if errorlevel 1 (
  "%PY%" -m pip install pywebview -i https://pypi.tuna.tsinghua.edu.cn/simple
)
if errorlevel 1 (
  echo.
  echo   [跳过] pywebview 没装上（不影响使用）。
  echo          没有它 Prism 会用系统浏览器打开界面，
  echo          但「把文件拖进来直接入库」这个功能需要原生窗口。
  echo          想要的话稍后手动执行：
  echo            .venv\Scripts\python -m pip install pywebview
)

echo.
echo   ============================================================
echo    验证
echo   ============================================================
"%PY%" -c "import sys,sqlite3,numpy; print('   Python   :', sys.version.split()[0]); print('   numpy    :', numpy.__version__); print('   sqlite3  :', sqlite3.sqlite_version)"
"%PY%" -c "import webview; print('   桌面窗口 : 可用（pywebview', webview.__version__ if hasattr(webview,'__version__') else 'ok', ')')" 2>nul || echo    桌面窗口 : 不可用（将用系统浏览器打开）
echo.
echo   环境就绪，现在可以双击 prism.bat 启动了。
echo.
echo   窗口将在 30 秒后自动关闭
"%SystemRoot%\System32\timeout.exe" /t 30 >nul
exit /b 0
