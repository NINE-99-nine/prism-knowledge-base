"""Prism · 棱镜 — SQLite 数据层（一个库一个文件）

## 文件布局

    data/prism.db               元信息库：libraries / jobs / lib_stats
    data/libs/lib_<库id>.db     每个库一个独立文件：items / virtual_files / tags / item_tags

为什么拆文件：
  · 删库 = 删文件 —— 瞬间、零重写、空间 100% 真释放
  · 压实只影响出事的那个库，不会为一个小库的碎片去重写整个大库
  · 库与库不再争同一个写锁

## 空间回收（关键修正）

    PRAGMA auto_vacuum = FULL     ← 必须在建表之前、切 journal_mode 之前设！
    PRAGMA journal_mode = WAL

旧代码顺序反了（先 WAL、后 auto_vacuum）。实测这种写法开关会被**静默忽略**：
库从出生起就不允许回收，删多少行文件一个字节都不缩。

FULL（即时回收）的语义：一次提交里腾出来的**整页**立刻还给文件系统。
写量 ∝ 被删掉的数据量，与库的总大小无关 —— 高频删除不会把磁盘写爆。
剩下的「页内碎片」（页里只剩几条、整页空不出来）由 housekeeping 按阈值压实：
    删除行数 / 现存行数 ≥ COMPACT_RATIO(0.3) 且没有导入在跑
    → VACUUM INTO 新文件 + 原子替换（只读源库，不阻塞读写）
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from config import DATA_DIR, EMBED_DIM, EMBED_MODEL

META_PATH = DATA_DIR / "prism.db"
LIBS_DIR = DATA_DIR / "libs"

COMPACT_RATIO = 0.30        # 删除量 / 现存行数 超过这个比例才做整库压实
COMPACT_MIN_ROWS = 2000     # 小库不折腾：现存行数少于这个值，压实的收益不值当

_local = threading.local()
_write_locks: dict[int, threading.RLock] = {}
_write_locks_guard = threading.Lock()
_compact_lock = threading.Lock()
_compact_busy: set[int] = set()

# 所有活着的连接（按库登记）。删库/压实时必须能把别的线程手里的句柄关掉 ——
# Windows 不允许改名或删除一个被打开的文件，不关就永远删不掉。
_conns: dict[int, set] = {}
_conns_lock = threading.Lock()


def _register(lid: int, c) -> None:
    with _conns_lock:
        _conns.setdefault(lid, set()).add(c)


def _forget(lid: int, c) -> None:
    with _conns_lock:
        s = _conns.get(lid)
        if s is not None:
            s.discard(c)
            if not s:
                _conns.pop(lid, None)


def _close_lib_conns(lid: int) -> None:
    """把这个库在所有线程里的连接通通关掉（连接是 check_same_thread=False 开的）。"""
    with _conns_lock:
        conns = list(_conns.get(lid, ()))
        _conns.pop(lid, None)
    for c in conns:
        try:
            c.close()
        except Exception:
            pass

# 连接代号：每压实一次 +1，各线程下次取连接时丢掉旧句柄
# （Windows 不允许覆盖被打开的文件，必须先让所有旧句柄失效）
_gen: dict[int, int] = {}


def _lock_for(lid: int) -> threading.RLock:
    with _write_locks_guard:
        lk = _write_locks.get(lid)
        if lk is None:
            lk = _write_locks[lid] = threading.RLock()
        return lk


META_SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',
    primary_type TEXT NOT NULL DEFAULT 'mixed',
    embed_model  TEXT NOT NULL DEFAULT '',
    embed_dim    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    library_id   INTEGER,
    state        TEXT    NOT NULL DEFAULT 'running',
    total        INTEGER NOT NULL DEFAULT 0,
    done         INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    failed       TEXT    NOT NULL DEFAULT '[]',
    skipped      TEXT    NOT NULL DEFAULT '[]',
    current      TEXT    NOT NULL DEFAULT '',
    error        TEXT,
    started      REAL,
    finished     REAL,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    updated      REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started);

-- 每个库的回收账本：上次压实之后删了多少行（判断要不要再压一次）
CREATE TABLE IF NOT EXISTS lib_stats (
    library_id      INTEGER PRIMARY KEY,
    deletes_since   INTEGER NOT NULL DEFAULT 0,
    rows_at_compact INTEGER NOT NULL DEFAULT 0,
    compacted_at    REAL,
    bytes_before    INTEGER NOT NULL DEFAULT 0
);
"""

