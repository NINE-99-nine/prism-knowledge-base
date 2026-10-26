"""Prism · 棱镜 — 向量检索

纯向量 top-k：把库里的向量读进内存做点积（万级 → 毫秒级）。
不调用 LLM，因此免费、离线、可预测。
"""
import numpy as np

import config
from config import EMBED_DIM as _DEFAULT_DIM
import settings
from embedder import embed_text, from_blob, normalize
import db


def _dim() -> int:
    """当前配置的向量维度（可在设置里改）。"""
    try:
        return int(settings.get("embed_dim") or _DEFAULT_DIM)
    except (TypeError, ValueError):
        return _DEFAULT_DIM


def _hit_tags(library_id: int, path: str, item_id: int) -> list[str]:
    """取这条命中该显示的标签。

    虚拟文件的标签统一挂在 chunk 0（标题片段）上，所以要转一手 ——
    不这样做的话，语义命中（描述/配图片段）会看不到标签。
    """
    vid = db.vid_of_path(path)
    if vid is not None:
        cid = db.virtual_chunk0_id(library_id, vid)
        if cid:
            return db.item_tags(library_id, cid)
    return db.item_tags(library_id, item_id)


def _virt_meta(library_id: int, path: str) -> dict | None:
    """虚拟文件：把伪路径 prism://virtual/<id> 换成人类可读的标题 / 封面 / 关联文件。

    前端拿到 virt 就显示标题和封面图，而不是那串 prism:// 伪路径。
    """
    vid = db.vid_of_path(path)
    if vid is None:
        return None
    vf = db.get_virtual(library_id, vid)
    if not vf:
        return None
    return {
        "vid": vid,
        "title": vf.get("title") or "",
        "cover": vf.get("cover") or "",
        "description": vf.get("description") or "",
        "related": vf.get("related") or [],
    }


def _score_rows(library_id: int, query_vec: np.ndarray, rows, top_k: int) -> list[dict]:
    if not rows:
        return []
    M = np.vstack([from_blob(r["vec"]) for r in rows])
    dim = _dim()
    if M.shape[1] != dim:
        # 维度不符的条目（换过模型）直接跳过，避免整批崩掉
        ok = [i for i, r in enumerate(rows)
              if len(r["vec"]) == dim * 2]
        rows = [rows[i] for i in ok]
        M = np.vstack([from_blob(r["vec"]) for r in rows]) if ok else np.zeros((0, dim))
        if M.size == 0:
            return []
    M = normalize(M)
    q = normalize(query_vec)
    sims = M @ q
    order = np.argsort(-sims)[:max(1, min(top_k, len(sims)))]
    out = []
    for i in order:
        r = rows[int(i)]
        out.append({
            "id": r["id"],
            "path": r["path"],
            "chunk_index": r["chunk_index"],
            "modality": r["modality"],
            "text": r["chunk_text"] or "",
            "score": float(sims[int(i)]),
            "tags": _hit_tags(library_id, r["path"], r["id"]),
            "virt": _virt_meta(library_id, r["path"]),
        })
    return out


def search_text(library_id: int, query: str, top_k: int = 12,
                modality: str | None = None, group: bool = False) -> list[dict]:
    """文本查询 → 跨模态检索（文本向量与图像/音频向量在同一空间）。

    group=True 时把结果按文件折叠（一个文件一条，带命中明细）。
    注意：向量层不变 —— 仍是逐段比对，折叠只是结果呈现层的后处理。
    """
    rows = db.all_vectors(library_id, modality)
    # 折叠会大量减少条数，所以先取更宽的候选池，否则折完只剩两三条
    n = max(top_k * 4, top_k) if group else top_k
    hits = _score_rows(library_id, np.asarray(embed_text(query), dtype=np.float32), rows, n)
    return group_by_file(hits, top_k) if group else hits


def _row_to_hit(library_id: int, r, score: float) -> dict:
    hit = {
        "id": r["id"],
        "path": r["path"],
        "chunk_index": r["chunk_index"],
        "modality": r["modality"],
        "text": r.get("text") or r.get("chunk_text") or "",
        "score": float(score),
        "tags": _hit_tags(library_id, r["path"], r["id"]),
        "virt": _virt_meta(library_id, r["path"]),
    }
    # 虚拟文件的图片/音视频片段本身没有正文，卡片上就用描述兜底
    if hit["virt"] and not hit["text"]:
        hit["text"] = hit["virt"].get("description") or ""
    return hit


