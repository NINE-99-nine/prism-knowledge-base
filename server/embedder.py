"""Prism · 棱镜 — 嵌入服务客户端

对接本地 llama.cpp 多模态服务（默认 http://127.0.0.1:8084）。
三种模态的请求格式都已实测：
    文本 → {"input": ["文本", ...]}
    图像 → {"input":[{"content":[{"type":"image_url", ...}]}]}
    音频 → {"input":[{"content":[{"type":"input_audio", ...}]}]}
    视频 → 抽帧后按「多张图」一次送入（服务端把它们聚合成一个向量）
"""
import base64
import hashlib
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

import settings


def _api() -> str:
    return settings.get("embed_api")


def _post(body: dict, timeout: int | None = None) -> dict:
    if timeout is None:
        timeout = int(settings.get("embed_timeout") or 180)
    req = urllib.request.Request(
        _api(), data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # 把服务端自己说的话带出来 —— 否则界面上永远只有「HTTP Error 500: Internal Server Error」，
        # 看不出到底是图太大、格式不吃、还是模型崩了。
        try:
            detail = e.read().decode("utf-8", "replace").strip()
        except Exception:                                   # noqa: BLE001
            detail = ""
        raise RuntimeError(f"嵌入服务返回 {e.code}：{detail[:300] or e.reason}") from e


def health() -> tuple[bool, str]:
    """探活。返回 (是否可用, 说明)。"""
    base = _api().split("/v1/")[0]
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            if r.status == 200:
                return True, "ok"
            return False, f"HTTP {r.status}"
    except Exception as e:
        return False, str(e)[:140]


# ------------------------------------------------------------------ 三种模态

def embed_texts(texts: list[str], batch: int = 16) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        d = _post({"input": texts[i:i + batch], "model": settings.get("embed_model")})
        out += [x["embedding"] for x in d["data"]]
    return out


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


def _b64(path) -> tuple[str, str]:
    p = Path(path)
    raw = base64.b64encode(p.read_bytes()).decode()
    return raw, p.suffix.lower().lstrip(".")


# 🔴 必须先转成 JPEG 才能嵌入的格式。
# 实测（同内容、同尺寸对照）：AVIF / TIFF 会被 llama.cpp 的图片解码路径解出
# **异常偏高的 token 数** —— 800×1420 的 AVIF 报 2061 token 直接被拒，
# 而同尺寸、同内容的 JPEG 毫无问题（JPEG 甚至到 1000 万像素也照过）。
# 结论：跟「图太大」无关，是这两种格式的解码路径有问题。转成 JPEG 后一切正常。
IMAGE_TRANSCODE_EXT = {".avif", ".tif", ".tiff"}


def _scratch(prefix: str = "img") -> Path:
    """Prism 自己的临时目录。

    🔴 绝不能用 tempfile.mkdtemp() 的默认位置 —— 那是
       C:\\Users\\<用户>\\AppData\\Local\\Temp，属于「禁写 C 盘」的雷区。
       统一落在 prism/data/tmp/ 下，由 app 的 cleanup_tmp 按既有策略清理。
    """
    import config
    d = Path(config.DATA_DIR) / "tmp" / prefix
    d.mkdir(parents=True, exist_ok=True)
    return d


def _to_jpeg(path, tmpdir, max_edge: int = 0) -> Path:
    """用 ffmpeg 把图片转成 JPEG。max_edge>0 时顺手把长边缩到该值以内。"""
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("这张图片要先转成 JPEG 才能嵌入，但没找到 ffmpeg（设置里可指定路径）")
    tag = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    out = Path(tmpdir) / (tag + (f"_{max_edge}.jpg" if max_edge else ".jpg"))
    cmd = [ff, "-y", "-v", "error", "-i", str(path),
           "-vf", f"scale='if(gt(max(iw,ih),{max_edge}),if(gt(iw,ih),-2,{max_edge}),iw)'"
                  f":'if(gt(max(iw,ih),{max_edge}),if(gt(iw,ih),{max_edge},-2),ih)'"
           if max_edge else None,
           "-q:v", "2", str(out)]
    cmd = [c for c in cmd if c is not None]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"图片转 JPEG 失败：{(r.stderr or '').strip()[:160]}")
    return out


