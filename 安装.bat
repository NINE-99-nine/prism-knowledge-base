@echo off
chcp 936 >nul
cd /d "%~dp0"
title Prism - 安装环境
setlocal enabledelayedexpansion

REM ============================================================
REM  本文件必须以 GBK / ANSI 编码保存，不要存成 UTF-8。
REM  cmd 读取 UTF-8 批处理时按字节位移找行，中文会错位：
REM  轻则提示乱码，重则把注释当成命令执行。存成 GBK 就没有这个问题。
REM ============================================================

echo.
echo   ============================================================
echo    Prism 棱镜 - 环境安装
echo   ============================================================
echo.

REM ---- 先判断这一步是不是早就做过了：.venv 在、numpy 与 pywebview 都能 import ----
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" goto do_install
"%PY%" -c "import numpy, webview" >nul 2>nul
if errorlevel 1 goto do_install

REM ============ 已经装好：不再问 [1]/[2]，直接进 ffmpeg 这一步 ============
echo   检查结果：Python 环境 .venv、numpy、pywebview 都已经装好，这一步跳过。
echo.
if exist "%~dp0.venv\ffmpeg\bin\ffmpeg.exe" goto ffmpeg_ready
where ffmpeg >nul 2>nul
if not errorlevel 1 goto ffmpeg_ready
echo   剩下只有 ffmpeg 没装：音频、视频、AVIF/TIFF 图片现在导不进去。
call :ask_ffmpeg
if defined DEPLOY_FFMPEG goto after_deps
set "QUICK=1"
goto done

:ffmpeg_ready
echo   ffmpeg 也已经可用（本项目 .venv 里，或系统里早就装过），不需要再做任何事。
set "QUICK=1"
goto done

REM ============ 还没装：完整安装流程 ============
:do_install
echo   这一步会在 prism 文件夹内创建一个独立 Python 环境
echo   (.venv)，并安装以下必需依赖：
echo.
echo     numpy      向量运算
echo     pywebview  原生桌面窗口（有了它才能把文件拖进窗口直接入库）
echo.
echo   另外还有一个可选的：ffmpeg（音频切段 / 视频抽帧 / AVIF-TIFF 转图要用）。
echo   它不是 Python 包，是单个可执行文件，约 80~100 MB，会装进 .venv\ffmpeg 里。
echo   现在不装也能用（只是音频、视频、AVIF/TIFF 图片导不进去），
echo   以后随时可以在界面的「设置 -^> ffmpeg」里点一下部署。
echo.
echo   ------------------------------------------------------------
echo     [1] 只装上面两个必需依赖          默认，10 秒内不选就选这个
echo     [2] 连 ffmpeg 一起装
echo   ------------------------------------------------------------
choice /c 12 /n /t 10 /d 1 /m "   请选择 [1/2]："
if errorlevel 2 goto want_ffmpeg
goto deps_start

:want_ffmpeg
call :ask_ffmpeg
if not defined DEPLOY_FFMPEG echo   [跳过] 之后可在「设置 -^> ffmpeg」里一键部署。
goto deps_start

:deps_start

set "PY=%~dp0.venv\Scripts\python.exe"
set "BASE="
set "BASEARG="

REM ---- 找一个可用的基础 Python（用来创建环境）----
REM ---- 顺序：PRISM_PYTHON 环境变量 → py 启动器 → PATH 里的 python ----
if defined PRISM_PYTHON if exist "%PRISM_PYTHON%" set "BASE=%PRISM_PYTHON%"

REM py 启动器：直接把它当解释器用，不捕获它的输出。
REM 捕获要经过管道和控制台代码页，路径里一旦有非 ASCII 字符（比如全角括号），
REM 取回来的路径会失真，导致把可用的 Python 误判成不可用。
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

