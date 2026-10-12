"""Prism · 棱镜 — 路径扫描与指纹

把用户拖进来的「文件 / 文件夹」展开成待索引的文件清单。
指纹 = path + mtime + size，用来判断文件有没有变过（增量导入的依据）。
"""
import hashlib
import os
from pathlib import Path

from config import classify


def fingerprint(path) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    raw = f"{os.path.abspath(path)}|{int(st.st_mtime)}|{st.st_size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def file_info(path) -> dict | None:
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    if not p.is_file():
        return None
    return {
        "path": str(p.resolve()),
        "name": p.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "modality": classify(p),
        "fingerprint": fingerprint(p),
    }


def scan(paths, recursive: bool = True, include_ext=None) -> tuple[list[dict], list[dict]]:
    """展开路径列表。

    返回 (文件清单, 跳过清单)。
    跳过项带 reason：not_found / not_file / unreadable / unsupported / filtered

    include_ext：只收这些后缀（如 [".txt", ".md"]）。
                 写 "txt" 还是 ".txt" 都认（统一归一化）。
                 None 或空 = 不筛 —— 按 config 支持的类型全收。
    """
    files: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()

    exts = {"." + str(e).strip().lstrip(".").lower()
            for e in (include_ext or []) if str(e).strip()}

    for raw in paths or []:
        p = Path(str(raw).strip().strip('"'))
        if not p.exists():
            skipped.append({"path": str(p), "reason": "not_found"})
            continue

        if p.is_file():
            candidates = [p]
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            candidates = [q for q in it if q.is_file()]
        else:
            skipped.append({"path": str(p), "reason": "not_file"})
            continue

        for q in candidates:
            info = file_info(q)
            if info is None:
                skipped.append({"path": str(q), "reason": "unreadable"})
                continue
            if info["modality"] == "other" or info["size"] == 0:
                skipped.append({"path": info["path"], "reason": "unsupported"})
                continue
            if exts and q.suffix.lower() not in exts:
                skipped.append({"path": info["path"], "reason": "filtered"})
                continue
            if info["path"] in seen:
                continue
            seen.add(info["path"])
            files.append(info)

    return files, skipped
