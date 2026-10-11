"""Prism · 棱镜 — 向量检索

纯向量 top-k：把库里的向量读进内存做点积（万级 → 毫秒级）。
不调用 LLM，因此免费、离线、可预测。
"""
import numpy as np

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


def _score_rows(query_vec: np.ndarray, rows, top_k: int) -> list[dict]:
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
            "tags": db.item_tags(r["id"]),
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
    hits = _score_rows(np.asarray(embed_text(query), dtype=np.float32), rows, n)
    return group_by_file(hits, top_k) if group else hits


def _row_to_hit(r, score: float) -> dict:
    return {
        "id": r["id"],
        "path": r["path"],
        "chunk_index": r["chunk_index"],
        "modality": r["modality"],
        "text": r.get("text") or r.get("chunk_text") or "",
        "score": float(score),
        "tags": db.item_tags(r["id"]),
    }


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
        if not (n_name or n_path or n_text):
            continue
        rank = (3 if n_name else 0) + (2 if n_path else 0) + (1 if n_text else 0)
        # 分数做成向量量级（0~1），且普遍高于语义分，混合时自然置顶
        score = min(0.999, 0.50 + 0.10 * rank + 0.01 * min(n_name + n_path + n_text, 5))
        scored.append((rank, n_name + n_path + n_text, path, r, score))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    n = max(top_k * 4, top_k) if group else top_k
    hits = [_row_to_hit(r, sc) for _, _, _, r, sc in scored[:n]]
    return group_by_file(hits, top_k) if group else hits


def search_mixed(library_id: int, query: str, top_k: int = 12,
                 modality: str | None = None, group: bool = False) -> list[dict]:
    """混合检索：关键词命中置顶 + 语义结果补足（按文件去重）。

    默认模式。搜确切名字时关键词那一路把正确答案顶到前面；
    搜概念（"写色情小说的技巧"）时关键词多半没命中，结果就全是语义排序，
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


def search_any(library_id: int, query: str, top_k: int = 12,
               modality: str | None = None, group: bool = False,
               mode: str = "mixed") -> list[dict]:
    """检索入口：按模式分派。mode = semantic / keyword / mixed。"""
    m = (mode or "mixed").lower()
    if m == "keyword":
        return search_keyword(library_id, query, top_k, modality, group)
    if m == "semantic":
        return search_text(library_id, query, top_k, modality, group)
    return search_mixed(library_id, query, top_k, modality, group)


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
                     modality: str | None = None) -> list[dict]:
    """拿一个现成的向量去库里搜（拖拽文件、以图搜图、任意模态互搜都走这里）。"""
    rows = db.all_vectors(library_id, modality)
    return _score_rows(np.asarray(vec, dtype=np.float32), rows, top_k)


def search_by_image(library_id: int, image_path: str, top_k: int = 12) -> list[dict]:
    """以图搜图 / 以图搜库。"""
    from embedder import embed_image
    rows = db.all_vectors(library_id, None)
    return _score_rows(np.asarray(embed_image(image_path), dtype=np.float32), rows, top_k)


def similar_to_item(library_id: int, item_id: int, top_k: int = 12) -> list[dict]:
    """找与某个已有条目相似的其它条目（"像这张的还有吗"）。"""
    item = db.get_item(item_id)
    if not item or not item.get("vec"):
        return []
    rows = db.all_vectors(library_id, None)
    return _score_rows(from_blob(item["vec"]), rows, top_k + 1)


def duplicates(library_id: int, threshold: float = 0.97, limit: int = 50,
               same_file: bool = False) -> list[dict]:
    """近似重复检测：两两相似度超阈值的配对。

    两个必须做的过滤（否则结果毫无意义）：
      1. **排除同一个文件的不同段** —— 一个文件切成 N 段后，段与段本来就高度相似，
         不排掉就会出现"文件自己和自己重复"的荒唐结果。
      2. **同一对文件只保留最高分** —— A 文件 5 段 × B 文件 4 段会产生 20 个组合，
         不去重的话同一对文件会刷屏。

    same_file=True 时才保留同文件配对（想看"这个文件内部有没有重复段落"时用）。
    """
    rows = db.all_vectors(library_id, None)
    if len(rows) < 2:
        return []
    M = normalize(np.vstack([from_blob(r["vec"]) for r in rows]))
    sims = M @ M.T
    np.fill_diagonal(sims, -1.0)

    paths = [r["path"] for r in rows]
    n = len(rows)
    best: dict[tuple, dict] = {}

    for i in range(n):
        for j in range(i + 1, n):
            if paths[i] == paths[j] and not same_file:
                continue                                   # 同文件的不同段，跳过
            sc = float(sims[i, j])
            if sc < threshold:
                continue
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

    out = sorted(best.values(), key=lambda x: -x["score"])
    return out[:max(1, limit)]
