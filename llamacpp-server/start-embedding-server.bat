@echo off
chcp 65001 >nul
title EmbeddingGemma-2 Multimodal Server (text+image+video+audio, 768d) - Port 8084
cd /d "%~dp0"

REM ============================================================
REM  EmbeddingGemma-2 多模态嵌入服务
REM
REM  用 llama.cpp 的 llama-server 同时加载：
REM    主模型  embeddinggemma-2-BF16.gguf          (270M 文本)
REM    编码器  mmproj-embeddinggemma-2-Q8_0.gguf   (170M 视觉 + 300M 音频)
REM  四模态进同一个 768 维向量空间 (text / image / video / audio)
REM
REM  端点:  http://0.0.0.0:8084
REM  接口:  POST /v1/embeddings   (OpenAI 兼容)
REM  停止:  关掉本窗口，或按 Ctrl+C
REM ============================================================
REM
REM  ★ 使用前请改这三处：
REM     1) LLAMA_SERVER  —— 你的 llama.cpp 的 llama-server.exe 路径
REM     2) MAIN_MODEL    —— 主模型 gguf 路径
REM     3) MMPROJ        —— 多模态编码器 gguf 路径
REM
REM  模型下载（Hugging Face 上的 GGUF 量化版）：
REM     https://huggingface.co/ (搜 embeddinggemma-2 gguf)
REM     两个文件都要下：主模型 + 对应的 mmproj
REM
REM  路径支持相对本脚本所在目录，也可以写绝对路径。
REM ============================================================

set "LLAMA_SERVER=llama-server.exe"
set "MAIN_MODEL=models\embeddinggemma-2-BF16.gguf"
set "MMPROJ=models\mmproj-embeddinggemma-2-Q8_0.gguf"

REM ---- 上下文长度 ----
REM  EmbeddingGemma-2 官方上限是 2048 token，所以这里就设 2048 ——
REM  设更大不会更好：模型只认前 2048，多出来的部分是死区。
REM  实测占用：512px 图像 80 token/帧；音频约 25 token/秒。
set "CTX=2048"

if not exist "%LLAMA_SERVER%" (
  echo.
  echo   [错误] 找不到 llama-server：%LLAMA_SERVER%
  echo   请编辑本脚本，把 LLAMA_SERVER 改成你的实际路径。
  echo.
  pause >nul
  exit /b 1
)
if not exist "%MAIN_MODEL%" (
  echo.
  echo   [错误] 找不到主模型：%MAIN_MODEL%
  echo   请编辑本脚本，把 MAIN_MODEL 改成你的实际路径。
  echo.
  pause >nul
  exit /b 1
)

echo.
echo   正在启动嵌入服务（端口 8084）...
echo.

"%LLAMA_SERVER%" ^
  -m "%MAIN_MODEL%" ^
  --mmproj "%MMPROJ%" ^
  --host 0.0.0.0 ^
  --port 8084 ^
  --batch-size %CTX% ^
  --ubatch-size %CTX% ^
  -c %CTX% ^
  -ngl 99 ^
  --embeddings ^
  --pooling mean

echo.
echo [Server stopped]
pause