def search_keyword(library_id: int, query: str, top_k: int = 12,
                   modality: str | None = None, group: bool = False) -> list[dict]:
    """朴素子串匹配（不做语义），按「命中在哪」排序。

    排序权重：文件名命中 > 路径命中 > 正文命中，同级再按命中次数。
    专治向量答不好的查询 —— 确切名字、文件后缀、你记得的一句原话。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    rows = db.search_keyword_rows(library_id, query, modality)
    scored = []
    for r in rows:
        path = r.get("path") or ""
        name = path.replace("/", "\\").split("\\")[-1]
        text = r.get("chunk_text") or ""
        n_name = name.lower().count(q)
        n_path = path.lower().count(q)
        n_text = text.lower().count(q)
        n_tag = 1 if r.get("tag_hit") else 0          # 标签命中（用户手打的，权重给足）
        if not (n_name or n_path or n_text or n_tag):
            continue
        rank = ((3 if n_name else 0) + (2 if n_path else 0)
                + (1 if n_text else 0) + (2 if n_tag else 0))
        # 分数做成向量量级（0~1），且普遍高于语义分，混合时自然置顶
        score = min(0.999, 0.50 + 0.10 * rank + 0.01 * min(n_name + n_path + n_text, 5))
        scored.append((rank, n_name + n_path + n_text, path, r, score))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    n = max(top_k * 4, top_k) if group else top_k
    hits = [_row_to_hit(library_id, r, sc) for _, _, _, r, sc in scored[:n]]
    return group_by_file(hits, top_k) if group else hits


def search_mixed(library_id: int, query: str, top_k: int = 12,
                 modality: str | None = None, group: bool = False) -> list[dict]:
    """混合检索：关键词命中置顶 + 语义结果补足（按文件去重）。

    默认模式。搜确切名字时关键词那一路把正确答案顶到前面；
    搜概念（"写小说的技巧"）时关键词多半没命中，结果就全是语义排序，
    行为与改造前一致 —— 两种问法都不会变差。
    """
    kw = search_keyword(library_id, query, top_k, modality, group)
    if len(kw) >= top_k:
        return kw[:top_k]

    sem = search_text(library_id, query, top_k, modality, group)
    seen = {h["path"] for h in kw}
    out = list(kw)
    for h in sem:
        if h["path"] in seen:
            continue
        out.append(h)
        seen.add(h["path"])
        if len(out) >= top_k:
            break
    return out


def filter_by_tags(hits: list[dict], library_id: int,
                   tags: list[str] | None = None,
                   not_tags: list[str] | None = None) -> list[dict]:
    """按标签过滤：tags = 必须全有（AND），not_tags = 一个都不能有（NOT）。

    标签只挂在**段**上，但用户想的是"这个文件"，所以按 path 判定。
    两个都为空 → 原样返回，不做任何事（默认不干扰检索）。
    """
    allow = db.path_tag_sets(library_id, tags, not_tags)
    if allow is None:
        return hits
    return [h for h in hits if h.get("path") in allow]


def filter_virtual(hits: list[dict], only: bool) -> list[dict]:
    """「只看虚拟文件」这一类的过滤。

    虚拟文件的各片段本身的 modality 是 text/image/audio/video，所以**不能**
    当成第五种模态去等值过滤 —— 只能按伪路径前缀筛。
    """
    if not only:
        return hits
    return [h for h in hits if db.is_virtual_path(h.get("path"))]


def search_any(library_id: int, query: str, top_k: int = 12,
               modality: str | None = None, group: bool = False,
               mode: str = "mixed", tags: list[str] | None = None,
               not_tags: list[str] | None = None) -> list[dict]:
    """检索入口：按模式分派。mode = semantic / keyword / mixed。

    modality 传 "virt" 时是「只看虚拟文件」—— 见 filter_virtual。
    tags = 必须全有；not_tags = 一个都不能有。带标签过滤时先多取候选，
    过滤完再截到 top_k —— 否则一过滤就只剩两三条，用户会以为"库里没有"。
    """
    m = (mode or "mixed").lower()
    only_virt = modality == "virt"
    mod = None if only_virt else modality
    n = max(top_k * 4, top_k) if (tags or not_tags or only_virt) else top_k
    if m == "keyword":
        out = search_keyword(library_id, query, n, mod, group)
    elif m == "semantic":
        out = search_text(library_id, query, n, mod, group)
    else:
        out = search_mixed(library_id, query, n, mod, group)
    out = filter_virtual(out, only_virt)
    return filter_by_tags(out, library_id, tags, not_tags)[:top_k]


def group_by_file(rows: list[dict], top_k: int, per_file_chunks: int = 5) -> list[dict]:
    """把段级结果折叠成文件级：同一个文件的多段命中合并为一条。

    为什么需要：一个 1 万字的文件有 20 段，若有多段同时命中，
    不折叠就会白占多个结果位（12 个位置里可能只有 4 个不同文件）。

    代表分 = 该文件命中的**最高段分** —— 是代表值，不是文件的整体相似度。
    """
    if not rows:
        return []

    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(r["path"], []).append(r)

    out: list[dict] = []
    for path, hits in buckets.items():
        hits.sort(key=lambda x: x["score"], reverse=True)
        best = hits[0]
        out.append({
            # 保持与段级结果同名的字段，前端可复用（id/chunk_index 指向最高分那段）
            "id": best["id"],
            "path": path,
            "chunk_index": best["chunk_index"],
            "modality": best["modality"],
            "text": best["text"],
            "score": best["score"],
            "tags": best["tags"],
            "virt": best.get("virt"),
            # 新增：文件级信息
            "grouped": True,
            "hits": len(hits),
            "chunks": [
                {"id": h["id"], "chunk_index": h["chunk_index"],
                 "text": h["text"], "score": h["score"]}
                for h in hits[:max(1, per_file_chunks)]
            ],
        })

    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:max(1, top_k)]


def search_by_vector(library_id: int, vec, top_k: int = 12,
                     modality: str | None = None,
                     tags: list[str] | None = None,
                     not_tags: list[str] | None = None) -> list[dict]:
    """拿一个现成的向量去库里搜（拖拽文件、以图搜图、任意模态互搜都走这里）。"""
    rows = db.all_vectors(library_id, modality)
    n = max(top_k * 4, top_k) if (tags or not_tags) else top_k
    hits = _score_rows(library_id, np.asarray(vec, dtype=np.float32), rows, n)
    return filter_by_tags(hits, library_id, tags, not_tags)[:top_k]


def search_by_image(library_id: int, image_path: str, top_k: int = 12) -> list[dict]:
    """以图搜图 / 以图搜库。"""
    from embedder import embed_image
    rows = db.all_vectors(library_id, None)
    return _score_rows(library_id, np.asarray(embed_image(image_path), dtype=np.float32), rows, top_k)


def similar_files(library_id: int, item_id: int, top_k: int = 12,
                  block: int = 1024, max_src: int = 200) -> list[dict]:
    """找「像这个文件」的**其它文件**（文件级，不是片段级）。

    用户问「像这张的还有吗」，问的是**文件**。所以：

      两个文件的相似度 = 它们所有片段两两余弦的**最大值**

    两点理由：
      · **为什么用全部片段**：一个文件可能有几十个片段（长文档切段、或虚拟文件的
        标题/描述/配图/音频/视频），只挑其中一个片段去比会漏掉真正像的地方。
      · **为什么取最大值而不是平均值**：只要**有一处很像**就算像；平均会被大量
        不相关的片段稀释掉（一个 5 小时音频里有 1 分钟和它很像，就该算像）。

    结果按 path 聚合，一条文件只出一次，带它的最高分和对应片段。
    分块算 Q @ M.T 是为了别一次开出「源片段数 × 全库片段数」的矩阵。
    """
    item = db.get_item(library_id, item_id)
    if not item:
        return []
    src_path = item["path"]
    rows = db.all_vectors(library_id, None)
    if not rows:
        return []

    # 源文件自己的全部片段（虚拟文件的「标题」片段没有向量，会被 all_vectors 自动排除）
    src_vecs = [r["vec"] for r in rows if r["path"] == src_path][:max_src]
    if not src_vecs:
        if not item.get("vec"):                 # 退路：至少用被点的那一条
            return []
        src_vecs = [item["vec"]]
    Q = normalize(np.vstack([from_blob(b) for b in src_vecs]))

    best: dict[str, dict] = {}
    paths = [r["path"] for r in rows]
    n = len(rows)
    for s in range(0, n, block):
        e = min(s + block, n)
        M = normalize(np.vstack([from_blob(rows[i]["vec"]) for i in range(s, e)]))
        sims = Q @ M.T                          # (源片段数) × (本块片段数)
        col = sims.max(axis=0)                  # 每个目标片段：源里最像它的那一处
        for j in range(e - s):
            p = paths[s + j]
            if p == src_path:                   # 自己不算自己的相似文件
                continue
            sc = float(col[j])
            cur = best.get(p)
            if cur is None or sc > cur["score"]:
                best[p] = {"row": rows[s + j], "score": sc}

    out: list[dict] = []
    for x in sorted(best.values(), key=lambda v: -v["score"])[:max(1, top_k)]:
        r = x["row"]
        hit = {
            "id": r["id"], "path": r["path"], "chunk_index": r["chunk_index"],
            "modality": r["modality"], "text": r["chunk_text"] or "",
            "score": x["score"], "tags": _hit_tags(library_id, r["path"], r["id"]),
            "virt": _virt_meta(library_id, r["path"]),
        }
        if hit["virt"] and not hit["text"]:
            hit["text"] = hit["virt"].get("description") or ""
        out.append(hit)
    return out


def similar_to_item(library_id: int, item_id: int, top_k: int = 12) -> list[dict]:
    """兼容旧名字 —— 实际走文件级比较（见 similar_files）。"""
    return similar_files(library_id, item_id, top_k)


def duplicates(library_id: int, threshold: float = 0.97, limit: int = 50,
               same_file: bool = False, force: bool = False,
               block: int = 512) -> dict:
    """近似重复检测：两两相似度超阈值的配对。

    两个必须做的过滤（否则结果毫无意义）：
      1. **排除同一个文件的不同段** —— 一个文件切成 N 段后，段与段本来就高度相似，
         不排掉就会出现"文件自己和自己重复"的荒唐结果。
      2. **同一对文件只保留最高分** —— A 文件 5 段 × B 文件 4 段会产生 20 个组合，
         不去重的话同一对文件会刷屏。

    same_file=True 时才保留同文件配对（想看"这个文件内部有没有重复段落"时用）。

    🔴 规模护栏：这是 O(n²)，内存随 n² 涨（实测 16,684 段 ≈ 1.1 GB、50 秒以上）。
       段数超过 config.DUPES_MAX_ITEMS 就不直接跑，先返回 blocked=True 让前端问一声，
       force=True 才真算 —— 免得手一滑点进来就卡死。
    🔴 分块计算：`M @ M.T` 会一次性开出 n×n 矩阵；改成按 block 行分块相乘，
       峰值内存从 n² 降到 block×n（16,684 段时约 34 MB），大库能跑完而不是炸掉。
    """
    n_total = db.count_vectors(library_id)
    out = {
        "total": n_total,
        "limit": int(config.DUPES_MAX_ITEMS),
        "blocked": False,
        "est_seconds": 0,
        "count": 0,
        "pairs": [],
    }
    if n_total > config.DUPES_MAX_ITEMS and not force:
        out["blocked"] = True
        out["est_seconds"] = max(2, int(n_total * n_total / 5.6e6))   # 经验值：16,684 段 ≈ 50 秒
        return out
    if n_total < 2:
        return out

    rows = db.all_vectors(library_id, None)
    M = normalize(np.vstack([from_blob(r["vec"]) for r in rows]))
    paths = [r["path"] for r in rows]
    n = len(rows)
    best: dict[tuple, dict] = {}

    for s in range(0, n, block):
        e = min(s + block, n)
        sims = M[s:e] @ M.T                     # 只有这一块，(e-s) × n
        for li in range(e - s):
            i = s + li
            row = sims[li]
            hits = np.nonzero(row[i + 1:] >= threshold)[0]   # 只看上三角
            for k in hits:
                j = i + 1 + int(k)
                if paths[i] == paths[j] and not same_file:
                    continue                    # 同文件的不同段，跳过
                sc = float(row[j])
                if paths[i] == paths[j]:
                    key = ("same", paths[i], i, j)
                else:
                    key = ("pair",) + tuple(sorted((paths[i], paths[j])))
                if key not in best or sc > best[key]["score"]:
                    best[key] = {
                        "a": {"id": rows[i]["id"], "path": paths[i],
                              "chunk_index": rows[i]["chunk_index"]},
                        "b": {"id": rows[j]["id"], "path": paths[j],
                              "chunk_index": rows[j]["chunk_index"]},
                        "score": sc,
                        "same_file": paths[i] == paths[j],
                    }

    out["pairs"] = sorted(best.values(), key=lambda x: -x["score"])[:max(1, limit)]
    out["count"] = len(out["pairs"])
    return out
