@echo off
chcp 65001 >nul
title 停止 Prism

echo.
echo   正在停止 Prism（端口 8848）...
echo.

set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8848" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
    if not errorlevel 1 (
        echo   已结束进程 PID %%a
        set FOUND=1
    )
)

if "%FOUND%"=="0" echo   没有发现正在运行的 Prism。

echo.
echo   完成。按任意键关闭窗口。
pause >nul
