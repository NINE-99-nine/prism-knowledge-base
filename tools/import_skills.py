#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把技能库的 name + description 导入 Prism 的某个库（一个技能一条）。

与 Prism 原生的「导入文件」不同：原生导入会把 SKILL.md 切段、全文入索引；
这个脚本只取 frontmatter 的 name + description，做的是**技能路由索引** ——
回答「这个任务该用哪个技能」。体积小、一条一技能、可反复重跑（按 path 覆盖）。

用法（路径按实际改）：
  <PRISM>\\.venv\\Scripts\\python.exe ^
      <PRISM>\\tools\\import_skills.py ^
      --library "dsh skill" ^
      --skills  "<你的 skills 目录>"

参数：
  --library  目标库名，或数字 id
  --skills   技能根目录（递归找 **/SKILL.md）
  --prune    额外删除该库中「已不在本次清单里」的条目（源文件永远不动）
  --dry-run  只采集+嵌入，不写库
"""
import argparse
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PRISM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PRISM / "server"))

import db            # noqa: E402  prism/server/db.py
import scanner       # noqa: E402  prism/server/scanner.py
from embedder import embed_texts, to_bytes   # noqa: E402

FM = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def parse_skill(text: str, fallback_name: str) -> tuple[str, str]:
    """取 frontmatter 的 name / description；没有 description 就用正文第一行。"""
    name = desc = ""
    m = FM.search(text)
    if m:
        fm = m.group(1)
        nm = re.search(r"^name:\s*(.+)$", fm, re.M)
        dm = re.search(r"^description:\s*(.+)$", fm, re.M)
        name = nm.group(1).strip() if nm else ""
        desc = dm.group(1).strip() if dm else ""
    if not desc:
        body = FM.sub("", text).strip().splitlines()
        desc = next((l.strip("# ").strip() for l in body if l.strip()), "")
    return (name or fallback_name), desc


def collect(skills_dir: Path) -> list[dict]:
    items = []
    for p in sorted(skills_dir.rglob("SKILL.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        name, desc = parse_skill(text, p.parent.name)
        items.append({"path": str(p.resolve()), "name": name,
                      "text": f"{name}: {desc}"[:600]})
    return items


def resolve_library(token: str) -> int:
    libs = db.list_libraries()
    if token.isdigit():
        lid = int(token)
        if any(l["id"] == lid for l in libs):
            return lid
        raise SystemExit(f"库 id 不存在: {lid}")
    for l in libs:
        if l["name"] == token:
            return l["id"]
    raise SystemExit(f"库名不存在: {token}（现有：{', '.join(l['name'] for l in libs)}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", required=True)
    ap.add_argument("--skills", required=True)
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    skills_dir = Path(a.skills)
    if not skills_dir.is_dir():
        raise SystemExit(f"技能目录不存在: {skills_dir}")

    lid = resolve_library(a.library)
    items = collect(skills_dir)
    if not items:
        raise SystemExit("没有采集到任何 SKILL.md")
    print(f"[{a.library}] (id={lid}) 采集 {len(items)} 个技能", flush=True)

    vecs = embed_texts([it["text"] for it in items], batch=16)
    print(f"  嵌入完成 {len(vecs)} 条", flush=True)

    if a.dry_run:
        for it in items[:3]:
            print(f"  - {it['name']}: {it['text'][:80]}")
        return 0

    ok = 0
    for it, v in zip(items, vecs):
        info = scanner.file_info(it["path"])
        db.upsert_item(library_id=lid, path=it["path"], chunk_index=0,
                       modality="text", vec_bytes=to_bytes(v),
                       chunk_text=it["text"],
                       size=info["size"] if info else 0,
                       mtime=info["mtime"] if info else 0.0,
                       fingerprint=info["fingerprint"] if info else "")
        ok += 1
        if ok % 40 == 0:
            print(f"  {ok}/{len(items)}", flush=True)

    pruned = 0
    if a.prune:
        keep = {it["path"] for it in items}
        rows = db.conn().execute(
            "SELECT DISTINCT path FROM items WHERE library_id=?", (lid,)).fetchall()
        for r in rows:
            if r["path"] not in keep:
                db.delete_items_by_path(lid, r["path"])
                pruned += 1

    print(f"完成：写入 {ok} 条" + (f"，清理 {pruned} 条陈旧条目" if a.prune else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