LIB_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id  INTEGER NOT NULL,
    path        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    modality    TEXT NOT NULL DEFAULT 'other',
    chunk_text  TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    meta        TEXT NOT NULL DEFAULT '{}',
    vec         BLOB,
    added_at    TEXT NOT NULL,
    UNIQUE(library_id, path, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_items_lib    ON items(library_id);
CREATE INDEX IF NOT EXISTS idx_items_path   ON items(path);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_modal  ON items(modality);

CREATE TABLE IF NOT EXISTS virtual_files (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id  INTEGER NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  cover       TEXT NOT NULL DEFAULT '',
  related     TEXT NOT NULL DEFAULT '[]',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vf_lib ON virtual_files(library_id);

CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL UNIQUE,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_item ON item_tags(item_id);
CREATE INDEX IF NOT EXISTS idx_item_tags_tag  ON item_tags(tag_id);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 连接

def _fresh_db(path: Path, schema: str) -> None:
    """建空库文件，并按正确顺序打开「即时回收」。

    顺序不能变：auto_vacuum → journal_mode → 建表。
    反过来的话数据库会把 auto_vacuum 当无效语句静默吞掉（不报错），
    这个库就一辈子不回收空间 —— 旧版的病根。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=30)
    try:
        c.execute("PRAGMA auto_vacuum = FULL")
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(schema)
        c.commit()
        mode = c.execute("PRAGMA auto_vacuum").fetchone()[0]
        if mode != 1:
            raise RuntimeError(f"回收开关没打开（auto_vacuum={mode}）：{path}")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()


def lib_path(library_id: int) -> Path:
    return LIBS_DIR / f"lib_{int(library_id)}.db"


def _open(path: Path) -> sqlite3.Connection:
    # check_same_thread=False：删库/压实时需要从别的线程把这些句柄关掉，
    # 否则 Windows 下文件被占用、删不掉也改不了名。实际使用仍是每线程一条。
    c = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.create_function("sortname", 1, _sort_name, deterministic=True)
    return c


def _open_lib(library_id: int) -> sqlite3.Connection:
    p = lib_path(library_id)
    if not p.exists():
        _fresh_db(p, LIB_SCHEMA)
    return _open(p)


def meta() -> sqlite3.Connection:
    c = getattr(_local, "meta", None)
    if c is None:
        if not META_PATH.exists():
            _fresh_db(META_PATH, META_SCHEMA)
        c = _open(META_PATH)
        _local.meta = c
    return c


def conn(library_id: int) -> sqlite3.Connection:
    """取某个库的连接（每线程一份；压实换过文件后自动重开）。"""
    box = getattr(_local, "libs", None)
    if box is None:
        box = _local.libs = {}
    lid = int(library_id)
    gen = _gen.get(lid, 0)
    item = box.get(lid)
    if item is not None and item[0] != gen:
        try:
            item[1].close()
        except Exception:
            pass
        _forget(lid, item[1])
        box.pop(lid, None)
        item = None
    if item is None:
        c = _open_lib(lid)
        _register(lid, c)
        item = box[lid] = (gen, c)
    return item[1]


def close_all() -> None:
    box = getattr(_local, "libs", None) or {}
    for lid, (_, c) in list(box.items()):
        try:
            c.close()
        except Exception:
            pass
        _forget(lid, c)
    box.clear()
    m = getattr(_local, "meta", None)
    if m is not None:
        try:
            m.close()
        except Exception:
            pass
        _local.meta = None


def ensure_full_autovacuum(library_id: int) -> dict:
    """老库（auto_vacuum 不是 FULL）转成 FULL —— 需要一次整库重写。"""
    p = lib_path(library_id)
    if not p.exists():
        return {"converted": False, "reason": "文件不存在"}
    c = sqlite3.connect(str(p), timeout=30)
    try:
        mode = c.execute("PRAGMA auto_vacuum").fetchone()[0]
        if mode == 1:
            return {"converted": False, "reason": "已是即时回收"}
        before = p.stat().st_size
        c.execute("PRAGMA auto_vacuum = FULL")
        c.execute("VACUUM")                 # 让开关真正落进文件头
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"converted": True, "before": before, "after": p.stat().st_size}
    finally:
        c.close()


def init() -> None:
    meta()
    LIBS_DIR.mkdir(parents=True, exist_ok=True)
    ids = set(all_library_ids())
    for r in meta().execute("SELECT id FROM libraries").fetchall():
        conn(r["id"])
    # 扫掉「元信息里已经没有、但文件还在」的孤儿库文件
    # （以前删库时可能有句柄没关、unlink 失败留下的）
    for f in LIBS_DIR.glob("lib_*.db*"):
        try:
            lid = int(f.name.split("_", 1)[1].split(".")[0])
        except (IndexError, ValueError):
            continue
        if lid not in ids:
            for _ in range(20):
                try:
                    f.unlink()
                    break
                except OSError:
                    time.sleep(0.1)


def rollback() -> None:
    """出错路径必须回滚：语句报错不会自动回滚，写锁会一直握着。"""
    box = getattr(_local, "libs", None) or {}
    for _, c in list(box.values()):
        try:
            c.rollback()
        except Exception:
            pass
    m = getattr(_local, "meta", None)
    if m is not None:
        try:
            m.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------- 空间回收

def reclaim(library_id: int, cleanup_tags: bool = True) -> dict:
    """删完东西后调一次：清死标签 + 合并草稿并截断。"""
    c = conn(library_id)
    freed_tags = 0
    if cleanup_tags:
        freed_tags = _cleanup_unused_tags(library_id)
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    c.commit()          # 兜底：任何残留的隐式事务都收掉，绝不把写锁带出去
    return {"unused_tags_removed": freed_tags}


def note_deletes(library_id: int, n: int) -> None:
    if n <= 0:
        return
    m = meta()
    m.execute("""INSERT INTO lib_stats(library_id, deletes_since, rows_at_compact, compacted_at, bytes_before)
                 VALUES (?,?,0,NULL,0)
                 ON CONFLICT(library_id) DO UPDATE SET deletes_since = deletes_since + ?""",
              (library_id, int(n), int(n)))
    m.commit()


def _cleanup_unused_tags(library_id: int) -> int:
    c = conn(library_id)
    before = c.total_changes
    c.execute("DELETE FROM tags WHERE pinned = 0 AND id NOT IN (SELECT tag_id FROM item_tags)")
    n = c.total_changes - before
    # 🔴 无论删没删到，都必须结束事务：DELETE 会隐式 BEGIN，
    #    不提交就把写锁一直捏在这条连接手里 → 别的线程一写就 database is locked。
    c.commit()
    return n


def lib_usage(library_id: int) -> dict:
    p = lib_path(library_id)
    c = conn(library_id)
    main = p.stat().st_size if p.exists() else 0
    w = Path(str(p) + "-wal")
    wal = w.stat().st_size if w.exists() else 0
    rows = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
    pages = c.execute("PRAGMA page_count").fetchone()[0]
    free = c.execute("PRAGMA freelist_count").fetchone()[0]
    live = c.execute("SELECT COALESCE(SUM(LENGTH(vec)+LENGTH(chunk_text)),0) b FROM items").fetchone()["b"]
    return {"library_id": library_id, "file_bytes": main, "wal_bytes": wal,
            "total_bytes": main + wal, "rows": rows, "live_bytes": int(live or 0),
            "pages": pages, "free_pages": free,
            "page_size": c.execute("PRAGMA page_size").fetchone()[0],
            "autovacuum": c.execute("PRAGMA auto_vacuum").fetchone()[0]}


def needs_compact(library_id: int, ratio: float = COMPACT_RATIO) -> bool:
    try:
        row = meta().execute("SELECT deletes_since FROM lib_stats WHERE library_id=?",
                             (library_id,)).fetchone()
    except Exception:
        return False
    if not row:
        return False
    u = lib_usage(library_id)
    live = max(1, u["rows"])
    if live < COMPACT_MIN_ROWS:
        return False
    return row["deletes_since"] / live >= ratio


def compact(library_id: int) -> dict:
    """整库压实：抄一份只有有效内容的新文件，原子替换。"""
    with _compact_lock:
        lid = int(library_id)
        if lid in _compact_busy:
            return {"ok": False, "reason": "已在压实中"}
        path = lib_path(lid)
        if not path.exists():
            return {"ok": False, "reason": "库文件不存在"}
        _compact_busy.add(lid)
        tmp = Path(str(path) + ".compact")
        try:
            before = path.stat().st_size
            _gen[lid] = _gen.get(lid, 0) + 1        # 让别的线程的旧句柄作废
            _close_lib_conns(lid)                   # 关掉所有线程的句柄，才改得动这个文件

            if tmp.exists():
                tmp.unlink()
            src = sqlite3.connect(str(path), timeout=30)
            try:
                src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                src.execute("VACUUM INTO ?", (str(tmp),))
            finally:
                src.close()

            last_err = None
            for _ in range(120):
                try:
                    os.replace(str(tmp), str(path))
                    last_err = None
                    break
                except PermissionError as e:
                    last_err = e
                    time.sleep(0.25)
            if last_err is not None:
                raise last_err

            after = path.stat().st_size
            _gen[lid] = _gen.get(lid, 0) + 1
            u = lib_usage(lid)
            m = meta()
            m.execute("""INSERT INTO lib_stats(library_id, deletes_since, rows_at_compact, compacted_at, bytes_before)
                         VALUES (?,0,?,?,?)
                         ON CONFLICT(library_id) DO UPDATE SET
                            deletes_since=0, rows_at_compact=?, compacted_at=?, bytes_before=?""",
                      (lid, u["rows"], time.time(), before, u["rows"], time.time(), before))
            m.commit()
            conn(lid).execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {"ok": True, "before": before, "after": after,
                    "saved": max(0, before - after)}
        except Exception as e:
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        finally:
            _compact_busy.discard(lid)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


# ------------------------------------------------------------------ 任务持久化

def save_job(j: dict) -> None:
    c = meta()
    c.execute("""
        INSERT INTO jobs (job_id, library_id, state, total, done, failed_count,
                          failed, skipped, current, error, started, finished,
                          cancelled, updated)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(job_id) DO UPDATE SET
            state=excluded.state, total=excluded.total, done=excluded.done,
            failed_count=excluded.failed_count, failed=excluded.failed,
            skipped=excluded.skipped, current=excluded.current,
            error=excluded.error, finished=excluded.finished,
            cancelled=excluded.cancelled, updated=excluded.updated
    """, (
        j.get("job_id"), j.get("library_id"), j.get("state") or "running",
        int(j.get("total") or 0), int(j.get("done") or 0),
        int(j.get("failed_count") or 0),
        json.dumps(j.get("failed") or [], ensure_ascii=False)[:200000],
        json.dumps(j.get("skipped") or [], ensure_ascii=False)[:50000],
        (j.get("current") or "")[:300], j.get("error"),
        j.get("started"), j.get("finished"),
        1 if j.get("cancelled") else 0, time.time(),
    ))
    c.commit()


def _job_row_to_dict(r) -> dict:
    d = dict(r)
    for k in ("failed", "skipped"):
        try:
            d[k] = json.loads(d.get(k) or "null") or []
        except Exception:
            d[k] = []
    d["cancelled"] = bool(d.get("cancelled"))
    return d


def load_jobs(limit: int = 50) -> list[dict]:
    rows = meta().execute(
        "SELECT * FROM jobs ORDER BY COALESCE(started, 0) DESC LIMIT ?", (limit,)).fetchall()
    return [_job_row_to_dict(r) for r in rows]


def get_job(job_id: str) -> dict | None:
    r = meta().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _job_row_to_dict(r) if r else None


def set_job_cancelled(job_id: str, flag: bool = True) -> None:
    c = meta()
    c.execute("UPDATE jobs SET cancelled=?, updated=? WHERE job_id=?",
              (1 if flag else 0, time.time(), job_id))
    c.commit()


def prune_jobs(keep: int = 200) -> int:
    c = meta()
    n = c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    if n <= keep:
        return 0
    before = c.total_changes
    c.execute("""DELETE FROM jobs WHERE job_id NOT IN (
                    SELECT job_id FROM jobs ORDER BY COALESCE(started,0) DESC LIMIT ?)""",
              (keep,))
    removed = c.total_changes - before
    c.commit()
    return removed


def prune_jobs_by_age(days: int = 30) -> int:
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    c = meta()
    before = c.total_changes
    c.execute("DELETE FROM jobs WHERE COALESCE(started, 0) < ?", (cutoff,))
    removed = c.total_changes - before
    c.commit()
    return removed


# ------------------------------------------------------------------ 库

def create_library(name: str, description: str = "", primary_type: str = "mixed") -> int:
    m = meta()
    cur = m.execute(
        "INSERT INTO libraries(name, description, primary_type, embed_model, embed_dim, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (name, description, primary_type, EMBED_MODEL, EMBED_DIM, _now()))
    m.commit()
    lid = int(cur.lastrowid)
    _fresh_db(lib_path(lid), LIB_SCHEMA)
    with _write_locks_guard:
        _gen[lid] = _gen.get(lid, 0) + 1
    return lid


def list_libraries() -> list[dict]:
    m = meta()
    rows = m.execute("SELECT * FROM libraries ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            c = conn(d["id"])
            d["item_count"] = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
            d["file_count"] = c.execute("SELECT COUNT(DISTINCT path) n FROM items").fetchone()["n"]
            d["missing_count"] = c.execute(
                "SELECT COUNT(*) n FROM items WHERE status='missing'").fetchone()["n"]
            d["disk_bytes"] = lib_usage(d["id"])["total_bytes"]
        except Exception:
            d.update(item_count=0, file_count=0, missing_count=0, disk_bytes=0)
        out.append(d)
    return out


def get_library(lib_id: int) -> dict | None:
    r = meta().execute("SELECT * FROM libraries WHERE id=?", (lib_id,)).fetchone()
    return dict(r) if r else None


def update_library(lib_id: int, **fields) -> bool:
    allowed = {"name", "description", "primary_type"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return False
    sql = "UPDATE libraries SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
    c = meta()
    c.execute(sql, (*sets.values(), lib_id))
    c.commit()
    return True


def delete_library(lib_id: int) -> dict:
    """删库 = 关连接 + 删文件 + 删元信息行。空间立刻全退回文件系统。"""
    lid = int(lib_id)
    bytes_freed = 0
    p = lib_path(lid)
    with _compact_lock:
        box = getattr(_local, "libs", None) or {}
        own = box.pop(lid, None)
        if own:
            try:
                own[1].close()
            except Exception:
                pass
        _gen[lid] = _gen.get(lid, 0) + 1
        _close_lib_conns(lid)          # 别的线程手里的句柄也要关，否则 Windows 删不掉文件
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                bytes_freed += f.stat().st_size
                for _ in range(40):
                    try:
                        f.unlink()
                        break
                    except PermissionError:
                        time.sleep(0.15)
        m = meta()
        m.execute("DELETE FROM libraries WHERE id=?", (lid,))
        m.execute("DELETE FROM lib_stats WHERE library_id=?", (lid,))
        m.execute("DELETE FROM jobs WHERE library_id=?", (lid,))
        m.commit()
    return {"ok": True, "bytes_freed": bytes_freed}


def all_library_ids() -> list[int]:
    return [r["id"] for r in meta().execute("SELECT id FROM libraries ORDER BY id").fetchall()]


# ------------------------------------------------------------------ 条目

def upsert_item(library_id: int, path: str, chunk_index: int, modality: str,
                vec_bytes: bytes, chunk_text: str = "", size: int = 0,
                mtime: float = 0.0, fingerprint: str = "", meta: dict | None = None) -> int:
    c = conn(library_id)
    cur = c.execute("""
        INSERT INTO items(library_id, path, chunk_index, modality, chunk_text, size, mtime,
                          fingerprint, status, meta, vec, added_at)
        VALUES (?,?,?,?,?,?,?,?,'active',?,?,?)
        ON CONFLICT(library_id, path, chunk_index) DO UPDATE SET
            modality=excluded.modality, chunk_text=excluded.chunk_text, size=excluded.size,
            mtime=excluded.mtime, fingerprint=excluded.fingerprint, status='active',
            meta=excluded.meta, vec=excluded.vec, added_at=excluded.added_at
    """, (library_id, path, chunk_index, modality, chunk_text, size, mtime,
          fingerprint, json.dumps(meta or {}, ensure_ascii=False),
          sqlite3.Binary(vec_bytes) if vec_bytes else None, _now()))
    c.commit()
    return cur.lastrowid


def delete_item(library_id: int, item_id: int, do_reclaim: bool = True) -> int:
    c = conn(library_id)
    n = c.execute("SELECT COUNT(*) n FROM items WHERE id=? AND library_id=?",
                  (item_id, library_id)).fetchone()["n"]
    c.execute("DELETE FROM items WHERE id=? AND library_id=?", (item_id, library_id))
    c.commit()
    note_deletes(library_id, n)
    if do_reclaim:
        reclaim(library_id)
    return n


def delete_items_by_path(library_id: int, path: str, do_reclaim: bool = True) -> int:
    c = conn(library_id)
    n = c.execute("SELECT COUNT(*) n FROM items WHERE library_id=? AND path=?",
                  (library_id, path)).fetchone()["n"]
    c.execute("DELETE FROM items WHERE library_id=? AND path=?", (library_id, path))
    c.commit()
    note_deletes(library_id, n)
    if do_reclaim:
        reclaim(library_id)
    return n


def get_item(library_id: int, item_id: int) -> dict | None:
    r = conn(library_id).execute("SELECT * FROM items WHERE id=? AND library_id=?",
                                 (item_id, library_id)).fetchone()
    return dict(r) if r else None


def path_is_indexed(path: str) -> bool:
    """这个路径是否已在任一库里建过索引（/api/open、/api/file 的安全校验用）。"""
    variants = tuple({path, path.replace("/", "\\"), path.replace("\\", "/")})
    q = ",".join("?" * len(variants))
    for lid in all_library_ids():
        try:
            row = conn(lid).execute(
                f"SELECT 1 FROM items WHERE path IN ({q}) LIMIT 1", variants).fetchone()
            if row:
                return True
        except Exception:
            continue
    return False


def search_keyword_rows(library_id: int, query: str,
                        modality: str | None = None, limit: int = 3000) -> list[dict]:
    """朴素子串匹配：path / 正文 / 标签名 含查询串即命中。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    pat = f"%{q}%"
    sql = ("SELECT i.id, i.path, i.chunk_index, i.modality, i.chunk_text, "
           "  (EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id "
           "           WHERE it.item_id = i.id AND lower(t.name) LIKE ?)) AS tag_hit "
           "FROM items i WHERE i.library_id=? AND i.status='active' "
           "AND (lower(i.path) LIKE ? OR lower(COALESCE(i.chunk_text,'')) LIKE ? "
           "     OR EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id "
           "                WHERE it.item_id = i.id AND lower(t.name) LIKE ?))")
    args: list = [pat, library_id, pat, pat, pat]
    if modality:
        sql += " AND i.modality=?"
        args.append(modality)
    sql += " LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn(library_id).execute(sql, args).fetchall()]


def list_missing(library_id: int) -> list[dict]:
    rows = conn(library_id).execute(
        "SELECT path, modality, COUNT(*) n FROM items "
        "WHERE library_id=? AND status='missing' GROUP BY path ORDER BY path",
        (library_id,)).fetchall()
    return [dict(r) for r in rows]


def purge_missing(library_id: int) -> int:
    c = conn(library_id)
    before = c.total_changes
    c.execute("DELETE FROM items WHERE library_id=? AND status='missing'", (library_id,))
    n = c.total_changes - before
    c.commit()
    if n:
        note_deletes(library_id, n)
        reclaim(library_id)
    return n


def known_fingerprints(library_id: int) -> dict[str, str]:
    rows = conn(library_id).execute(
        "SELECT path, fingerprint FROM items WHERE library_id=? AND chunk_index=0",
        (library_id,)).fetchall()
    return {r["path"]: r["fingerprint"] for r in rows}


def count_vectors(library_id: int, modality: str | None = None) -> int:
    sql = "SELECT COUNT(*) n FROM items WHERE library_id=? AND status='active' AND vec IS NOT NULL"
    args: list = [library_id]
    if modality:
        sql += " AND modality=?"
        args.append(modality)
    return conn(library_id).execute(sql, args).fetchone()["n"]


def all_vectors(library_id: int, modality: str | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT id, path, chunk_index, modality, chunk_text, vec "
           "FROM items WHERE library_id=? AND status='active' AND vec IS NOT NULL")
    args: list = [library_id]
    if modality:
        sql += " AND modality=?"
        args.append(modality)
    return conn(library_id).execute(sql, args).fetchall()


def mark_missing(library_id: int, path: str) -> None:
    c = conn(library_id)
    c.execute("UPDATE items SET status='missing' WHERE library_id=? AND path=?", (library_id, path))
    c.commit()


# --------------------------------------------- 文件级视图（排序 / 正反筛选）

def _sort_name(path: str) -> str:
    """「显示名」排序键 —— 与前端 displayName 同一规则。"""
    p = str(path or "").replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if base.startswith("prism://virtual/"):
        return base
    if re.match(r"^(skill|readme|index|__init__|main)\."
                r"(md|markdown|txt|py|js|ts|json|html|css|sh|bat|yaml|yml)$", base, re.I):
        parts = p.split("/")
        if len(parts) >= 2:
            base = parts[-2] or base
    return base.lower()


_SORT_EXPR = {
    "added": "MAX(i.id)",
    # 虚拟文件排「名称」时要用它的标题，不能拿 prism://virtual/12 当名字排
    "name": "COALESCE(NULLIF(v.title, ''), sortname(i.path)) COLLATE NOCASE",
    "size": "MAX(i.size)",
    "mtime": "MAX(i.mtime)",
}


def _norm_tag_list(tags) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = tags.replace("，", ",").split(",")
    return [str(t).strip() for t in tags if str(t).strip()]


def _tag_filter_sql(library_id: int, include: list[str], exclude: list[str]):
    """返回 (cte_sql, extra_where, params)；标签是段级的，按 path 判定。"""
    ctes, extra, params = [], [], []
    if include:
        qs = ",".join("?" * len(include))
        ctes.append(f"""inc AS (
            SELECT i.path AS path FROM items i
            JOIN item_tags it ON it.item_id = i.id
            JOIN tags t ON t.id = it.tag_id
            WHERE i.library_id = ? AND t.name IN ({qs})
            GROUP BY i.path HAVING COUNT(DISTINCT t.name) = {len(include)})""")
        params += [library_id] + include
        extra.append("i.path IN (SELECT path FROM inc)")
    if exclude:
        qs = ",".join("?" * len(exclude))
        ctes.append(f"""exc AS (
            SELECT DISTINCT i.path AS path FROM items i
            JOIN item_tags it ON it.item_id = i.id
            JOIN tags t ON t.id = it.tag_id
            WHERE i.library_id = ? AND t.name IN ({qs}))""")
        params += [library_id] + exclude
        extra.append("i.path NOT IN (SELECT path FROM exc)")
    cte_sql = ("WITH " + ", ".join(ctes) + " ") if ctes else ""
    return cte_sql, extra, params


def files_page(library_id: int, limit: int = 300, offset: int = 0,
               modality: str | None = None, sort: str = "added", order: str = "desc",
               tags_include=None, tags_exclude=None, q: str | None = None) -> dict:
    """浏览用：一个文件一行。排序 + 标签正反筛选全在 SQL 里做，分页才准。"""
    inc = _norm_tag_list(tags_include)
    exc = _norm_tag_list(tags_exclude)
    key = (sort or "added").lower()
    expr = _SORT_EXPR.get(key, _SORT_EXPR["added"])
    direction = "ASC" if (order or "desc").lower() == "asc" else "DESC"

    where = ["i.library_id = ?"]
    params: list = [library_id]
    if modality == "virt":
        where.append("i.path LIKE ?")
        params.append(VIRTUAL_PREFIX + "%")
    elif modality:
        where.append("i.modality = ?")
        params.append(modality)
    if q:
        where.append("(lower(i.path) LIKE ? OR lower(COALESCE(i.chunk_text,'')) LIKE ?)")
        params += [f"%{q.lower()}%", f"%{q.lower()}%"]

    cte_sql, extra, cte_params = _tag_filter_sql(library_id, inc, exc)
    where_sql = " AND ".join(where + extra)

    c = conn(library_id)
    # 按名称排序时把虚拟文件的标题连进来（伪路径 p… 的 basename 排出来的顺序没有意义）
    vjoin = ("LEFT JOIN virtual_files v ON v.library_id = i.library_id "
             "AND i.path = '" + VIRTUAL_PREFIX + "' || v.id ") if key == "name" else ""
    rows = c.execute(f"""
        {cte_sql}
        SELECT i.path AS path,
               MAX(i.id)       AS id,
               i.modality      AS modality,
               COUNT(*)        AS chunks,
               MAX(i.size)     AS size,
               MAX(i.mtime)    AS mtime,
               MAX(i.added_at) AS added_at,
               MAX(CASE WHEN i.chunk_index = 0 THEN substr(i.chunk_text, 1, 220) END) AS preview,
               CASE WHEN MIN(i.status) = 'missing' THEN 'missing' ELSE 'ok' END AS status
        FROM items i {vjoin}
        WHERE {where_sql}
        GROUP BY i.path
        ORDER BY {expr} {direction}
        LIMIT ? OFFSET ?
    """, cte_params + params + [int(limit), int(offset)]).fetchall()

    total = c.execute(f"""
        {cte_sql}
        SELECT COUNT(*) FROM (SELECT 1 FROM items i WHERE {where_sql} GROUP BY i.path)
    """, cte_params + params).fetchone()[0]

    tagmap = path_tags_map(library_id, [r["path"] for r in rows])
    out = []
    vmeta: dict[int, dict] = {}
    for r in rows:
        d = dict(r)
        vid = vid_of_path(d.get("path"))
        if vid is not None:
            if not vmeta:
                vmeta = virtual_meta_map(library_id)
            info = vmeta.get(vid)
            if info:
                d["virt_title"] = info.get("title") or ""
                d["virt_cover"] = info.get("cover") or ""
                d["virt_description"] = info.get("description") or ""
        d["tags"] = tagmap.get(d["path"], [])
        out.append(d)
    return {"total": int(total), "files": out, "sort": key,
            "order": direction.lower(), "tags_include": inc, "tags_exclude": exc}


def path_tags_map(library_id: int, paths: list[str]) -> dict[str, list[str]]:
    if not paths:
        return {}
    c = conn(library_id)
    out: dict[str, set[str]] = {}
    for s in range(0, len(paths), 400):
        chunk = paths[s:s + 400]
        qs = ",".join("?" * len(chunk))
        for r in c.execute(f"""
            SELECT DISTINCT i.path AS path, t.name AS name
            FROM items i JOIN item_tags it ON it.item_id = i.id
            JOIN tags t ON t.id = it.tag_id
            WHERE i.library_id = ? AND i.path IN ({qs})
        """, [library_id] + chunk):
            out.setdefault(r["path"], set()).add(r["name"])
    return {k: sorted(v) for k, v in out.items()}


# ------------------------------------------------------- 跨库移动 / 复制

def transfer_paths(src_library: int, paths: list[str], dst_library: int,
                   delete_src: bool = True) -> dict:
    """把若干**文件**（全部段 + 标签 + 虚拟文件元数据）搬到另一个库或复制过去。"""
    if int(src_library) == int(dst_library):
        raise ValueError("源库和目标库相同")
    if not get_library(dst_library):
        raise ValueError("目标库不存在")
    src, dst = conn(src_library), conn(dst_library)
    moved = copied = 0
    skipped: list[str] = []
    for path in paths or []:
        rows = src.execute("SELECT * FROM items WHERE library_id=? AND path=? ORDER BY chunk_index",
                           (src_library, path)).fetchall()
        if not rows:
            skipped.append(path)
            continue
        tags = [r["name"] for r in src.execute("""
            SELECT DISTINCT t.name AS name FROM items i
            JOIN item_tags it ON it.item_id = i.id
            JOIN tags t ON t.id = it.tag_id
            WHERE i.library_id=? AND i.path=?""", (src_library, path))]
        vid = vid_of_path(path)
        vf = None
        if vid is not None:
            r = src.execute("SELECT * FROM virtual_files WHERE id=? AND library_id=?",
                            (vid, src_library)).fetchone()
            vf = dict(r) if r else None

        dst.execute("DELETE FROM items WHERE library_id=? AND path=?", (dst_library, path))
        for r in rows:
            dst.execute("""
                INSERT INTO items(library_id, path, chunk_index, modality, chunk_text, size,
                                  mtime, fingerprint, status, meta, vec, added_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (dst_library, r["path"], r["chunk_index"], r["modality"], r["chunk_text"],
                  r["size"], r["mtime"], r["fingerprint"], r["status"], r["meta"],
                  r["vec"], r["added_at"]))
        if vf is not None:
            dst.execute("""INSERT INTO virtual_files(library_id, title, description, cover,
                                                     related, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (dst_library, vf.get("title") or "", vf.get("description") or "",
                         vf.get("cover") or "", vf.get("related") or "[]",
                         vf.get("created_at") or _now(), vf.get("updated_at") or _now()))
        if tags:
            first = dst.execute("SELECT id FROM items WHERE library_id=? AND path=? "
                                "ORDER BY chunk_index LIMIT 1", (dst_library, path)).fetchone()
            if first:
                for t in tags:
                    _tag_one(dst, first["id"], t)

        if delete_src:
            src.execute("DELETE FROM items WHERE library_id=? AND path=?", (src_library, path))
            if vid is not None:
                src.execute("DELETE FROM virtual_files WHERE id=? AND library_id=?",
                            (vid, src_library))
            moved += 1
            note_deletes(src_library, len(rows))
        else:
            copied += 1
        dst.commit()
    if delete_src:
        src.commit()
        reclaim(src_library)
    reclaim(dst_library)
    return {"moved": moved, "copied": copied, "skipped": skipped[:50],
            "target_library_id": dst_library}


# ------------------------------------------------------------------ 标签

def _tag_one(c, item_id: int, name: str) -> None:
    c.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
    tid = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()["id"]
    c.execute("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES (?,?)", (item_id, tid))


def add_tag(library_id: int, item_id: int, tag: str) -> None:
    name = (tag or "").strip()
    if not name:
        return
    c = conn(library_id)
    _tag_one(c, item_id, name)
    c.commit()


def remove_tag(library_id: int, item_id: int, tag: str) -> None:
    c = conn(library_id)
    r = c.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()
    if r:
        c.execute("DELETE FROM item_tags WHERE item_id=? AND tag_id=?", (item_id, r["id"]))
        c.commit()
        _cleanup_unused_tags(library_id)


def tag_path(library_id: int, path: str, tags: list[str]) -> int:
    """给一个**文件**打标签（落在第一个段上，与虚拟文件的约定一致）。"""
    names = [t.strip() for t in (tags or []) if t and t.strip()]
    if not names:
        return 0
    c = conn(library_id)
    row = c.execute("SELECT id FROM items WHERE library_id=? AND path=? "
                    "ORDER BY chunk_index LIMIT 1", (library_id, path)).fetchone()
    if not row:
        return 0
    for t in names:
        _tag_one(c, row["id"], t)
    c.commit()
    return len(names)


def tag_batch(library_id: int, paths: list[str], add=None, remove=None) -> dict:
    """批量打标 / 移除标签（按「文件」语义）。

    · 加：落在该文件的第一个段上（多段文件只算一个标签）
    · 删：把这个文件**所有段**上的该标签都删掉，否则统计会虚高
    """
    addl = _norm_tag_list(add)
    removel = _norm_tag_list(remove)
    paths = [p for p in (paths or []) if p]
    if not paths or (not addl and not removel):
        return {"added": 0, "removed": 0, "files": 0, "unused_tags_removed": 0}
    c = conn(library_id)
    added = removed = touched = 0
    for path in paths:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM items WHERE library_id=? AND path=? ORDER BY chunk_index",
            (library_id, path)).fetchall()]
        if not ids:
            continue
        touched += 1
        for t in addl:
            _tag_one(c, ids[0], t)
            added += 1
        for t in removel:
            cur = c.execute("""
                DELETE FROM item_tags
                WHERE tag_id = (SELECT id FROM tags WHERE name = ?)
                  AND item_id IN (SELECT id FROM items WHERE library_id = ? AND path = ?)
            """, (t, library_id, path))
            removed += max(0, cur.rowcount or 0)
    c.commit()
    dead = _cleanup_unused_tags(library_id)
    return {"added": added, "removed": removed, "files": touched,
            "unused_tags_removed": dead}


def item_tags(library_id: int, item_id: int) -> list[str]:
    rows = conn(library_id).execute("""
        SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id
        WHERE it.item_id=? ORDER BY t.name""", (item_id,)).fetchall()
    return [r["name"] for r in rows]


def all_tags(library_id: int | None = None) -> list[str]:
    if library_id is None:
        out: list[str] = []
        for lid in all_library_ids():
            try:
                out += [r["name"] for r in conn(lid).execute("SELECT name FROM tags ORDER BY name")]
            except Exception:
                continue
        return sorted(set(out))
    return [r["name"] for r in conn(library_id).execute("SELECT name FROM tags ORDER BY name")]


def _tags_base(library_id: int, like: str | None, pin_only: bool = False):
    sql = """
        SELECT t.name AS name, t.pinned AS pinned, COUNT(DISTINCT i.path) AS n
        FROM tags t
        JOIN item_tags it ON it.tag_id = t.id
        JOIN items i ON i.id = it.item_id AND i.status='active'
        WHERE i.library_id = ?
    """
    args: list = [library_id]
    if like:
        sql += " AND lower(t.name) LIKE ?"
        args.append(like)
    if pin_only:
        sql += " AND t.pinned = 1"
    sql += " GROUP BY t.id"
    return sql, args


def tags_page(library_id: int, q: str | None = None, sort: str = "count",
              limit: int = 200, offset: int = 0) -> dict:
    """标签分页：置顶单独给一份，其余按数量/名称分页 —— 几千个标签也拖不死前端。"""
    c = conn(library_id)
    like = f"%{(q or '').strip().lower()}%" if (q or "").strip() else None
    base, args = _tags_base(library_id, like)
    order = "n DESC, t.name" if (sort or "count") == "count" else "t.name COLLATE NOCASE"
    rows = c.execute(f"{base} ORDER BY {order} LIMIT ? OFFSET ?",
                     args + [int(limit), int(offset)]).fetchall()
    total = c.execute(f"SELECT COUNT(*) FROM ({base})", args).fetchone()[0]
    pbase, pargs = _tags_base(library_id, like, pin_only=True)
    pins = c.execute(f"{pbase} ORDER BY n DESC, t.name", pargs).fetchall()
    return {"total": int(total),
            "tags": [{"name": r["name"], "count": r["n"], "pinned": bool(r["pinned"])}
                     for r in rows],
            "pinned": [{"name": r["name"], "count": r["n"], "pinned": True} for r in pins]}


def set_pin(library_id: int, name: str, pinned: bool) -> dict:
    nm = (name or "").strip()
    if not nm:
        raise ValueError("标签名不能为空")
    c = conn(library_id)
    c.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (nm,))
    c.execute("UPDATE tags SET pinned=? WHERE name=?", (1 if pinned else 0, nm))
    c.commit()
    return {"name": nm, "pinned": bool(pinned)}


