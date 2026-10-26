"""Prism · 棱镜 — 一次性迁移：单文件库 → 一个库一个文件

旧结构：data/prism.db 里同时装着 libraries / items / tags / jobs
新结构：data/prism.db 只放元信息（libraries / jobs / lib_stats）
        data/libs/lib_<id>.db 每个库一个独立文件（items / virtual_files / tags / item_tags）

迁移步骤（全部在暂存目录里做完、验完，才整体换上去）：
  1. 在 data/_migrate/ 下建好新文件
  2. 按库把 items / virtual_files / 用到的标签 / item_tags 抄进对应文件
  3. 逐项核对数量（库数、每库行数、标签数、虚拟文件数、任务数）
  4. 核对通过才 swap；旧文件在暂存目录里，验证后删除（真正的备份在备份仓里）

不改动磁盘上任何一个源文件；只搬索引。

用法：
    .venv/Scripts/python.exe tools/migrate_split.py            # 真跑
    .venv/Scripts/python.exe tools/migrate_split.py --dry-run   # 只核对，不动现场
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import db  # noqa: E402

OLD = ROOT / "data" / "prism.db"
STAGE = ROOT / "data" / "_migrate"
NEW_META = STAGE / "prism.db"
NEW_LIBS = STAGE / "libs"
OLD_KEEP = STAGE / "_old"


def _q(c, sql, args=()):
    return c.execute(sql, args).fetchone()[0]


def already_split(path: Path) -> bool:
    """老库有 items 表；新元信息库没有。用这个区分。"""
    c = sqlite3.connect(str(path))
    try:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    return "libraries" in names and "items" not in names


def main(dry: bool = False) -> int:
    if not OLD.exists():
        print(f"[×] 找不到 {OLD}")
        return 1
    if already_split(OLD):
        print("[=] 已经是拆分后的结构，无需迁移")
        return 0

    if STAGE.exists():
        shutil.rmtree(STAGE)
    NEW_LIBS.mkdir(parents=True)
    OLD_KEEP.mkdir(parents=True)

    old = sqlite3.connect(str(OLD))
    old.row_factory = sqlite3.Row
    libs = old.execute("SELECT * FROM libraries ORDER BY id").fetchall()
    print(f"[1/4] 老库：{len(libs)} 个库 · "
          f"{_q(old, 'SELECT COUNT(*) FROM items')} 条索引 · "
          f"{_q(old, 'SELECT COUNT(*) FROM tags')} 个标签")

    # ---- 元信息库
    db._fresh_db(NEW_META, db.META_SCHEMA)          # noqa: SLF001
    meta_c = sqlite3.connect(str(NEW_META))
    meta_c.execute("ATTACH DATABASE ? AS old", (str(OLD),))
    meta_c.execute("INSERT INTO libraries(id, name, description, primary_type, "
                   "embed_model, embed_dim, created_at) "
                   "SELECT id, name, description, primary_type, embed_model, "
                   "embed_dim, created_at FROM old.libraries")
    meta_c.execute("INSERT INTO jobs SELECT * FROM old.jobs")
    meta_c.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) "
                   "VALUES ('libraries', (SELECT COALESCE(MAX(id),0) FROM libraries))")
    meta_c.commit()
    meta_c.execute("DETACH DATABASE old")
    meta_c.close()

    # ---- 每个库一个文件
    for i, lib in enumerate(libs, 1):
        lid = lib["id"]
        p = NEW_LIBS / f"lib_{lid}.db"
        db._fresh_db(p, db.LIB_SCHEMA)               # noqa: SLF001
        c = sqlite3.connect(str(p))
        c.execute("ATTACH DATABASE ? AS old", (str(OLD),))
        c.execute("INSERT INTO items SELECT * FROM old.items WHERE library_id=?", (lid,))
        c.execute("INSERT INTO virtual_files SELECT * FROM old.virtual_files "
                  "WHERE library_id=?", (lid,))
        # 只抄「这个库真的用到了」的标签（顺手就把死标签清掉了）
        c.execute("""INSERT INTO tags(name)
                     SELECT DISTINCT t.name FROM old.item_tags it
                     JOIN old.tags t ON t.id = it.tag_id
                     JOIN old.items i ON i.id = it.item_id
                     WHERE i.library_id = ?""", (lid,))
        c.execute("""INSERT INTO item_tags(item_id, tag_id)
                     SELECT it.item_id, nt.id FROM old.item_tags it
                     JOIN old.items i ON i.id = it.item_id
                     JOIN old.tags ot ON ot.id = it.tag_id
                     JOIN tags nt ON nt.name = ot.name
                     WHERE i.library_id = ?""", (lid,))
        c.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) "
                  "VALUES ('items', (SELECT COALESCE(MAX(id),0) FROM items))")
        c.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) "
                  "VALUES ('virtual_files', (SELECT COALESCE(MAX(id),0) FROM virtual_files))")
        c.commit()
        c.execute("DETACH DATABASE old")
        c.close()
        print(f"    [{i}/{len(libs)}] 库 {lid} 《{lib['name']}》抄完")

    # ---- 核对
    print("[2/4] 核对数量")
    errs: list[str] = []
    meta_c = sqlite3.connect(str(NEW_META))
    if _q(meta_c, "SELECT COUNT(*) FROM libraries") != len(libs):
        errs.append("库数量不符")
    old_jobs = _q(old, "SELECT COUNT(*) FROM jobs")
    if _q(meta_c, "SELECT COUNT(*) FROM jobs") != old_jobs:
        errs.append(f"任务数不符 old={old_jobs}")
    meta_c.close()

    total_items = total_tags = total_vf = total_links = 0
    expect: dict[int, int] = {}
    for lib in libs:
        lid = lib["id"]
        c = sqlite3.connect(str(NEW_LIBS / f"lib_{lid}.db"))
        n_items = _q(c, "SELECT COUNT(*) FROM items WHERE library_id=?", (lid,))
        n_vf = _q(c, "SELECT COUNT(*) FROM virtual_files WHERE library_id=?", (lid,))
        n_tags = _q(c, "SELECT COUNT(*) FROM tags")
        n_links = _q(c, "SELECT COUNT(*) FROM item_tags")
        mode = _q(c, "PRAGMA auto_vacuum")
        o_items = _q(old, "SELECT COUNT(*) FROM items WHERE library_id=?", (lid,))
        o_vf = _q(old, "SELECT COUNT(*) FROM virtual_files WHERE library_id=?", (lid,))
        o_tags = _q(old, """SELECT COUNT(DISTINCT t.name) FROM item_tags it
                            JOIN old_tags t ON t.id = it.tag_id
                            JOIN items i ON i.id = it.item_id
                            WHERE i.library_id = ?""".replace("old_tags", "tags"), (lid,))
        o_links = _q(old, """SELECT COUNT(*) FROM item_tags it
                             JOIN items i ON i.id = it.item_id WHERE i.library_id=?""", (lid,))
        c.close()
        expect[lid] = n_items
        total_items += n_items
        total_tags += n_tags
        total_vf += n_vf
        total_links += n_links
        if n_items != o_items:
            errs.append(f"库{lid} 行数 {n_items} != {o_items}")
        if n_vf != o_vf:
            errs.append(f"库{lid} 虚拟文件 {n_vf} != {o_vf}")
        if n_tags != o_tags:
            errs.append(f"库{lid} 标签 {n_tags} != {o_tags}")
        if n_links != o_links:
            errs.append(f"库{lid} 标签关系 {n_links} != {o_links}")
        if mode != 1:
            errs.append(f"库{lid} 回收开关={mode}（应为 1=FULL）")
        print(f"    库{lid}: {n_items} 行 · {n_tags} 标签 · {n_vf} 虚拟 · "
              f"{n_links} 关系 · 回收开关={mode}")

    old_items = _q(old, "SELECT COUNT(*) FROM items")
    old_vf = _q(old, "SELECT COUNT(*) FROM virtual_files")
    if total_items != old_items:
        errs.append(f"总行数 {total_items} != {old_items}")
    if total_vf != old_vf:
        errs.append(f"总虚拟文件 {total_vf} != {old_vf}")
    print(f"    合计 {total_items} 行 / {total_vf} 虚拟 / {total_tags} 标签"
          f"（老库死标签 {_q(old, 'SELECT COUNT(*) FROM tags') - total_tags} 个已清除）")

    if errs:
        print("[×] 核对失败，现场未动：")
        for e in errs:
            print("    -", e)
        old.close()
        return 2

    if dry:
        print("[dry-run] 核对通过，未替换现场")
        old.close()
        return 0

    old.close()

    # ---- 替换现场
    print("[3/4] 替换现场")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(OLD) + suffix)
        if src.exists():
            shutil.move(str(src), str(OLD_KEEP / src.name))
    shutil.move(str(NEW_META), str(OLD))
    old_libs = ROOT / "data" / "libs"
    if old_libs.exists():
        shutil.move(str(old_libs), str(OLD_KEEP / "libs_prev"))
    shutil.move(str(NEW_LIBS), str(old_libs))

    # ---- 复验（读现场）
    print("[4/4] 复验现场")
    db.close_all()
    c = sqlite3.connect(str(OLD))
    n_lib = _q(c, "SELECT COUNT(*) FROM libraries")
    c.close()
    ok = n_lib == len(libs)
    for lid, want in expect.items():
        p = old_libs / f"lib_{lid}.db"
        if not p.exists():
            ok = False
            print(f"    [×] 缺文件 {p}")
            continue
        c = sqlite3.connect(str(p))
        n = _q(c, "SELECT COUNT(*) FROM items")
        mode = _q(c, "PRAGMA auto_vacuum")
        c.close()
        if n != want or mode != 1:
            ok = False
            print(f"    [×] 库{lid} 现场有 {n} 行（应 {want}），回收开关={mode}")
    print(f"    现场：{n_lib} 个库 · 文件位于 {old_libs}")

    shutil.rmtree(STAGE)          # 暂存区（含旧库副本）删掉 —— 备份在备份仓里
    print("完成。" if ok else "完成但有告警，请检查上面的输出。")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main(dry="--dry-run" in sys.argv))