def _looks_like_jpeg_or_png(p: Path) -> bool:
    """按文件头判断，不看后缀 —— 后缀经常是骗人的（webp 存成 .jpg 很常见）。"""
    try:
        with open(p, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")


def _embed_image_file(path) -> list[float]:
    raw, ext = _b64(path)
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    d = _post({"model": settings.get("embed_model"), "input": [{"content": [
        {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{raw}"}}]}]})
    return d["data"][0]["embedding"]


def embed_image(path) -> list[float]:
    """嵌入一张图。三级处理，尽量不让"格式问题"毁掉一次导入：

    1) 文件头就是 JPEG / PNG → 直接送（绝大多数图走这条，行为与以前一致）
    2) 其他格式（webp / avif / tiff / bmp / gif，或后缀骗人的）→ 先用 ffmpeg 转 JPEG 再送
    3) 转换了还失败 → 再缩一次长边到 1280 px 送（有些模型对超大图直接 500）
    全失败时把服务端原话带出来，别只说 "HTTP Error 500"
    """
    p = Path(path)
    first_err = None
    if _looks_like_jpeg_or_png(p):
        try:
            return _embed_image_file(p)
        except Exception as e:                              # noqa: BLE001
            first_err = e

    tmp = _scratch("img") / hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:12]
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        try:
            return _embed_image_file(_to_jpeg(p, tmp))
        except Exception as e:                              # noqa: BLE001
            second_err = e
            try:
                return _embed_image_file(_to_jpeg(p, tmp, max_edge=1280))
            except Exception as third:                       # noqa: BLE001
                raise RuntimeError(
                    f"{Path(path).name}：原图直接嵌入失败（{first_err or '格式不被支持'}）；"
                    f"转 JPEG 后仍失败（{second_err}）；缩小到 1280 后仍失败（{third}）"
                ) from third
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def embed_audio(path) -> list[float]:
    raw, ext = _b64(path)
    fmt = "mp3" if ext in ("mp3", "mpeg") else ext
    d = _post({"model": settings.get("embed_model"), "input": [{"content": [
        {"type": "input_audio", "input_audio": {"data": raw, "format": fmt}}]}]})
    return d["data"][0]["embedding"]


# ------------------------------------------------------------------ 视频

def _ffmpeg() -> str:
    """复用设置里的 ffmpeg（音频切分已经探测过一遍）。"""
    p = (settings.get("ffmpeg_path") or "").strip()
    if p:
        return p
    import shutil
    return shutil.which("ffmpeg") or ""


def _duration(ffmpeg: str, path) -> float:
    """取视频时长（秒）。失败返回 0。"""
    import re, subprocess
    probe = str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg.lower().endswith(".exe") else "ffprobe"
    try:
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float((r.stdout or "0").strip().splitlines()[0])
    except Exception:
        return 0.0


def embed_video(path, frames: int = 0, width: int = 0, tmpdir: str = "") -> list[float]:
    """视频 → 均匀抽 N 帧 → 多帧一次嵌入 → 一个 768 维向量。

    为什么是抽帧而不是直接喂视频：EmbeddingGemma-2 的上下文上限是 2048 token，
    实测每张 512px 图占 80 token，直接送整段视频会远超上限（曾被服务端
    以 "input (8498 tokens) is too large" 拒绝）。16 帧约 1280 token，安全且够表达。

    抽帧文件落在给定 tmpdir（调用方负责清理），不碰源视频。
    """
    import subprocess, shutil
    import config

    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("找不到 ffmpeg（音频与视频抽帧都需要它）")

    n = int(frames or config.VIDEO_FRAMES or 16)
    w = int(width or config.VIDEO_FRAME_WIDTH or 512)

    dur = _duration(ff, path)

    # 限「长边」而不是限宽度：竖版视频若只限宽，面积会翻几倍，token 直接爆掉。
    # force_original_aspect_ratio=decrease 让长边缩到 w、短边等比，两种朝向都控制在 w² 以内。
    scale = f"scale={w}:{w}:force_original_aspect_ratio=decrease"
    base_vf = scale if dur <= 0 else f"fps={max(n / dur, 0.01):.6f},{scale}"

    tmp = Path(tmpdir) if tmpdir else _scratch("vid")
    tmp.mkdir(parents=True, exist_ok=True)
    pattern = str(tmp / "f_%03d.jpg")

    def _grab(ffmpeg, vf, count):
        for p in tmp.glob("f_*.jpg"):
            try: p.unlink()
            except OSError: pass
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(path),
                        "-vf", vf, "-frames:v", str(count), pattern],
                       capture_output=True, text=True, timeout=600)
        return sorted(tmp.glob("f_*.jpg"))

    frames_got = _grab(ff, base_vf, n)
    if not frames_got:
        frames_got = _grab(ff, scale, n)          # fps 表达式可能不被某些文件接受

    if not frames_got:
        raise RuntimeError("视频抽帧失败（ffmpeg 没能取出任何帧）")

    # 逐档降帧重试：不同视频每帧 token 有差异（分辨率/朝向/编码），
    # 撞上模型 2048 上限就退一档，直到能嵌入为止。最低 4 帧。
    try:
        attempts = [len(frames_got)]
        cnt = len(frames_got)
        while cnt > 4:
            cnt = max(4, cnt // 2)
            attempts.append(cnt)

        last_err = None
        for i, count in enumerate(attempts):
            use = frames_got[:count] if i == 0 else _grab(ff, scale if count == attempts[-1] else base_vf, count)
            if not use:
                continue
            content = []
            for p in use:
                raw = base64.b64encode(p.read_bytes()).decode()
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{raw}"}})
            try:
                d = _post({"model": settings.get("embed_model"),
                           "input": [{"content": content}]})
                return d["data"][0]["embedding"]
            except Exception as ex:
                last_err = ex
                continue
        raise RuntimeError(f"视频嵌入失败（已降到 {attempts[-1]} 帧仍未通过）：{type(last_err).__name__}: {str(last_err)[:100]}")
    finally:
        for p in tmp.glob("f_*.jpg"):
            try: p.unlink()
            except OSError: pass


# ------------------------------------------------------------------ 任意内容

def embed_blob(filename: str, raw: bytes, tmp_root=None) -> list[float]:
    """按文件名后缀判断模态，把一段二进制内容变成向量（前端拖拽检索用）。

    图片：直接在内存里 base64，不落盘。
    音频/视频：ffmpeg 只认文件路径，必须先落临时文件 —— 落在 tmp_root
              （调用方给 prism/data/tmp），用完立即删除；不传则用系统临时目录。

    这一层是「拖进来的东西」与「库里已有的东西」之间唯一的桥：
    两边都进同一个 768 维空间，所以图搜视频、音频搜图都成立。
    """
    import config

    ext = Path(filename).suffix.lower()

    if ext in config.IMAGE_EXT:
        if ext in IMAGE_TRANSCODE_EXT:
            # AVIF / TIFF 直接送会被解出异常 token 数，先落盘转 JPEG（用完即删）
            tmp = Path(tmp_root) if tmp_root else _scratch("q")
            tmp.mkdir(parents=True, exist_ok=True)
            src = tmp / (hashlib.sha1(raw[:4096]).hexdigest()[:12] + ext)
            try:
                src.write_bytes(raw)
                return _embed_image_file(_to_jpeg(src, tmp))
            finally:
                for f in tmp.glob("*"):
                    try: f.unlink()
                    except OSError: pass
                if not tmp_root:
                    try: tmp.rmdir()
                    except OSError: pass
        mime = "jpeg" if ext in (".jpg", ".jpeg") else ext.lstrip(".")
        d = _post({"model": settings.get("embed_model"), "input": [{"content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/{mime};base64," + base64.b64encode(raw).decode()}}]}]})
        return d["data"][0]["embedding"]

    if ext in config.AUDIO_EXT:
        # 检索只需要「一段有代表性的片段」，不必整首送：
        # 实测音频 25 token/秒，245 秒的歌 = 6125 token ≫ 2048，整首必然 500。
        # 做法：落临时文件 → ffmpeg 截前 AUDIO_CHUNK_SECONDS 秒 → 嵌入；截取失败才退回整段。
        import hashlib
        ff = _ffmpeg()
        tmp = Path(tmp_root) if tmp_root else _scratch("q")
        tmp.mkdir(parents=True, exist_ok=True)
        # 文件名用 hash：源文件名可能含中文/空格/% 等，ffmpeg 处理起来易出岔子
        stem = hashlib.sha1(raw[:4096]).hexdigest()[:12]
        src = tmp / (stem + ext)
        clip = tmp / (stem + "_clip.mp3")
        data_bytes = raw
        try:
            src.write_bytes(raw)
            if ff:
                r = subprocess.run([ff, "-y", "-v", "error", "-t", str(config.AUDIO_CHUNK_SECONDS),
                                    "-i", str(src), "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(clip)],
                                   capture_output=True, timeout=600)
                if clip.exists() and clip.stat().st_size > 0:
                    data_bytes = clip.read_bytes()
            d = _post({"model": settings.get("embed_model"), "input": [{"content": [
                {"type": "input_audio",
                 "input_audio": {"data": base64.b64encode(data_bytes).decode(), "format": "mp3"}}]}]})
            return d["data"][0]["embedding"]
        finally:
            for f in (src, clip):
                try:
                    if f.is_file(): f.unlink()
                except OSError:
                    pass
            if not tmp_root:
                try: tmp.rmdir()
                except OSError: pass

    if ext in config.VIDEO_EXT:
        import hashlib
        tmp = Path(tmp_root) if tmp_root else _scratch("q")
        tmp.mkdir(parents=True, exist_ok=True)
        f = tmp / (hashlib.sha1(raw[:4096]).hexdigest()[:12] + ext)
        try:
            f.write_bytes(raw)
            return embed_video(f, tmpdir=tmp / "frames")
        finally:
            for p in (tmp / "frames").glob("*"):
                try: p.unlink()
                except OSError: pass
            try: (tmp / "frames").rmdir()
            except OSError: pass
            try: f.unlink()
            except OSError: pass
            if not tmp_root:
                try: tmp.rmdir()
                except OSError: pass

    # 文本类：直接当文本嵌入
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"不支持的内容类型：{ext or '（无扩展名）'}")
    return embed_text(text[:config.CHUNK_SIZE])


# ------------------------------------------------------------------ 向量存取

def to_bytes(vec) -> bytes:
    """向量 → float16 字节（体积减半）。"""
    a = np.asarray(vec, dtype=np.float32)
    return a.astype(np.float16).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


def matrix_from_blobs(blobs) -> np.ndarray:
    if not blobs:
        return np.zeros((0, int(settings.get("embed_dim") or 768)), dtype=np.float32)
    return np.vstack([from_blob(b) for b in blobs])


def normalize(m: np.ndarray) -> np.ndarray:
    """L2 归一化（归一化后点积 = 余弦）。"""
    if m.ndim == 1:
        n = np.linalg.norm(m)
        return m / n if n else m
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n
