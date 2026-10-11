"""Prism · 棱镜 — 导入流程（带进度条，后台线程）

导入规则：
    文本 → 分块，每块一个向量
    图像 → 整图一个向量
    音频 → 整段一个向量（超过体积上限的跳过并记入失败清单）
增量规则：
    路径 + mtime + size 指纹不变 → 跳过
    变了 → 删旧条目、重新嵌入
    源文件消失 → 标记 missing（不删条目，不碰源文件）
"""
import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import config
import db
import scanner
import settings
from chunker import chunk_text
from config import AUDIO_MAX_BYTES, classify
from embedder import embed_audio, embed_image, embed_texts, embed_video, to_bytes

_jobs: dict[str, dict] = {}
_last_save: dict[str, float] = {}
_lock = threading.Lock()
_SAVE_INTERVAL = 3.0        # 进度落盘节流：最多每 3 秒写一次库


# ------------------------------------------------------------------ 音频切分

def _fmt_time(sec: float) -> str:
    s = int(max(0, sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def _ffmpeg() -> str:
    """拿 ffmpeg 路径（设置优先，其次自动探测）。"""
    ff = (settings.get("ffmpeg_path") or "").strip() or settings.find_ffmpeg()
    if not ff:
        raise ValueError("音频切分需要 ffmpeg，但没找到。请在「设置」里指定 ffmpeg 路径")
    if not os.path.isfile(ff):
        import shutil
        w = shutil.which(ff)
        if not w:
            raise ValueError(f"ffmpeg 路径无效: {ff}")
        ff = w
    return ff


def _probe_duration(ff: str, path: str) -> float:
    """音频总时长（秒）。优先 ffprobe，退化为解析 ffmpeg -i 的输出。"""
    p = Path(ff)
    probe = p.with_name("ffprobe.exe") if p.suffix.lower() == ".exe" else p.with_name("ffprobe")
    if probe.is_file():
        try:
            r = subprocess.run(
                [str(probe), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=120)
            return max(0.0, float(r.stdout.strip()))
        except (ValueError, subprocess.SubprocessError):
            pass
    try:
        r = subprocess.run([ff, "-i", path], capture_output=True, text=True, timeout=120)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except subprocess.SubprocessError:
        pass
    return 0.0


def _split_audio(path: str, chunk_seconds: int) -> list[tuple[float, float, str]]:
    """把音频按时长切成若干段 → [(起, 止, 文件路径)]。

    为什么要切：嵌入模型的上下文有限（本机 8084 是 -c 4096），
    实测音频约 68 token/秒 → 60 秒段 ≈ 4080 token，正好卡在 4096 悬崖边
    （采样率稍高就溢出，服务端报 "Failed to tokenize prompt"），
    所以默认取 30 秒（≈2000 token），留一半余量。

    短于一段的音频原样返回，不产生临时文件。
    """
    ff = _ffmpeg()
    dur = _probe_duration(ff, path)
    if dur <= 0:
        # 探测不出时长 = 读不了这个文件（损坏/格式不支持）。
        # 绝不能当成"短文件"直接整段送上去 —— 那必然 token 溢出（HTTP 500）。
        raise ValueError("无法识别音频（文件可能损坏或格式不受支持），已跳过")
    if dur <= chunk_seconds:
        return [(0.0, dur, path)]

    tmpdir = config.DATA_DIR / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]

    out: list[tuple[float, float, str]] = []
    t, i = 0.0, 0
    while t < dur - 0.2:
        seg = min(chunk_seconds, dur - t)
        outp = tmpdir / f"a_{stem}_{i:04d}.mp3"
        subprocess.run(
            [ff, "-v", "error", "-y", "-ss", f"{t:.3f}", "-t", f"{seg:.3f}",
             "-i", path, "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(outp)],
            check=True, capture_output=True, timeout=1800)
        out.append((t, t + seg, str(outp)))
        t += seg
        i += 1
    return out


# ------------------------------------------------------------------ 进度

def get_job(job_id: str) -> dict | None:
    """单个任务：内存优先，其次落盘的记录。"""
    with _lock:
        j = _jobs.get(job_id)
        if j:
            return dict(j)
    try:
        return db.get_job(job_id)
    except Exception:
        return None


def list_jobs() -> list[dict]:
    """内存里的 + 落盘的合并（内存优先），按开始时间倒序。"""
    with _lock:
        mem = {j["job_id"]: dict(j) for j in _jobs.values()}
    try:
        for d in db.load_jobs(60):
            mem.setdefault(d["job_id"], d)
    except Exception:
        pass
    # 服务重启后残留的 running 记录，标成「已中断」（进程都没了）
    running_ids = set()
    with _lock:
        running_ids = {j["job_id"] for j in _jobs.values()
                       if j.get("state") in ("running", "cancelling")}
    for jid, j in mem.items():
        if j.get("state") in ("running", "cancelling") and jid not in running_ids:
            j["state"] = "interrupted"
    return sorted(mem.values(), key=lambda x: x.get("started") or 0, reverse=True)


def _update(job_id: str, **fields) -> None:
    with _lock:
        if job_id not in _jobs:
            return
        _jobs[job_id].update(fields)
        snap = dict(_jobs[job_id])
    # 节流落盘：状态终结时必写，否则每 3 秒写一次
    now = time.time()
    final = snap.get("state") in ("done", "error", "cancelled", "interrupted")
    if final or now - _last_save.get(job_id, 0) >= _SAVE_INTERVAL:
        _last_save[job_id] = now
        try:
            db.save_job(snap)
        except Exception:
            pass


def is_cancelled(job_id: str) -> bool:
    with _lock:
        j = _jobs.get(job_id)
        if j and j.get("cancelled"):
            return True
    try:
        d = db.get_job(job_id)
        return bool(d and d.get("cancelled"))
    except Exception:
        return False


def cancel_job(job_id: str) -> dict:
    """请求中断导入。正在跑的循环会在下一步退出；已导入的条目保留。"""
    with _lock:
        cur = _jobs.get(job_id)
    if cur and cur.get("state") == "running":
        with _lock:
            _jobs[job_id]["cancelled"] = True
            _jobs[job_id]["state"] = "cancelling"
        try:
            db.set_job_cancelled(job_id, True)
        except Exception:
            pass
        return {"ok": True, "message": "已请求中断，当前文件处理完就停"}

    # 不在内存（服务重启过）→ 直接标记为中断，免得一直显示 running
    try:
        d = db.get_job(job_id)
        if d and d.get("state") == "running":
            d["state"] = "interrupted"
            d["cancelled"] = True
            d["finished"] = d.get("finished") or time.time()
            db.save_job(d)
            return {"ok": True, "message": "该任务已不在运行（服务重启过），已标记为中断"}
    except Exception:
        pass
    return {"ok": False, "message": "这个任务不在运行中"}


def retry_failed(job_id: str) -> dict:
    """把某个任务的失败清单重新导一遍。已成功的会被指纹跳过，不会重复。"""
    d = None
    with _lock:
        if job_id in _jobs:
            d = dict(_jobs[job_id])
    if not d:
        try:
            d = db.get_job(job_id)
        except Exception:
            d = None
    if not d:
        return {"ok": False, "message": "找不到这个任务"}
    paths = [f.get("path") for f in (d.get("failed") or []) if f.get("path")]
    lib = d.get("library_id")
    if not paths:
        return {"ok": False, "message": "这个任务没有失败项"}
    if not lib:
        return {"ok": False, "message": "任务缺少库信息"}
    new_id = start_import(int(lib), paths, recursive=False)
    return {"ok": True, "job_id": new_id, "count": len(paths)}


# ------------------------------------------------------------------ 单文件嵌入

def embed_file(info: dict, on_chunk=None) -> list[dict]:
    """返回 [{chunk_index, modality, vec_bytes, chunk_text}]"""
    path = info["path"]
    modality = info["modality"]
    out: list[dict] = []

    if modality == "image":
        out.append({"chunk_index": 0, "modality": "image",
                    "vec_bytes": to_bytes(embed_image(path)), "chunk_text": ""})
        if on_chunk:
            on_chunk(1)
        return out

    if modality == "video":
        # 抽帧 → 多帧一次嵌入 → 一个向量（整段视频直接送会超出模型的 2048 token 上限）
        # 帧文件落在 Prism 自己的 data/tmp 里，由 cleanup_tmp 按既有策略清理
        tmpdir = config.DATA_DIR / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
        sub = tmpdir / f"v_{stem}"
        try:
            vec = embed_video(path, tmpdir=sub)
            out.append({"chunk_index": 0, "modality": "video",
                        "vec_bytes": to_bytes(vec), "chunk_text": ""})
        except Exception as e:
            raise ValueError(f"视频处理失败：{type(e).__name__}: {str(e)[:160]}")
        finally:
            # 帧文件当即清掉，不留到 cleanup_tmp 那一轮（视频帧很占地方）
            try:
                for f in sub.glob("*"):
                    f.unlink()
                sub.rmdir()
            except OSError:
                pass
        if on_chunk:
            on_chunk(1)
        return out

    if modality == "audio":
        size = info.get("size", 0)
        # 切分后每段都很小，所以上限放宽了很多（只是防呆，不再卡住长音频）
        if size > AUDIO_MAX_BYTES * 20:
            raise ValueError(f"音频过大 {size/1048576:.0f}MB > 上限 {AUDIO_MAX_BYTES*20/1048576:.0f}MB")
        chunk_sec = int(settings.get("audio_chunk_seconds") or 30)
        temps: list[str] = []
        try:
            # 逐段嵌入。若某段 token 溢出（服务端 500 "Failed to tokenize prompt"），
            # 自动把该段对半再切、直到能过 —— 兜住采样率高/时长卡边界的情况。
            a0, b0 = 0.0, _probe_duration(_ffmpeg(), path) or 0.0
            base = _split_audio(path, chunk_sec)
            queue: list[tuple[float, float, str]] = list(base)
            while queue:
                a, b, f = queue.pop(0)
                if f != path:
                    temps.append(f)
                try:
                    vec = to_bytes(embed_audio(f))
                except Exception as e:
                    msg = str(e)
                    if ("Failed to tokenize" in msg or "500" in msg) and (b - a) > 8:
                        # 太长了 → 对半切，两半重新排队（若已经是原文件则先切一次）
                        mid = (a + b) / 2
                        if f == path:
                            subs = _split_audio(path, max(8, int((b - a) / 2)))
                        else:
                            ff = _ffmpeg()
                            tmpdir = config.DATA_DIR / "tmp"
                            tmpdir.mkdir(parents=True, exist_ok=True)
                            subs = []
                            for k, (sa, sb) in enumerate(((a, mid), (mid, b))):
                                if sb - sa < 0.5:
                                    continue
                                outp = tmpdir / f"a_split_{int(a*10)}_{k}.mp3"
                                subprocess.run(
                                    [ff, "-v", "error", "-y", "-ss", f"{sa:.3f}",
                                     "-t", f"{sb - sa:.3f}", "-i", path, "-vn",
                                     "-c:a", "libmp3lame", "-q:a", "4", str(outp)],
                                    check=True, capture_output=True, timeout=1800)
                                subs.append((sa, sb, str(outp)))
                        queue = subs + queue
                        continue
                    raise
                out.append({"chunk_index": len(out), "modality": "audio",
                            "vec_bytes": vec, "chunk_text": f"{_fmt_time(a)}–{_fmt_time(b)}"})
                if on_chunk:
                    on_chunk(1)
        finally:
            for f in set(temps):        # 临时段文件用完就删，不留垃圾
                if f != path:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        return out

    # text
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"读取失败: {str(e)[:80]}")
    pieces = chunk_text(text)
    if not pieces:
        raise ValueError("空文件")
    vecs = embed_texts(pieces)
    for i, (piece, vec) in enumerate(zip(pieces, vecs)):
        out.append({"chunk_index": i, "modality": "text",
                    "vec_bytes": to_bytes(vec), "chunk_text": piece})
        if on_chunk:
            on_chunk(1)
    return out


# ------------------------------------------------------------------ 主流程

def _run(job_id: str, library_id: int, paths: list[str], recursive: bool) -> None:
    try:
        files, skipped = scanner.scan(paths, recursive=recursive)
        _update(job_id, total=len(files), skipped=skipped[:200])

        known = db.known_fingerprints(library_id)
        done = failed = 0

        for idx, info in enumerate(files, 1):
            # ---- 中断检查：每次循环开头看一眼 ----
            if is_cancelled(job_id):
                _update(job_id, state="cancelled", finished=time.time(), current="",
                        note=f"已手动中断 —— 完成 {done} 个，其余未处理")
                return

            _update(job_id, current=info["name"], done=done, failed_count=failed)
            old = known.get(info["path"])
            if old == info["fingerprint"]:
                done += 1
                _update(job_id, done=done)
                continue

            # ---- 单文件最多试 3 次：连续打请求时偶发 500 / 超时，重试基本能过 ----
            last_err = None
            for attempt in range(3):
                try:
                    if old:                      # 变过了 → 先删旧条目
                        db.delete_items_by_path(library_id, info["path"])
                    pieces = embed_file(info)
                    for p in pieces:
                        db.upsert_item(
                            library_id=library_id, path=info["path"],
                            chunk_index=p["chunk_index"], modality=p["modality"],
                            vec_bytes=p["vec_bytes"], chunk_text=p["chunk_text"],
                            size=info["size"], mtime=info["mtime"],
                            fingerprint=info["fingerprint"])
                    known[info["path"]] = info["fingerprint"]
                    done += 1
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    try:
                        db.rollback()            # 出错必须回滚，否则写锁不放
                    except Exception:
                        pass
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))

            if last_err is not None:
                failed += 1
                with _lock:
                    _jobs[job_id]["failed"].append(
                        {"path": info["path"], "error": str(last_err)[:160],
                         "size": info.get("size", 0), "attempts": 3})
            _update(job_id, done=done, failed_count=failed)

        _update(job_id, state="done", done=done, failed_count=failed,
                finished=time.time(), current="",
                note=f"完成 {done} 个" + (f"，失败 {failed} 个" if failed else ""))

    except Exception as e:
        _update(job_id, state="error", error=str(e)[:300], finished=time.time())


def start_import(library_id: int, paths: list[str], recursive: bool = True) -> str:
    job_id = f"job_{int(time.time() * 1000)}"
    with _lock:
        _jobs[job_id] = {
            "job_id": job_id, "library_id": library_id, "state": "running",
            "total": 0, "done": 0, "failed": [], "failed_count": 0, "skipped": [],
            "current": "", "started": time.time(), "finished": None, "error": None,
            "cancelled": False, "note": "",
        }
    try:
        db.save_job(_jobs[job_id])          # 一开始就落盘，重启后也能看到
    except Exception:
        pass
    threading.Thread(target=_run, args=(job_id, library_id, paths, recursive),
                     daemon=True).start()
    return job_id


# ------------------------------------------------------------------ 维护

def rescan_missing(library_id: int) -> dict:
    """检查库里所有条目的源文件是否还在。"""
    rows = db.all_vectors(library_id, None)
    missing, restored = [], 0
    seen: set[str] = set()
    for r in rows:
        p = r["path"]
        if p in seen:
            continue
        seen.add(p)
        if os.path.exists(p):
            restored += 1
        else:
            db.mark_missing(library_id, p)
            missing.append(p)
    return {"checked": len(seen), "missing": missing, "present": restored}
