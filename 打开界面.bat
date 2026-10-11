@echo off
REM 只打开 Prism 界面（不启动服务）。
REM 用途：Prism 已经在运行时，想再看一眼界面 —— 用它，就不会和 prism.bat 混淆。
REM 如果服务没在运行，浏览器会显示连不上，这时请双击 prism.bat。

start "" "http://127.0.0.1:8848"
exit /b 0