def path_tag_sets(library_id: int, include=None, exclude=None) -> set[str] | None:
    """检索侧标签过滤：返回允许的 path 集合（None = 不过滤）。"""
    inc = {t.strip().lower() for t in _norm_tag_list(include)}
    exc = {t.strip().lower() for t in _norm_tag_list(exclude)}
    if not inc and not exc:
        return None
    rows = conn(library_id).execute(
        "SELECT DISTINCT i.path AS path, lower(t.name) AS name "
        "FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        "JOIN items i ON i.id = it.item_id "
        "WHERE i.library_id=? AND i.status='active'", (library_id,)).fetchall()
    have: dict[str, set[str]] = {}
    for r in rows:
        have.setdefault(r["path"], set()).add(r["name"])
    out = set()
    for p, names in have.items():
        if inc and not inc <= names:
            continue
        if exc and (exc & names):
            continue
        out.add(p)
    return out


def paths_with_all_tags(library_id: int, tags: list[str]) -> set[str] | None:
    return path_tag_sets(library_id, tags, None)


# ------------------------------------------------------------------ 虚拟文件

VIRTUAL_PREFIX = "prism://virtual/"


def vpath(vid: int) -> str:
    return f"{VIRTUAL_PREFIX}{vid}"


def is_virtual_path(path: str | None) -> bool:
    return bool(path) and str(path).startswith(VIRTUAL_PREFIX)


