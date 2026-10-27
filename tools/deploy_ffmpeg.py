"""把 ffmpeg 部署到本项目的虚拟环境里（不依赖系统安装、不污染 git）。

用法：
    .venv/Scripts/python.exe tools/deploy_ffmpeg.py            # 自动挑源下载
    .venv/Scripts/python.exe tools/deploy_ffmpeg.py --url URL  # 指定下载地址
    .venv/Scripts/python.exe tools/deploy_ffmpeg.py --check    # 只检查，不下载

落点：<项目>/.venv/ffmpeg/bin/ffmpeg.exe
     · 在 .venv 里 —— 属于"这套自包含软件"自己的东西
     · .venv 已被 .gitignore 忽略，所以发布包里不会有它
     · 重新跑 安装.bat 会把 .venv 重建成空的 → 需要时再部署一次

踩过的坑，都在这里修掉了：
  1) 不以"文件存在"判成功 —— 必须跑一次 `ffmpeg -version` 拿到版本号
  2) 不假设用户有梯子 —— 几个源各试 256 KB 测速，挑最快的下，其余排后面做备用
  3) 断了不白断 —— 按 HTTP Range 从断点续传，一个源最多试 5 次；一个源真死了才换下一个
  4) 国内下不动还有一条路 —— pip 镜像里的 imageio-ffmpeg（自带 ffmpeg 二进制，约 30 MB，
     走清华/官方 PyPI，pip 自己带哈希校验），最后才需要手动放文件

输出协议（给服务端/批处理读）：每行 `PROGRESS <百分比> <说明>`
"""
from __future__ import annotations

import argparse
import hashlib
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
UA = "prism-deployer/1.2"

# 下载源：名字 → 地址。
# 第一个是官方精简构建（体积最小，旁边有 .sha256 校验文件）；后三个是同一个 GitHub 包的三条通路。
SOURCES = [
    ("gyan.dev 官方精简构建",
     "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("ghfast.top 加速",
     "https://ghfast.top/https://github.com/BtbN/FFmpeg-Builds/releases/"
     "latest/download/ffmpeg-master-latest-win64-gpl.zip"),
    ("gh-proxy.com 加速",
     "https://gh-proxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/"
     "latest/download/ffmpeg-master-latest-win64-gpl.zip"),
    ("GitHub 直连",
     "https://github.com/BtbN/FFmpeg-Builds/releases/"
     "latest/download/ffmpeg-master-latest-win64-gpl.zip"),
]

# pip 兜底（和 安装.bat 选的镜像一致：先清华，再官方）
PIP_MIRRORS = ["https://pypi.tuna.tsinghua.edu.cn/simple", "https://pypi.org/simple"]

READ_TIMEOUT = 30          # 单次读超时（秒）：断了就续传，不整包重下
PROBE_BYTES = 256 * 1024   # 测速取样大小
MAX_TRIES = 5              # 同一个源最多试几次（每次从断点续传）


def say(pct: int, msg: str) -> None:
    print(f"PROGRESS {int(pct)} {msg}", flush=True)


def _opener():
    """支持 PRISM_PROXY；没设就用系统代理 —— 不强行假设用户开了梯子。"""
    p = (os.environ.get("PRISM_PROXY") or "").strip()
    if p:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener()


