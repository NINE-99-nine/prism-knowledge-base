"""Prism · 棱镜 — 文本分块

长文本切成段再嵌入：能定位到「第几段说了什么」，检索质量远高于整文件一个向量。
优先在自然边界（空行 → 换行 → 句号）断开，避免把句子劈开。
"""
from config import CHUNK_OVERLAP, CHUNK_SIZE

_BREAKS = ["\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", ";", "，", ",", " "]


def _cut_point(text: str, start: int, hard_end: int) -> int:
    """在 [start+size*0.6, hard_end] 区间里从后往前找一个自然断点。"""
    if hard_end >= len(text):
        return len(text)
    floor = start + int(CHUNK_SIZE * 0.6)
    for br in _BREAKS:
        idx = text.rfind(br, floor, hard_end)
        if idx > floor:
            return idx + len(br)
    return hard_end


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把 text 切成若干段（含轻微重叠，避免断点处信息丢失）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        hard_end = min(pos + size, n)
        end = _cut_point(text, pos, hard_end)
        piece = text[pos:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return chunks


def preview(text: str, limit: int = 120) -> str:
    """给前端用的单行摘要。"""
    s = " ".join((text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")