def vid_of_path(path: str | None) -> int | None:
    if not is_virtual_path(path):
        return None
    try:
        return int(str(path)[len(VIRTUAL_PREFIX):])
    except (TypeError, ValueError):
        return None


def _vf_row(r) -> dict:
    d = dict(r)
    try:
        d["related"] = json.loads(d.get("related") or "[]")
    except (TypeError, ValueError):
        d["related"] = []
    return d


def create_virtual(library_id: int, title: str, description: str = "",
                   cover: str = "", related: list[str] | None = None) -> int:
    c = conn(library_id)
    now = _now()
    cur = c.execute(
        "INSERT INTO virtual_files(library_id, title, description, cover, related, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (library_id, title, description, cover,
         json.dumps(related or [], ensure_ascii=False), now, now))
    c.commit()
    return cur.lastrowid


def get_virtual(library_id: int, vid: int) -> dict | None:
    r = conn(library_id).execute("SELECT * FROM virtual_files WHERE id=? AND library_id=?",
                                 (vid, library_id)).fetchone()
    return _vf_row(r) if r else None


def find_virtual(vid: int) -> tuple[int, dict] | None:
    for lid in all_library_ids():
        v = get_virtual(lid, vid)
        if v:
            return lid, v
    return None


def list_virtuals(library_id: int) -> list[dict]:
    rows = conn(library_id).execute(
        "SELECT * FROM virtual_files WHERE library_id=? ORDER BY id DESC", (library_id,)).fetchall()
    return [_vf_row(r) for r in rows]


