"""Prism · 棱镜 — SQLite 数据层

一张库表 + 一张条目表（条目 = 一个文件的一段）。
库只存索引关系，源文件永远留在原处。
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from config import DB_PATH, EMBED_DIM, EMBED_MODEL

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA auto_vacuum = INCREMENTAL;   -- 删除后能真正回收磁盘空间（否则空闲页永远不还）

CREATE TABLE IF NOT EXISTS libraries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',
    primary_type TEXT NOT NULL DEFAULT 'mixed',   -- image / text / audio / mixed
    embed_model  TEXT NOT NULL DEFAULT '',
    embed_dim    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,                 -- 源文件绝对路径
    chunk_index INTEGER NOT NULL DEFAULT 0,    -- 0 = 整文件；>0 = 第 N 段
    modality    TEXT NOT NULL DEFAULT 'other', -- image / audio / text / other
    chunk_text  TEXT NOT NULL DEFAULT '',      -- 文本段内容（便于高亮展示）
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',      -- path+mtime+size 的哈希
    status      TEXT NOT NULL DEFAULT 'active',-- active / missing
    meta        TEXT NOT NULL DEFAULT '{}',    -- JSON：标签等
    vec         BLOB,                          -- float16 向量
    added_at    TEXT NOT NULL,
    UNIQUE(library_id, path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_items_lib      ON items(library_id);
CREATE INDEX IF NOT EXISTS idx_items_path     ON items(path);
CREATE INDEX IF NOT EXISTS idx_items_status   ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_modal    ON items(modality);

-- 虚拟文件：内部自建的「条目」，用不大的附加信息（标题/描述/配图/音视频）
-- 去关联本地的大文件。它自己不占 items 的额外结构 —— 它的每个片段都写进 items，
-- 且**共用一个伪路径** prism://virtual/<id>，这样「按文件」合并、检索、标签全部直接复用。
CREATE TABLE IF NOT EXISTS virtual_files (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id  INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  title       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  cover       TEXT NOT NULL DEFAULT '',
  related     TEXT NOT NULL DEFAULT '[]',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vf_lib ON virtual_files(library_id);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_item ON item_tags(item_id);

-- 导入任务：落盘保存，服务重启后仍能看到进度与失败清单
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    library_id   INTEGER,
    state        TEXT    NOT NULL DEFAULT 'running',   -- running / done / error / cancelled
    total        INTEGER NOT NULL DEFAULT 0,
    done         INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    failed       TEXT    NOT NULL DEFAULT '[]',        -- JSON 数组
    skipped      TEXT    NOT NULL DEFAULT '[]',        -- JSON 数组
    current      TEXT    NOT NULL DEFAULT '',
    error        TEXT,
    started      REAL,
    finished     REAL,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    updated      REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started);
"""


def conn() -> sqlite3.Connection:
    """每线程一个连接（http.server 是多线程的）。"""
    c = getattr(_local, "conn", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        _local.conn = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)
    conn().commit()


def rollback() -> None:
    """丢弃当前线程连接上未提交的事务（出错路径必须调用）。

    为什么必须有：sqlite3 在执行 INSERT/UPDATE 前会隐式 BEGIN，而语句报错
    （例如 UNIQUE 冲突）**不会**自动回滚 —— 写锁就留在这个连接手里，
    整个库对外变成只读，连 Prism 自己都写不进去（只能重启服务才解开）。
    """
    try:
        conn().rollback()
    except Exception:
        pass


# ---------------------------------------------------------------- 任务持久化

def save_job(j: dict) -> None:
    """把导入任务写进数据库（调用方负责节流，不是每次都写）。"""
    c = conn()
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
    rows = conn().execute(
        "SELECT * FROM jobs ORDER BY COALESCE(started, 0) DESC LIMIT ?", (limit,)).fetchall()
    return [_job_row_to_dict(r) for r in rows]


def get_job(job_id: str) -> dict | None:
    r = conn().execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _job_row_to_dict(r) if r else None


