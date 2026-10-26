"""Prism · 棱镜 — 可运行时修改的设置

存 data/settings.json，启动时覆盖 config.py 里的默认值。
放在 prism 目录内（便携自包含），且是纯文本、能手改。

为什么不用数据库：配置不该跟着某个库走；换库/重建库时配置应当还在。
"""
import json
import threading

import config

FILE = config.DATA_DIR / "settings.json"

# 允许在界面上修改的键（其余 config 项属于程序常量，不暴露）
EDITABLE = {
    "embed_api":     "嵌入服务地址（OpenAI 兼容的 /v1/embeddings 端点）",
    "embed_model":   "模型名（请求体里的 model 字段）",
    "embed_dim":     "向量维度（必须与模型实际输出一致）",
    "embed_timeout": "单次请求超时（秒）",
    "ffmpeg_path":   "ffmpeg 可执行文件路径（音频切分用；留空 = 自动探测）",
    "audio_chunk_seconds": "音频每段时长（秒）。实测约 25 token/秒，2048 上下文下单段上限约 80 秒",
}


def find_ffmpeg() -> str:
    """自动探测 ffmpeg。找到返回路径，找不到返回空串。

    顺序：环境变量 → PATH → 常见安装位置（不写死任何人的本机路径）。
    """
    import os
    import shutil
    env = (os.environ.get("PRISM_FFMPEG") or "").strip()
    if env and os.path.isfile(env):
        return env
    p = shutil.which("ffmpeg")
    if p:
        return p
    for c in (r"C:\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"):
        if os.path.isfile(c):
            return c
    return ""


DEFAULTS = {
    "embed_api":     config.EMBED_API,
    "embed_model":   config.EMBED_MODEL,
    "embed_dim":     config.EMBED_DIM,
    "embed_timeout": config.EMBED_TIMEOUT,
    "ffmpeg_path":   find_ffmpeg(),
    "audio_chunk_seconds": 60,
}

_lock = threading.Lock()
_cache: dict | None = None


def load() -> dict:
    """读当前生效的设置（默认值 + settings.json 覆盖）。"""
    global _cache
    with _lock:
        if _cache is None:
            data = dict(DEFAULTS)
            if FILE.is_file():
                try:
                    saved = json.loads(FILE.read_text(encoding="utf-8"))
                    data.update({k: v for k, v in saved.items() if k in DEFAULTS})
                except Exception:
                    pass          # 文件坏了就用默认值，不阻塞启动
            _cache = data
        return dict(_cache)


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def save(patch: dict) -> dict:
    """只接受可编辑的键；写盘并立即生效。"""
    global _cache
    with _lock:
        data = dict(_cache) if _cache else dict(DEFAULTS)
        for k, v in (patch or {}).items():
            if k not in EDITABLE or v in (None, ""):
                continue
            if k in ("embed_dim", "embed_timeout", "audio_chunk_seconds"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            data[k] = v
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8", newline="\n")
        _cache = data
        return dict(data)


def reset() -> dict:
    """回到 config.py 的默认值（删掉 settings.json）。"""
    global _cache
    with _lock:
        if FILE.is_file():
            FILE.unlink()
        _cache = dict(DEFAULTS)
        return dict(_cache)


def describe() -> dict:
    """给界面用：当前值 + 默认值 + 哪些被改过。"""
    cur = load()
    return {
        "current": cur,
        "defaults": dict(DEFAULTS),
        "editable": EDITABLE,
        "changed": {k: v for k, v in cur.items() if v != DEFAULTS.get(k)},
        "file": str(FILE),
        "file_exists": FILE.is_file(),
    }