def update_virtual(library_id: int, vid: int, **fields) -> bool:
    allowed = ("title", "description", "cover", "related")
    sets: list[str] = []
    args: list = []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "related":
            v = json.dumps(v or [], ensure_ascii=False)
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    args.append(_now())
    args += [vid, library_id]
    c = conn(library_id)
    c.execute(f"UPDATE virtual_files SET {', '.join(sets)} WHERE id=? AND library_id=?", args)
    c.commit()
    return True


def delete_virtual(library_id: int, vid: int) -> None:
    c = conn(library_id)
    n = c.execute("SELECT COUNT(*) n FROM items WHERE library_id=? AND path=?",
                  (library_id, vpath(vid))).fetchone()["n"]
    c.execute("DELETE FROM items WHERE library_id=? AND path=?", (library_id, vpath(vid)))
    c.execute("DELETE FROM virtual_files WHERE id=? AND library_id=?", (vid, library_id))
    c.commit()
    note_deletes(library_id, n)
    reclaim(library_id)


def virtual_chunk0_id(library_id: int, vid: int) -> int | None:
    r = conn(library_id).execute(
        "SELECT id FROM items WHERE library_id=? AND path=? AND chunk_index=0",
        (library_id, vpath(vid))).fetchone()
    return r["id"] if r else None


