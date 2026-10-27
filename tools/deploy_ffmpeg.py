"""把 ffmpeg 部署到本项目的虚拟环境里（不依赖系统安装、不污染 git）。

用法：
    .venv/Scripts/python.exe tools/deploy_ffmpeg.py            # 自动挑源下载
    .venv/Scripts/python.exe tools/deploy_ffmpeg.py --url URL  # 指定下载地址
    .venv/Scripts/python.exe tools/deploy_ffmpeg.py --check    # 只检查，不下载

落点：<项目>/.venv/ffmpeg/bin/ffmpeg.exe
     · 在 .venv 里 —— 属于"这套自包含软件"自己的东西
     · .venv 已被 .gitignore 忽略，所以发布包里不会有它
     · 重新跑 安装.bat 会把 .venv 重建成空的 → 需要时再部署一次（脚本会提示）

成功判据不是"文件存在"，而是**跑得起来**：执行 `ffmpeg -version` 能拿到版本号。
输出协议（给服务端/批处理读）：每行 `PROGRESS <百分比> <说明>`
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / ".venv" / "ffmpeg" / "bin" / "ffmpeg.exe"

# 下载源（按顺序试）。只取单文件 ffmpeg.exe，不装 ffplay/ffprobe。
SOURCES = [
    # gyan.dev essentials（单 exe 约 80~100 MB，Windows 上最常用的稳定构建）
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    # BtbN 的 GitHub release（备用）
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
]


def say(pct: int, msg: str) -> None:
    print(f"PROGRESS {int(pct)} {msg}", flush=True)


def _proxy() -> dict | None:
    """支持 PRISM_PROXY（例如 http://127.0.0.1:7890）；否则用系统代理。"""
    p = (os.environ.get("PRISM_PROXY") or "").strip()
    if p:
        return {"http": p, "https": p}
    return urllib.request.getproxies() or None


def download(url: str, dest: Path) -> None:
    proxy = _proxy()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxy) if proxy else urllib.request.BaseHandler())
    req = urllib.request.Request(url, headers={"User-Agent": "prism-deployer/1.0"})
    with opener.open(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        last = -1
        with open(dest, "wb") as f:
            while True:
                buf = r.read(1024 * 256)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                pct = int(done * 100 / total) if total else min(90, done // (1024 * 1024))
                if pct != last:
                    last = pct
                    say(pct, f"下载中 {done/1048576:.1f}/{total/1048576:.1f} MB" if total
                        else f"下载中 {done/1048576:.1f} MB")
    if total and dest.stat().st_size != total:
        raise RuntimeError(f"下载不完整：{dest.stat().st_size}/{total} 字节")


def extract(archive: Path, dest: Path) -> None:
    say(92, "解压中")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        name = next((n for n in z.namelist()
                     if n.lower().endswith("/bin/ffmpeg.exe") or n.lower() == "ffmpeg.exe"), None)
        if not name:
            raise RuntimeError("这个压缩包里找不到 ffmpeg.exe")
        with z.open(name) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def verify(exe: Path) -> str:
    """真正跑一次 -version；成功才算部署成功。"""
    say(96, "验证可执行")
    r = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"部署后跑不起来：{(r.stderr or r.stdout or '')[:200]}")
    first = (r.stdout or "").splitlines()[0] if r.stdout else ""
    return first.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="指定下载地址（默认自动挑源）")
    ap.add_argument("--dest", default=str(DEST))
    ap.add_argument("--check", action="store_true", help="只检查是否已部署，不下载")
    a = ap.parse_args()
    dest = Path(a.dest)

    if a.check:
        ok = dest.is_file() and verify_quiet(dest)
        print(json.dumps({"deployed": bool(ok), "path": str(dest)}, ensure_ascii=False))
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="prism-ffmpeg-"))
    forced = (os.environ.get("PRISM_FFMPEG_URL") or "").strip()   # 内网镜像 / 离线包用
    urls = [a.url] if a.url else ([forced] if forced else SOURCES)
    errs = []
    try:
        for url in urls:
            archive = tmpdir / "ffmpeg.zip"
            try:
                say(1, f"从 {url.split('/')[2]} 下载")
                download(url, archive)
                extract(archive, dest)
                version = verify(dest)
                say(100, f"完成：{version[:80]}")
                print(json.dumps({"ok": True, "path": str(dest), "version": version},
                                 ensure_ascii=False))
                return 0
            except Exception as e:                       # noqa: BLE001
                errs.append(f"{url.split('/')[2]}: {type(e).__name__}: {e}")
                say(0, f"这个源失败，换下一个（{type(e).__name__}）")
                archive.unlink(missing_ok=True)
        say(0, "所有源都失败")
        print(json.dumps({"ok": False, "errors": errs}, ensure_ascii=False))
        return 2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def verify_quiet(exe: Path) -> bool:
    try:
        r = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