def probe(url: str, opener) -> float:
    """取 256 KB 看这条通路能跑多快（字节/秒）；连不上返回 0。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Range": f"bytes=0-{PROBE_BYTES - 1}"})
        t0 = time.time()
        with opener.open(req, timeout=10) as r:
            got = len(r.read(PROBE_BYTES))
        return got / max(time.time() - t0, 0.001)
    except Exception:                                      # noqa: BLE001
        return 0.0


def download(url: str, dest: Path, opener) -> None:
    """下载到 dest；中断按 Range 续传，同一源最多 MAX_TRIES 次。"""
    total = 0
    for attempt in range(1, MAX_TRIES + 1):
        done = dest.stat().st_size if dest.exists() else 0
        headers = {"User-Agent": UA}
        if done:
            headers["Range"] = f"bytes={done}-"
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=READ_TIMEOUT) as r:
                if getattr(r, "status", 200) == 200:        # 不支持续传 → 从头写
                    done = 0
                    total = int(r.headers.get("Content-Length") or 0)
                    mode = "wb"
                else:                                       # 206：接着上次位置写
                    cr = r.headers.get("Content-Range") or ""
                    total = (int(cr.rsplit("/", 1)[-1]) if "/" in cr
                             else done + int(r.headers.get("Content-Length") or 0))
                    mode = "ab"
                last_pct = -1
                with open(dest, mode) as f:
                    while True:
                        buf = r.read(512 * 1024)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        pct = int(done * 100 / total) if total else min(90, done // (1024 * 1024))
                        if pct != last_pct:
                            last_pct = pct
                            tag = "（续传）" if attempt > 1 else ""
                            say(pct, f"下载中 {done/1048576:.1f}/{total/1048576:.1f} MB {tag}".rstrip())
        except Exception as e:                              # noqa: BLE001
            if attempt >= MAX_TRIES:
                raise RuntimeError(f"{type(e).__name__}: {e}") from e
            wait = min(2 * attempt, 8)
            say(0, f"连接断了（{type(e).__name__}），{wait} 秒后从断点续传…")
            time.sleep(wait)
            continue
        if total and done < total:
            if attempt >= MAX_TRIES:
                raise RuntimeError(f"下载不完整：{done}/{total} 字节")
            say(0, f"只收到 {done/1048576:.1f}/{total/1048576:.1f} MB，续传…")
            continue
        return


def check_sha256(archive: Path, sidecar_url: str, opener) -> None:
    """有官方 .sha256 就对一下；拿不到校验文件就跳过（不因为侧车 404 就判失败）。"""
    try:
        req = urllib.request.Request(sidecar_url, headers={"User-Agent": UA})
        with opener.open(req, timeout=15) as r:
            want = r.read(4096).decode("utf-8", "replace").split()[0].strip().lower()
        if len(want) != 64:
            return
        h = hashlib.sha256()
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest().lower() != want:
            raise RuntimeError("SHA-256 对不上（包可能在传输中被改过）")
        say(91, "SHA-256 校验通过")
    except RuntimeError:
        raise
    except Exception:                                       # noqa: BLE001
        pass


def extract(archive: Path, dest: Path, marker: str = "/bin/ffmpeg.exe") -> None:
    """从 zip / wheel 里把 ffmpeg 那个 exe 掏出来。"""
    say(93, "解压中")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        name = next((n for n in z.namelist()
                     if n.lower().endswith(".exe")
                     and (marker in n.lower() or n.lower().endswith("ffmpeg.exe"))), None)
        if not name:
            raise RuntimeError("这个包里找不到 ffmpeg.exe")
        with z.open(name) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, length=1024 * 1024)


def verify(exe: Path) -> str:
    """真正跑一次 -version；跑得起来才算部署成功。"""
    say(97, "验证可执行")
    r = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"部署后跑不起来：{(r.stderr or r.stdout or '')[:200]}")
    return ((r.stdout or "").splitlines() or [""])[0].strip()


def verify_quiet(exe: Path) -> bool:
    try:
        return subprocess.run([str(exe), "-version"], capture_output=True,
                              timeout=60).returncode == 0
    except Exception:                                       # noqa: BLE001
        return False


def via_pip(dest: Path, tmpdir: Path) -> str:
    """兜底：从 pip 镜像下 imageio-ffmpeg（内含 ffmpeg 二进制），把里面的 exe 掏出来。

    国内网络这条通常最快：pip 镜像速度快，且 pip 自己会做哈希校验。
    """
    pkg = tmpdir / "pipdl"
    pkg.mkdir(exist_ok=True)
    for idx in PIP_MIRRORS:
        say(2, f"改用 pip 镜像：{idx.split('/')[2]}")
        r = subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps",
                            "--disable-pip-version-check", "--timeout", "30", "-d", str(pkg),
                            "imageio-ffmpeg", "-i", idx],
                           capture_output=True, text=True)
        if r.returncode != 0:
            continue
        whl = next(iter(sorted(pkg.glob("imageio_ffmpeg-*.whl"))), None)
        if not whl:
            continue
        extract(whl, dest, marker="/binaries/ffmpeg-win")
        return verify(dest)
    raise RuntimeError("pip 镜像也拿不到 imageio-ffmpeg")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="指定下载地址（默认自动测速挑源）")
    ap.add_argument("--dest", default=str(DEST))
    ap.add_argument("--check", action="store_true", help="只检查是否已部署，不下载")
    a = ap.parse_args()
    dest = Path(a.dest)

    if a.check:
        print(json.dumps({"deployed": dest.is_file() and verify_quiet(dest), "path": str(dest)},
                         ensure_ascii=False))
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    opener = _opener()
    forced = (os.environ.get("PRISM_FFMPEG_URL") or "").strip()   # 内网镜像 / 离线包
    if a.url:
        plan = [("指定的地址", a.url)]
    elif forced:
        plan = [("PRISM_FFMPEG_URL", forced)]
    else:
        say(1, "先测速：每个源试 256 KB，挑最快的那个下")
        scored = []
        for name, url in SOURCES:
            speed = probe(url, opener)
            scored.append((speed, name, url))
            say(1, f"  {name}：" + (f"{speed/1048576:.2f} MB/s" if speed else "连不上"))
        scored.sort(key=lambda x: -x[0])
        plan = [(n, u) for s, n, u in scored]

    tmpdir = Path(tempfile.mkdtemp(prefix="prism-ffmpeg-"))
    errs = []
    try:
        for name, url in plan:
            archive = tmpdir / "ffmpeg.zip"
            archive.unlink(missing_ok=True)
            try:
                say(1, f"从 {name} 下载")
                download(url, archive, opener)
                if not archive.is_file() or archive.stat().st_size < 1024 * 1024:
                    raise RuntimeError("下到的包太小，不像 ffmpeg 压缩包")
                if url.endswith(".zip"):
                    check_sha256(archive, url + ".sha256", opener)
                extract(archive, dest)
                version = verify(dest)
                say(100, f"完成：{version[:70]}")
                print(json.dumps({"ok": True, "path": str(dest), "version": version,
                                  "source": name}, ensure_ascii=False))
                return 0
            except Exception as e:                          # noqa: BLE001
                errs.append(f"{name}: {type(e).__name__}: {e}")
                say(0, f"{name} 不行（{type(e).__name__}），换下一个源")

        # 直连源全废 → 走 pip 镜像（国内往往这条才通）
        if not a.url and not forced:
            try:
                version = via_pip(dest, tmpdir)
                say(100, f"完成：{version[:70]}")
                print(json.dumps({"ok": True, "path": str(dest), "version": version,
                                  "source": "pip 镜像里的 imageio-ffmpeg"}, ensure_ascii=False))
                return 0
            except Exception as e:                          # noqa: BLE001
                errs.append(f"pip: {type(e).__name__}: {e}")
                say(0, f"pip 镜像也不行（{type(e).__name__}）")

        say(0, "所有下载方式都失败")
        say(0, "提示：本机若有代理，设环境变量 PRISM_PROXY=http://127.0.0.1:7890 再试；"
               f"也可以手动把 ffmpeg.exe 放到 {dest}")
        print(json.dumps({"ok": False, "errors": errs}, ensure_ascii=False))
        return 2
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