def virtual_items(library_id: int, vid: int) -> list[dict]:
    rows = conn(library_id).execute(
        "SELECT id, chunk_index, modality, chunk_text, meta FROM items "
        "WHERE library_id=? AND path=? ORDER BY chunk_index",
        (library_id, vpath(vid))).fetchall()
    return [dict(x) for x in rows]


def virtual_meta_map(library_id: int) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in conn(library_id).execute(
            "SELECT id, title, cover, description FROM virtual_files WHERE library_id=?",
            (library_id,)):
        out[r["id"]] = {"title": r["title"] or "", "cover": r["cover"] or "",
                        "description": r["description"] or ""}
    return out


def _norm_path(p: str) -> str:
    try:
        return str(Path(p).resolve()).replace("\\", "/").lower()
    except OSError:
        return str(p).replace("\\", "/").lower()


def virtual_asset_paths(library_id: int) -> set[str]:
    """某库里虚拟文件挂过的真实路径（配图/音视频 + 关联文件），规范化后返回。"""
    out: set[str] = set()
    c = conn(library_id)
    for r in c.execute("SELECT meta FROM items WHERE path LIKE ?", (VIRTUAL_PREFIX + "%",)):
        try:
            m = json.loads(r["meta"] or "{}")
        except (TypeError, ValueError):
            continue
        if m.get("src"):
            out.add(_norm_path(m["src"]))
    for r in c.execute("SELECT related FROM virtual_files"):
        try:
            rel = json.loads(r["related"] or "[]")
        except (TypeError, ValueError):
            continue
        for q in rel:
            out.add(_norm_path(q))
    return out


# ------------------------------------------------------------------ 统计

def stats() -> dict:
    m = meta()
    libs = m.execute("SELECT COUNT(*) n FROM libraries").fetchone()["n"]
    items = missing = tags = disk = 0
    for lid in all_library_ids():
        try:
            c = conn(lid)
            items += c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
            missing += c.execute("SELECT COUNT(*) n FROM items WHERE status='missing'").fetchone()["n"]
            tags += c.execute("SELECT COUNT(*) n FROM tags").fetchone()["n"]
            disk += lib_usage(lid)["total_bytes"]
        except Exception:
            continue
    return {"libraries": libs, "items": items, "missing": missing,
            "tags": tags, "disk_bytes": disk}
