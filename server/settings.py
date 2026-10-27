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


def _ffmpeg_candidates() -> list[str]:
    """按「越可能命中、越不打扰」的顺序列出候选路径。

    顺序：设置里的手填值 → PRISM_FFMPEG → PATH → 本项目（虚拟环境 / data/bin）
          → 各种包管理器安装位置 → 常见安装位置 → 类 Unix 路径
    刻意不写死任何个人机器的路径。
    """
    import os
    import shutil
    from pathlib import Path

    out: list[str] = []
    manual = ""
    try:
        manual = (load().get("ffmpeg_path") or "").strip()
    except Exception:                                   # noqa: BLE001
        manual = ""
    if manual:
        out.append(manual)                              # 用户明确指定的，最有优先级
    env = (os.environ.get("PRISM_FFMPEG") or "").strip()
    if env:
        out.append(env)

    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    root = Path(config.ROOT)
    out += [
        str(root / ".venv" / "ffmpeg" / "bin" / exe),   # 本项目部署的（推荐位置）
        str(root / "data" / "bin" / exe),
        str(root / "bin" / exe),
    ]
    w = shutil.which("ffmpeg")                          # 系统 PATH
    if w:
        out.append(w)
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA", "")
        up = os.environ.get("USERPROFILE", "")
        pd = os.environ.get("ProgramData", "")
        for base in (
            f"{la}\\Microsoft\\WinGet\\Links\\ffmpeg.exe",
            f"{up}\\scoop\\shims\\ffmpeg.exe",
            f"{pd}\\chocolatey\\bin\\ffmpeg.exe",
            "C:\\ffmpeg\\bin\\ffmpeg.exe",
            "C:\\ffmpeg.exe",
            "C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe",
            "C:\\Program Files (x86)\\ffmpeg\\bin\\ffmpeg.exe",
        ):
            if base:
                out.append(base)
    else:
        out += ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]
    return [p for p in dict.fromkeys(out) if p]


def find_ffmpeg() -> str:
    """自动探测 ffmpeg。找到返回路径，找不到返回空串（不做版本校验，够快）。"""
    import os
    for c in _ffmpeg_candidates():
        if os.path.isfile(c):
            return c
    return ""


def ffmpeg_version(path: str) -> str:
    """跑一次 -version 取版本行；拿不到返回空串。"""
    import subprocess
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout:
            return (r.stdout.splitlines() or [""])[0].strip()
    except Exception:
        pass
    return ""


def ffmpeg_status() -> dict:
    """给界面/接口用：找到没有、在哪、什么版本、从哪来的。"""
    manual = (load().get("ffmpeg_path") or "").strip()
    hit = find_ffmpeg()
    deployed = str((__import__("pathlib").Path(config.ROOT) / ".venv" / "ffmpeg" / "bin" /
                    ("ffmpeg.exe" if __import__("os").name == "nt" else "ffmpeg")))
    src = ""
    if hit:
        low = hit.lower()
        if low.endswith(deployed.lower()):
            src = "本项目虚拟环境里部署的"
        elif manual and hit == manual:
            src = "设置里手填"
        elif "winget" in low:
            src = "WinGet"
        elif "scoop" in low:
            src = "Scoop"
        elif "chocolatey" in low:
            src = "Chocolatey"
        else:
            src = "系统 PATH / 常见位置"
    return {
        "found": bool(hit),
        "path": hit,
        "version": ffmpeg_version(hit) if hit else "",
        "source": src,
        "deployed_path": deployed,
        "deployed": bool(hit) and hit.lower().endswith(deployed.lower()),
        "candidates": _ffmpeg_candidates()[:12],
    }


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


def apply_deployed_ffmpeg() -> str:
    """部署成功后，把 .venv 里的那个路径写进设置（优先级最高、永远先命中）。"""
    from pathlib import Path
    import os
    exe = Path(config.ROOT) / ".venv" / "ffmpeg" / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if exe.is_file():
        save({"ffmpeg_path": str(exe)})
        return str(exe)
    return ""


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