def set_job_cancelled(job_id: str, flag: bool = True) -> None:
    c = conn()
    c.execute("UPDATE jobs SET cancelled=?, updated=? WHERE job_id=?",
              (1 if flag else 0, time.time(), job_id))
    c.commit()


def prune_jobs(keep: int = 200) -> int:
    """只留最近 N 条任务记录，防无限增长。"""
    c = conn()
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
    """删掉超过 N 天的任务记录（即使总数没超上限，也不能一直留着）。"""
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    c = conn()
    before = c.total_changes
    c.execute("DELETE FROM jobs WHERE COALESCE(started, 0) < ?", (cutoff,))
    removed = c.total_changes - before
    c.commit()
    return removed


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 库

def create_library(name: str, description: str = "", primary_type: str = "mixed") -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO libraries(name, description, primary_type, embed_model, embed_dim, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (name, description, primary_type, EMBED_MODEL, EMBED_DIM, _now()))
    c.commit()
    return cur.lastrowid


def list_libraries() -> list[dict]:
    c = conn()
    rows = c.execute("""
        SELECT l.*, 
               (SELECT COUNT(*) FROM items i WHERE i.library_id = l.id)  AS item_count,
               (SELECT COUNT(DISTINCT i.path) FROM items i WHERE i.library_id = l.id) AS file_count,
               (SELECT COUNT(*) FROM items i WHERE i.library_id = l.id AND i.status='missing') AS missing_count
        FROM libraries l ORDER BY l.id
    """).fetchall()
    return [dict(r) for r in rows]


def get_library(lib_id: int) -> dict | None:
    r = conn().execute("SELECT * FROM libraries WHERE id=?", (lib_id,)).fetchone()
    return dict(r) if r else None