REM ---- 选下载源 ----
REM 光测延迟不准：延迟几百毫秒的源也可能只有几十 kB/s。
REM 这里用 pip 真下一遍它自己（约 1.8MB），超过 8 秒就改用国内镜像；
REM 这一步下的包后面升级 pip 时正好用得上，不浪费。
set "PIPIDX=-i https://pypi.org/simple"
set "PIPALT=-i https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIPNAME=PyPI 官方源"
set "ALTNAME=清华镜像"
set "SPDDIR=%~dp0_pipcheck_tmp"
set "DT="
mkdir "%SPDDIR%" 2>nul
for /f "tokens=1-4 delims=:.," %%a in ("%TIME: =0%") do set /a "T1=1%%a*3600+1%%b*60+1%%c-366100"
"%PY%" -m pip download --no-deps --quiet --disable-pip-version-check --timeout 8 --retries 1 -d "%SPDDIR%" pip !PIPIDX! >nul 2>nul
if errorlevel 1 (
  set "DT=99"
) else (
  for /f "tokens=1-4 delims=:.," %%a in ("%TIME: =0%") do set /a "T2=1%%a*3600+1%%b*60+1%%c-366100"
  set /a "DT=T2-T1"
)
rmdir /s /q "%SPDDIR%" 2>nul

if defined DT if !DT! GEQ 8 (
  set "PIPIDX=-i https://pypi.tuna.tsinghua.edu.cn/simple"
  set "PIPALT=-i https://pypi.org/simple"
  set "PIPNAME=清华镜像"
  set "ALTNAME=PyPI 官方源"
)
if defined DT (
  echo   下载源: !PIPNAME!（官方源下 1.8MB 用了 !DT! 秒）
) else (
  echo   下载源: !PIPNAME!
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

:after_deps
if defined DEPLOY_FFMPEG (
  echo.
  echo   ---- ffmpeg（部署到 .venv\ffmpeg\bin）----
  echo   下载中请勿关闭窗口，失败会自动换备用源再试一次。
  echo.
  "%PY%" "%~dp0tools\deploy_ffmpeg.py"
  if errorlevel 1 (
    echo.
    echo   [提示] ffmpeg 没部署成功（多半是网络或被代理拦住）。
    echo          或自己下载 ffmpeg.exe 放到 .venv\ffmpeg\bin\ 下。
    echo          不影响文本和普通图片；音频/视频暂时导不进去。
  ) else (
    echo   ffmpeg 已就绪：.venv\ffmpeg\bin\ffmpeg.exe
  )
)

:done

echo   环境就绪，现在可以双击 prism.bat 启动了。
echo.
if defined QUICK (
  echo   窗口将在 6 秒后自动关闭
  "%SystemRoot%\System32\timeout.exe" /t 6 >nul
) else (
  echo   窗口将在 30 秒后自动关闭
  "%SystemRoot%\System32\timeout.exe" /t 30 >nul
)
exit /b 0

REM ============================================================
REM  子程序：讲清楚 ffmpeg 是什么、多大、装在哪，要按一次 Y 才部署
REM  两个入口都会 call 它：首次完整安装时、以及依赖已装好时
REM ============================================================
:ask_ffmpeg
echo.
echo   ============================================================
echo    先看清楚：ffmpeg 是什么、装了有什么用
echo   ============================================================
echo    作用   ：音频切段、视频抽帧、AVIF/TIFF 图片转 JPEG
echo    不装   ：音频、视频、AVIF/TIFF 图片 无法导入索引；
echo             文本和 png/jpg/webp/gif 图片完全不受影响
echo    体积   ：约 80~100 MB（单个文件）
echo    位置   ：本项目 .venv\ffmpeg\bin\ffmpeg.exe
echo             （在 .venv 里，不进 git，也不会撑大发布包；
echo               重跑本脚本若重建了 .venv，需要再部署一次）
echo    建议   ：装上。本机如果早就装过 ffmpeg，程序会自动探测到，不必再装。
echo.
echo    必须按 Y 才会真的下载部署。
set "YN="
set /p "YN=   确认部署 ffmpeg？按 Y 回车继续，直接回车则跳过："
if /i "%YN%"=="Y" set "DEPLOY_FFMPEG=1"
exit /b

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
