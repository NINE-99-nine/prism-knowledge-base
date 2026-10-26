"""虚拟文件：内部自建的「条目」。

用**不大的附加信息**（标题 / 描述 / 配图 / 音频 / 视频）去关联本地的大文件 ——
检索这些轻量信息，就能定位到那个大文件（比如「指环王1」+ 海报 + 剧情简介 → 本地那部 4K）。

设计（详见 ⑩ 方案）：
- 一个虚拟文件的**所有片段共用伪路径** ``prism://virtual/<id>``
  → 「按文件」合并、关键词/语义检索、标签、去重**全部零改动直接复用**
- ``chunk 0`` = **标题**：**不进向量**，只走关键词 —— 标题就是触发关键词的那一路
- ``chunk 1`` = **描述**：文本向量（语义检索）
- ``chunk 2..`` = **配图 / 音频 / 视频**：各自模态的向量
- 媒体**只存真实路径的索引**，绝不复制进 prism 目录

⚠️ 伪路径不是真实存在的文件，所以 rescan / missing / purge 必须跳过它
   （见 db.is_virtual_path），否则会被当成「源文件失效」清掉。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import db
import embedder
import importer

KINDS = ("image", "audio", "video")


def _meta(it: dict) -> dict:
    try:
        m = json.loads(it.get("meta") or "{}")
        return m if isinstance(m, dict) else {}
    except (TypeError, ValueError):
        return {}


def _stat(path: str) -> tuple[int, float]:
    try:
        st = Path(path).stat()
        return int(st.st_size), float(st.st_mtime)
    except OSError:
        return 0, 0.0


def assets_of(library_id: int, vid: int) -> list[dict]:
    """从片段里反推这个虚拟文件的媒体清单。

    🔴 **按源文件去重**：长音频会被切成很多段（5 小时的长音频按 30 秒一段
    就是 600 段），一段列一条的话编辑界面直接被刷屏。所以同一个源文件
    只列一项，另附「共几段」。
    """
    seen: dict[tuple, dict] = {}
    for it in db.virtual_items(library_id, vid):
        m = _meta(it)
        if m.get("kind") in KINDS and m.get("src"):
            key = (m["kind"], m["src"])
            e = seen.get(key)
            if e is None:
                src = m["src"]
                seen[key] = e = {
                    "kind": m["kind"],
                    "src": src,
                    "label": m.get("label") or Path(src).name,
                    "segments": 0,
                    "missing": not Path(src).is_file(),
                }
            e["segments"] += 1
    return list(seen.values())


def get_full(library_id: int, vid: int) -> dict | None:
    """虚拟文件元数据 + 媒体清单 + 关联文件 + 标签（编辑界面 / 抽屉用）。"""
    vf = db.get_virtual(library_id, vid)
    if not vf:
        return None
    vf["assets"] = assets_of(library_id, vid)
    vf["related"] = list(vf.get("related") or [])
    vf["related_is_dir"] = [Path(p).is_dir() for p in vf["related"]]
    vf["related_exist"] = [Path(p).exists() for p in vf["related"]]
    # 标签统一挂在「标题片段」上（标签是片段级概念，虚拟文件得指定一个落点）
    cid = db.virtual_chunk0_id(library_id, vid)
    vf["tag_item_id"] = cid
    vf["tags"] = db.item_tags(library_id, cid) if cid else []
    return vf


def _embed_media(kind: str, src: str) -> list[dict]:
    """嵌入一个媒体文件 → [{modality, vec_bytes, chunk_text}]（可能多段）。

    **统一走 importer.embed_file** —— 那是导入用的同一套逻辑，已经被验证过：
      · 音频按时长切段、某段 token 超限就自动对半再切、临时段用完即删
      · 视频抽 16 帧、撞上下文上限自动降帧重试（整段送上去必被拒）
      · 图片先做格式归一（AVIF/TIFF 转 JPEG）
    绝对不要在这里重写一遍分段逻辑 —— 那会把导入那条好路径的行为带偏。
    """
    info = {"path": src, "modality": kind, "size": os.path.getsize(src)}
    return importer.embed_file(info)


def rebuild(library_id: int, vid: int, *, title: str | None = None,
            description: str | None = None,
            assets: list[dict] | None = None, on_progress=None) -> dict:
    """把虚拟文件的片段重新对齐。

    **内容没变的媒体复用旧向量**，只有新增/换掉的才重新嵌入 ——
    所以只改标题、或只调一下顺序，是秒回的，不会把视频重算一遍。

    assets: [{"kind": "image"|"audio"|"video", "src": 绝对路径, "label": 可选}]
            顺序即 chunk 顺序（前两个 chunk 固定是标题和描述）。
            一个音频可能展开成多段（见 _embed_media），那就在这个位置占多个 chunk。
    """
    vf = db.get_virtual(library_id, vid)
    if not vf:
        raise ValueError("虚拟文件不存在")
    title = vf["title"] if title is None else title
    description = vf["description"] if description is None else description
    if assets is None:
        assets = assets_of(library_id, vid)

    # 旧片段：按 (kind, src) 收下**这一整组**向量，能复用就不重算
    # （音频是多段的，所以存的是列表，不是单个向量）
    old: dict[tuple, list[bytes]] = {}
    for r in db.conn(library_id).execute(
            "SELECT chunk_index, meta, vec FROM items WHERE library_id=? AND path=? "
            "ORDER BY chunk_index", (vf["library_id"], db.vpath(vid))).fetchall():
        try:
            m = json.loads(r["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        if m.get("kind") in KINDS and m.get("src") and r["vec"]:
            old.setdefault((m["kind"], m["src"]), []).append(r["vec"])

    chunks: list[dict] = []
    skipped: list[str] = []
    reused = 0
    embedded = 0

    # chunk 0 —— 标题。**故意不给向量**：标题只走关键词，这是需求本身。
    chunks.append({"chunk_index": 0, "modality": "text", "chunk_text": (title or "").strip(),
                   "vec": None, "kind": "title", "src": "", "size": 0, "mtime": 0.0})

    # chunk 1 —— 描述（文本向量，语义检索靠它）
    desc = (description or "").strip()
    if desc:
        chunks.append({"chunk_index": 1, "modality": "text", "chunk_text": desc,
                       "vec": embedder.to_bytes(embedder.embed_text(desc)),
                       "kind": "description", "src": "", "size": 0, "mtime": 0.0})
        embedded += 1

    # chunk 2.. —— 媒体（一个源文件可能占多段）
    idx = 2
    size, mtime = 0, 0.0
    for a in assets:
        kind = (a.get("kind") or "").strip()
        src = (a.get("src") or "").strip()
        if kind not in KINDS or not src:
            continue
        if not Path(src).is_file():
            skipped.append(f"{Path(src).name}（源文件找不到）")
            continue
        parts = old.get((kind, src))
        if parts:
            reused += 1
        else:
            try:
                parts = [p["vec_bytes"] for p in _embed_media(kind, src)]
                embedded += 1
            except Exception as e:                       # noqa: BLE001
                skipped.append(f"{Path(src).name}（{type(e).__name__}: {str(e)[:90]}）")
                continue
        size, mtime = _stat(src)
        label = a.get("label") or Path(src).name
        for n, vec in enumerate(parts):
            chunks.append({"chunk_index": idx, "modality": kind, "chunk_text": "",
                           "vec": vec, "kind": kind, "src": src,
                           "label": label, "seg": n, "segs": len(parts),
                           "size": size, "mtime": mtime})
            idx += 1
        if on_progress:
            on_progress(idx - 1, len(assets) + 2)

    # 封面：没指定就用第一张配图（前端展示用，不改数据）
    cover = (vf.get("cover") or "").strip()
    if not cover:
        first_img = next((c["src"] for c in chunks if c["kind"] == "image"), "")
        if first_img:
            db.update_virtual(library_id, vid, cover=first_img)

    # 整体重写片段：先清空这条伪路径，再按新编排写进去 —— 不会留下半截旧片段
    db.conn(library_id).execute("DELETE FROM items WHERE library_id=? AND path=?",
                                (vf["library_id"], db.vpath(vid)))
    db.conn(library_id).commit()
    for c in chunks:
        db.upsert_item(
            vf["library_id"], db.vpath(vid), c["chunk_index"], c["modality"],
            c["vec"], c["chunk_text"], c["size"], c["mtime"], "",
            {"virt": True, "vid": vid, "kind": c["kind"], "src": c["src"],
             "label": c.get("label", ""), "seg": c.get("seg"), "segs": c.get("segs")},
        )

    db.update_virtual(library_id, vid, title=(title or "").strip(), description=desc)
    return {"vid": vid, "chunks": len(chunks), "embedded": embedded,
            "reused": reused, "skipped": skipped}


def _norm(p: str) -> str:
    try:
        return str(Path(p).resolve()).replace("\\", "/").lower()
    except OSError:
        return str(p).replace("\\", "/").lower()


def path_is_asset(path: str) -> bool:
    """这个真实路径是不是**某个虚拟文件挂过的东西**（配图/音视频，或它关联的本地文件）。

    用途：``/api/file`` 代转发和「打开文件」都只放行**已索引**的路径；
    而虚拟文件的媒体挂的是真实路径、并不在 items.path 里（那儿是伪路径），
    所以这两个接口必须额外放行它们 —— 否则封面图 403、关联文件打不开。
    """
    want = _norm(path)
    for lid in db.all_library_ids():          # 一个库一个文件 → 得逐库问
        try:
            if want in db.virtual_asset_paths(lid):
                return True
        except Exception:
            continue
    return False


def create(library_id: int, title: str, description: str = "",
           assets: list[dict] | None = None,
           related: list[str] | None = None) -> dict:
    """新建虚拟文件。先把元数据落库拿到 id，再按伪路径写片段。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能为空（它是关键词检索的触发词）")
    vid = db.create_virtual(library_id, title, description or "", "",
                            [p for p in (related or []) if p])
    r = rebuild(library_id, vid, title=title, description=description or "", assets=assets or [])
    return {"vid": vid, **r}


def update(library_id: int, vid: int, *, title=None, description=None, assets=None,
           related=None, cover=None) -> dict:
    """改虚拟文件。**媒体没变的不重算向量**（见 rebuild）。"""
    if db.get_virtual(library_id, vid) is None:
        raise ValueError("虚拟文件不存在")
    if related is not None:
        db.update_virtual(library_id, vid, related=[p for p in related if p])
    if cover is not None:
        db.update_virtual(library_id, vid, cover=cover)
    return rebuild(library_id, vid, title=title, description=description, assets=assets)


def delete(library_id: int, vid: int) -> dict:
    if db.get_virtual(library_id, vid) is None:
        raise ValueError("虚拟文件不存在")
    db.delete_virtual(library_id, vid)
    return {"ok": True, "vid": vid}