def update_library(lib_id: int, **fields) -> bool:
    allowed = {"name", "description", "primary_type"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    sql = "UPDATE libraries SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
    c = conn()
    c.execute(sql, (*sets.values(), lib_id))
    c.commit()
    return True


def delete_library(lib_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM items WHERE library_id=?", (lib_id,))
    c.execute("DELETE FROM libraries WHERE id=?", (lib_id,))
    c.commit()
    _reclaim()                       # 立刻把删掉的空间还给文件


def _reclaim() -> None:
    """删除数据后把空闲页真正还给文件。

    auto_vacuum=INCREMENTAL 时，incremental_vacuum 只回收空闲页、不重写整库，
    代价远小于 VACUUM。回收属于优化，失败不应让删除操作本身报错。
    """
    try:
        conn().execute("PRAGMA incremental_vacuum")
    except Exception:
        pass


# ------------------------------------------------------------------ 条目

def upsert_item(library_id: int, path: str, chunk_index: int, modality: str,
                vec_bytes: bytes, chunk_text: str = "", size: int = 0,
                mtime: float = 0.0, fingerprint: str = "", meta: dict | None = None) -> int:
    c = conn()
    # 允许 vec=None：虚拟文件的「标题」片段**故意不走向量**（标题只触发关键词），
    # 存成 SQL NULL，这样 all_vectors() 的 `vec IS NOT NULL` 会自动把它排除。
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


def delete_item(item_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM items WHERE id=?", (item_id,))
    c.commit()


def delete_items_by_path(library_id: int, path: str) -> None:
    c = conn()
    c.execute("DELETE FROM items WHERE library_id=? AND path=?", (library_id, path))
    c.commit()


def get_item(item_id: int) -> dict | None:
    r = conn().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return dict(r) if r else None


def path_is_indexed(path: str) -> bool:
    """这个路径是否已在任一库里建过索引。

    用于 /api/open 的安全校验：只允许用系统默认程序打开已索引的文件，
    免得 Prism 变成一个"任意路径都能拉起来"的启动器。
    建库时路径可能存成正斜杠或反斜杠，所以两种写法都查。
    """
    c = conn()
    variants = tuple({path, path.replace("/", "\\"), path.replace("\\", "/")})
    q = ",".join("?" * len(variants))
    row = c.execute(f"SELECT 1 FROM items WHERE path IN ({q}) LIMIT 1", variants).fetchone()
    return row is not None


def search_keyword_rows(library_id: int, query: str,
                        modality: str | None = None, limit: int = 3000) -> list[dict]:
    """朴素子串匹配：path 或 chunk_text 含查询串即命中（大小写不敏感）。

    为什么需要：向量检索按"整体意思像不像"打分，对「一个确切的名字」
    「文件后缀」「你记得的一句原话」这类查询天然弱（实测搜确切歌名，
    正确答案只排第三）。这一路不走向量，纯 SQL 子串匹配来兜底。
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    pat = f"%{q}%"
    # 🔴 第三路：标签。用户给文件打的标签必须能被搜到 —— 否则标签就是死数据。
    #    之前这里只查 path / chunk_text，导致「给图片打了标签却搜不出来」。
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
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def list_missing(library_id: int) -> list[dict]:
    """该库里所有「原文件已找不到」的条目（按路径聚合）。"""
    rows = conn().execute(
        "SELECT path, modality, COUNT(*) n FROM items "
        "WHERE library_id=? AND status='missing' GROUP BY path ORDER BY path",
        (library_id,)).fetchall()
    return [dict(r) for r in rows]


def purge_missing(library_id: int) -> int:
    """删掉该库里全部 status='missing' 的索引记录。

    **只动数据库，绝不碰磁盘上的任何文件**（这些文件本来就找不到了）。
    不做自动触发 —— 只有人明确点「移除失效」才执行，避免外接盘/网络盘
    临时没挂上时把索引误清空。
    """
    c = conn()
    before = c.total_changes
    c.execute("DELETE FROM items WHERE library_id=? AND status='missing'", (library_id,))
    n = c.total_changes - before
    c.commit()
    if n:
        _reclaim()                   # 真删掉了东西才回收
    return n


def known_fingerprints(library_id: int) -> dict[str, str]:
    """{path: fingerprint} —— 用于增量导入时判断是否需要重新嵌入。"""
    rows = conn().execute(
        "SELECT path, fingerprint FROM items WHERE library_id=? AND chunk_index=0",
        (library_id,)).fetchall()
    return {r["path"]: r["fingerprint"] for r in rows}


def count_vectors(library_id: int, modality: str | None = None) -> int:
    """数一下参与向量比对的行数。

    近似重复的规模护栏要用它 —— 别为了拿个数就把全部向量 load 进内存。
    """
    sql = "SELECT COUNT(*) n FROM items WHERE library_id=? AND status='active' AND vec IS NOT NULL"
    args: list = [library_id]
    if modality:
        sql += " AND modality=?"
        args.append(modality)
    return conn().execute(sql, args).fetchone()["n"]


def all_vectors(library_id: int, modality: str | None = None) -> list[sqlite3.Row]:
    """取某库全部 (id, path, chunk_index, modality, chunk_text, vec_blob)。"""
    sql = ("SELECT id, path, chunk_index, modality, chunk_text, vec "
           "FROM items WHERE library_id=? AND status='active' AND vec IS NOT NULL")
    args: list = [library_id]
    if modality:
        sql += " AND modality=?"
        args.append(modality)
    return conn().execute(sql, args).fetchall()


def move_items(item_ids: list[int], target_library_id: int) -> int:
    """跨库移动条目（同路径重复则覆盖）。"""
    c = conn()
    moved = 0
    for iid in item_ids:
        r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        if not r:
            continue
        c.execute("""
            INSERT INTO items(library_id, path, chunk_index, modality, chunk_text, size, mtime,
                              fingerprint, status, meta, vec, added_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(library_id, path, chunk_index) DO UPDATE SET
                modality=excluded.modality, chunk_text=excluded.chunk_text, vec=excluded.vec,
                size=excluded.size, mtime=excluded.mtime, fingerprint=excluded.fingerprint,
                status='active', meta=excluded.meta
        """, (target_library_id, r["path"], r["chunk_index"], r["modality"], r["chunk_text"],
              r["size"], r["mtime"], r["fingerprint"], r["status"], r["meta"],
              r["vec"], _now()))
        c.execute("DELETE FROM items WHERE id=?", (iid,))
        moved += 1
    c.commit()
    return moved


def copy_items(item_ids: list[int], target_library_id: int) -> int:
    c = conn()
    copied = 0
    for iid in item_ids:
        r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        if not r:
            continue
        c.execute("""
            INSERT INTO items(library_id, path, chunk_index, modality, chunk_text, size, mtime,
                              fingerprint, status, meta, vec, added_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(library_id, path, chunk_index) DO UPDATE SET
                modality=excluded.modality, chunk_text=excluded.chunk_text, vec=excluded.vec,
                status='active', meta=excluded.meta
        """, (target_library_id, r["path"], r["chunk_index"], r["modality"], r["chunk_text"],
              r["size"], r["mtime"], r["fingerprint"], r["status"], r["meta"],
              r["vec"], _now()))
        copied += 1
    c.commit()
    return copied


def mark_missing(library_id: int, path: str) -> None:
    c = conn()
    c.execute("UPDATE items SET status='missing' WHERE library_id=? AND path=?",
              (library_id, path))
    c.commit()


# ------------------------------------------------------------------ 标签

def add_tag(item_id: int, tag: str) -> None:
    c = conn()
    c.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
    tid = c.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()["id"]
    c.execute("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES (?,?)", (item_id, tid))
    c.commit()


def remove_tag(item_id: int, tag: str) -> None:
    c = conn()
    r = c.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()
    if r:
        c.execute("DELETE FROM item_tags WHERE item_id=? AND tag_id=?", (item_id, r["id"]))
        c.commit()


def item_tags(item_id: int) -> list[str]:
    rows = conn().execute("""
        SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id
        WHERE it.item_id=? ORDER BY t.name""", (item_id,)).fetchall()
    return [r["name"] for r in rows]


def all_tags() -> list[str]:
    return [r["name"] for r in conn().execute("SELECT name FROM tags ORDER BY name").fetchall()]


def tags_with_counts(library_id: int | None = None) -> list[dict]:
    """所有标签 + 各自挂了多少个**文件**（按 path 去重），条目多的排前面。

    只数当前库里的，这样标签条跟着切库变 —— 在电影库里不该看到图片库的标签。
    同一文件的多段各挂一次同一个标签时只算 1，避免数字虚高。
    """
    if library_id is None:
        rows = conn().execute(
            "SELECT t.name AS name, COUNT(DISTINCT i.path) AS n "
            "FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "JOIN items i ON i.id = it.item_id "
            "WHERE i.status='active' GROUP BY t.name ORDER BY n DESC, t.name").fetchall()
    else:
        rows = conn().execute(
            "SELECT t.name AS name, COUNT(DISTINCT i.path) AS n "
            "FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "JOIN items i ON i.id = it.item_id "
            "WHERE i.library_id=? AND i.status='active' "
            "GROUP BY t.name ORDER BY n DESC, t.name", (library_id,)).fetchall()
    return [{"name": r["name"], "count": r["n"]} for r in rows]


def paths_with_all_tags(library_id: int, tags: list[str]) -> set[str] | None:
    """同时带齐**全部**所选标签的文件路径集合。

    语义说明：标签挂在段（item）上，但用户心里想的是"这个文件"，
    所以先按 path 汇总出「每个文件有哪些标签」，再要求**包含所选的全部标签**（AND）。
    多选就是"同时满足"—— 选「白发」+「校服」只出同时有两个标签的文件。

    tags 为空 → 返回 None（表示不做过滤，调用方直接放行）。
    """
    want = {t.strip().lower() for t in (tags or []) if t and t.strip()}
    if not want:
        return None
    rows = conn().execute(
        "SELECT DISTINCT i.path AS path, lower(t.name) AS name "
        "FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        "JOIN items i ON i.id = it.item_id "
        "WHERE i.library_id=? AND i.status='active'", (library_id,)).fetchall()
    have: dict[str, set[str]] = {}
    for r in rows:
        have.setdefault(r["path"], set()).add(r["name"])
    return {p for p, names in have.items() if want <= names}


# ------------------------------------------------------------------ 虚拟文件

VIRTUAL_PREFIX = "prism://virtual/"


def vpath(vid: int) -> str:
    """虚拟文件在 items 表里的伪路径 —— **它的所有片段共用这一条**，
    这样「按文件」合并 / 检索 / 标签 / 去重全部零改动直接复用。"""
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
    c = conn()
    now = _now()
    cur = c.execute(
        "INSERT INTO virtual_files(library_id, title, description, cover, related, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (library_id, title, description, cover,
         json.dumps(related or [], ensure_ascii=False), now, now))
    c.commit()
    return cur.lastrowid


def get_virtual(vid: int) -> dict | None:
    r = conn().execute("SELECT * FROM virtual_files WHERE id=?", (vid,)).fetchone()
    return _vf_row(r) if r else None


def list_virtuals(library_id: int) -> list[dict]:
    rows = conn().execute(
        "SELECT * FROM virtual_files WHERE library_id=? ORDER BY id DESC",
        (library_id,)).fetchall()
    return [_vf_row(r) for r in rows]


def update_virtual(vid: int, **fields) -> bool:
    allowed = ("title", "description", "cover", "related", "library_id")
    sets: list[str] = []
    args: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "related":
            v = json.dumps(v or [], ensure_ascii=False)
        sets.append(f"{k}=?")
        args.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    args.append(_now())
    args.append(vid)
    c = conn()
    c.execute(f"UPDATE virtual_files SET {', '.join(sets)} WHERE id=?", args)
    c.commit()
    return True


def delete_virtual(vid: int) -> None:
    """删虚拟文件：元数据 + 伪造路径下的全部片段（含向量）。
    **真实媒体文件一个都不动** —— 我们只存索引。"""
    c = conn()
    row = c.execute("SELECT library_id FROM virtual_files WHERE id=?", (vid,)).fetchone()
    if row:
        c.execute("DELETE FROM items WHERE library_id=? AND path=?",
                  (row["library_id"], vpath(vid)))
    c.execute("DELETE FROM virtual_files WHERE id=?", (vid,))
    c.commit()


def virtual_chunk0_id(vid: int) -> int | None:
    """虚拟文件「标题片段」（chunk 0）的 item id。

    标签是挂在**片段**上的，而一个虚拟文件有多个片段（标题/描述/配图…）。
    要让「给这个虚拟文件打标签」有一个确定的落点，就统一挂在 chunk 0 上 ——
    检索时再把标签读出来盖到所有片段上（见 search._hit_tags）。
    """
    r = conn().execute(
        "SELECT i.id FROM items i JOIN virtual_files v ON v.library_id = i.library_id "
        "WHERE i.path = ? AND i.chunk_index = 0", (vpath(vid),)).fetchone()
    return r["id"] if r else None


def virtual_items(vid: int) -> list[dict]:
    """这个虚拟文件当前的片段（按 chunk_index 排）。"""
    r = conn().execute("SELECT library_id FROM virtual_files WHERE id=?", (vid,)).fetchone()
    if not r:
        return []
    rows = conn().execute(
        "SELECT id, chunk_index, modality, chunk_text, meta FROM items "
        "WHERE library_id=? AND path=? ORDER BY chunk_index",
        (r["library_id"], vpath(vid))).fetchall()
    return [dict(x) for x in rows]


def virtual_meta_map(library_id: int) -> dict[int, dict]:
    """{vid: {title, cover, description}} —— 检索结果把伪路径换成人类可读的标题/封面。"""
    out: dict[int, dict] = {}
    for r in conn().execute(
            "SELECT id, title, cover, description FROM virtual_files WHERE library_id=?",
            (library_id,)):
        out[r["id"]] = {"title": r["title"] or "", "cover": r["cover"] or "",
                        "description": r["description"] or ""}
    return out


def stats() -> dict:
    c = conn()
    return {
        "libraries": c.execute("SELECT COUNT(*) n FROM libraries").fetchone()["n"],
        "items":     c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"],
        "missing":   c.execute("SELECT COUNT(*) n FROM items WHERE status='missing'").fetchone()["n"],
        "tags":      c.execute("SELECT COUNT(*) n FROM tags").fetchone()["n"],
    }
