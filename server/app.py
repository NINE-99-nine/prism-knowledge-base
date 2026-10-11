"""Prism · 棱镜 — HTTP 服务（纯标准库 http.server，零第三方依赖）

一个进程同时干两件事：
    1) 提供 /api/*  给前端和 AI 调用
    2) 提供 /   静态前端，以及 /api/file  —— 代转发源文件
       （浏览器不能直接读本地磁盘，图片缩略图/音频播放必须由服务中转）

安全：/api/file 只允许读取「已在本库索引中」的文件，防止变成任意文件读取器。
"""
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import embedder
import importer
import search
import settings
import config
from config import HOST, PORT, WEB_DIR
from embedder import health as embed_health

MAX_BODY = 96 * 1024 * 1024     # 请求体上限（拖拽检索会把文件以 base64 送进来，膨胀约 33%）


class Api:
    """所有业务逻辑，与 HTTP 细节解耦（便于将来换框架）。"""

    # ---- 元信息 ----
    @staticmethod
    def health() -> dict:
        ok, msg = embed_health()
        return {
            "prism": "ok",
            "embed": {"ok": ok, "message": msg},
            "stats": db.stats(),
        }

    @staticmethod
    def stats() -> dict:
        return db.stats()

    # ---- 库 ----
    @staticmethod
    def libraries() -> list:
        return db.list_libraries()

    @staticmethod
    def library_create(body: dict) -> dict:
        name = (body.get("name") or "").strip()
        if not name:
            raise ValueError("库名不能为空")
        lid = db.create_library(name,
                                (body.get("description") or "").strip(),
                                body.get("primary_type") or "mixed")
        return {"id": lid}

    @staticmethod
    def library_update(lid: int, body: dict) -> dict:
        db.update_library(lid, name=body.get("name"), description=body.get("description"),
                          primary_type=body.get("primary_type"))
        return {"ok": True}

    @staticmethod
    def library_delete(lid: int) -> dict:
        db.delete_library(lid)
        return {"ok": True}

    @staticmethod
    def library_items(lid: int, limit: int = 200, offset: int = 0) -> dict:
        c = db.conn()
        rows = c.execute(
            "SELECT id, path, chunk_index, modality, size, status, added_at, "
            "       substr(chunk_text,1,200) AS text_preview "
            "FROM items WHERE library_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (lid, limit, offset)).fetchall()
        total = c.execute("SELECT COUNT(*) n FROM items WHERE library_id=?", (lid,)).fetchone()["n"]
        return {"total": total, "items": [dict(r) for r in rows]}

    # ---- 文件级视图（浏览用：一个文件一行）----
    @staticmethod
    def library_files(lid: int, limit: int = 300, offset: int = 0,
                      modality: str | None = None) -> dict:
        """按 path 聚合 —— 用户关心「库里有哪些文件」，不是「有哪些向量块」。"""
        c = db.conn()
        where, params = "WHERE library_id=?", [lid]
        if modality:
            where += " AND modality=?"
            params.append(modality)
        rows = c.execute(f"""
            SELECT path,
                   MAX(id)          AS id,
                   modality,
                   COUNT(*)         AS chunks,
                   MAX(size)        AS size,
                   MAX(mtime)       AS mtime,
                   MAX(added_at)    AS added_at,
                   MAX(CASE WHEN chunk_index = 0 THEN substr(chunk_text, 1, 220) END) AS preview,
                   CASE WHEN MIN(status)='missing' THEN 'missing' ELSE 'ok' END AS status
            FROM items {where}
            GROUP BY path
            ORDER BY MAX(id) DESC
            LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
        total = c.execute(
            f"SELECT COUNT(DISTINCT path) n FROM items {where}", params).fetchone()["n"]
        return {"total": total, "files": [dict(r) for r in rows]}

    @staticmethod
    def file_chunks(lid: int, path: str) -> dict:
        """某个文件在该库里被切成哪些段（展开查看用）。"""
        if not path:
            raise ValueError("缺少 path")
        rows = db.conn().execute(
            "SELECT id, chunk_index, modality, status, "
            "       length(chunk_text) AS text_len, substr(chunk_text,1,180) AS preview "
            "FROM items WHERE library_id=? AND path=? ORDER BY chunk_index",
            (lid, path)).fetchall()
        return {"path": path, "count": len(rows), "chunks": [dict(r) for r in rows]}

    @staticmethod
    def file_delete(body: dict) -> dict:
        """把一个文件在该库里的全部索引删掉（只删索引，绝不动源文件）。"""
        lid = int(body.get("library_id") or 0)
        path = body.get("path") or ""
        if not lid or not path:
            raise ValueError("需要 library_id 与 path")
        n = db.conn().execute(
            "SELECT COUNT(*) n FROM items WHERE library_id=? AND path=?",
            (lid, path)).fetchone()["n"]
        db.delete_items_by_path(lid, path)
        return {"removed": n, "path": path}

    # ---- 设置（嵌入服务等，可运行时改）----
    @staticmethod
    def settings_get() -> dict:
        return settings.describe()

    @staticmethod
    def settings_save(body: dict) -> dict:
        before = settings.load()
        after = settings.save(body)
        return {
            "ok": True,
            "settings": after,
            "changed": {k: {"from": before.get(k), "to": after.get(k)}
                        for k in after if before.get(k) != after.get(k)},
        }

    @staticmethod
    def settings_reset() -> dict:
        return {"ok": True, "settings": settings.reset()}

    @staticmethod
    def settings_test(body: dict) -> dict:
        """拿给定配置真发一次嵌入请求，验证能不能用、维度对不对。"""
        import time as _t
        cur = settings.load()
        cand = {
            "embed_api":   (body.get("embed_api") or cur["embed_api"]).strip(),
            "embed_model": (body.get("embed_model") or cur["embed_model"]).strip(),
            "embed_dim":   body.get("embed_dim") or cur["embed_dim"],
        }
        snapshot = dict(cur)
        try:
            settings.save(cand)                 # 临时生效以复用 embedder
            t0 = _t.time()
            vec = embedder.embed_text("连接测试")
            ms = int((_t.time() - t0) * 1000)
            dim = len(vec)
            expect = int(cand["embed_dim"])
            return {
                "ok": True, "dim": dim, "expected_dim": expect, "match": dim == expect,
                "ms": ms,
                "message": (f"连接成功 · 返回 {dim} 维 · {ms} ms" +
                            ("" if dim == expect else f" · ⚠ 与配置的 {expect} 维不一致")),
            }
        except Exception as e:
            return {"ok": False, "message": f"{type(e).__name__}: {str(e)[:240]}"}
        finally:
            settings.save(snapshot)             # 测完还原，别把失败配置留下

    # ---- 导入 ----
    @staticmethod
    def import_start(body: dict) -> dict:
        lid = int(body.get("library_id") or 0)
        if not db.get_library(lid):
            raise ValueError("库不存在")
        paths = body.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            raise ValueError("没有要导入的路径")
        job = importer.start_import(lid, paths, bool(body.get("recursive", True)))
        return {"job_id": job}

    @staticmethod
    def job(job_id: str) -> dict:
        j = importer.get_job(job_id)
        if not j:
            raise ValueError("任务不存在")
        return j
    @staticmethod
    def jobs() -> list:
        return importer.list_jobs()

    # ---- 检索 ----
    @staticmethod
    def search_text(body: dict) -> dict:
        lid = int(body.get("library_id") or 0)
        q = (body.get("query") or "").strip()
        if not q:
            raise ValueError("查询不能为空")
        grouped = bool(body.get("group"))
        mode = (body.get("mode") or "mixed").lower()
        if mode not in ("semantic", "keyword", "mixed"):
            mode = "mixed"
        res = search.search_any(lid, q, int(body.get("top_k") or 12),
                                body.get("modality") or None, grouped, mode)
        return {"query": q, "mode": mode, "count": len(res),
                "grouped": grouped, "results": res}

    @staticmethod
    def search_blob(body: dict) -> dict:
        """拖拽进来的文件 → 按后缀嵌入 → 搜库（任意模态互搜）。"""
        import embedder
        lid = int(body.get("library_id") or 0)
        raw_b64 = body.get("data") or ""
        filename = (body.get("filename") or "").strip()
        if not raw_b64:
            raise ValueError("缺少文件内容")
        try:
            raw = base64.b64decode(raw_b64)
        except Exception:
            raise ValueError("文件内容不是合法 base64")
        if not raw:
            raise ValueError("文件内容为空")

        tmp_root = config.DATA_DIR / "tmp" / "q"
        vec = embedder.embed_blob(filename, raw, tmp_root=tmp_root)
        res = search.search_by_vector(lid, vec, int(body.get("top_k") or 12),
                                      body.get("modality") or None)
        return {"query": filename, "count": len(res),
                "size": len(raw), "results": res}

    @staticmethod
    def search_image(body: dict) -> dict:
        lid = int(body.get("library_id") or 0)
        p = body.get("path")
        if not p or not os.path.exists(p):
            raise ValueError("图片路径不存在")
        res = search.search_by_image(lid, p, int(body.get("top_k") or 12))
        return {"count": len(res), "results": res}

    @staticmethod
    def similar(body: dict) -> dict:
        res = search.similar_to_item(int(body.get("library_id") or 0),
                                     int(body.get("item_id") or 0),
                                     int(body.get("top_k") or 12))
        return {"count": len(res), "results": res}

    @staticmethod
    def duplicates(body: dict) -> dict:
        res = search.duplicates(int(body.get("library_id") or 0),
                                float(body.get("threshold") or 0.97),
                                int(body.get("limit") or 50),
                                bool(body.get("same_file")))
        return {"count": len(res), "pairs": res}

    # ---- 条目 ----
    @staticmethod
    def item_delete(item_id: int) -> dict:
        db.delete_item(item_id)
        return {"ok": True}

    @staticmethod
    def items_move(body: dict) -> dict:
        n = db.move_items([int(x) for x in body.get("item_ids") or []],
                          int(body.get("target_library_id") or 0))
        return {"moved": n}

    @staticmethod
    def items_copy(body: dict) -> dict:
        n = db.copy_items([int(x) for x in body.get("item_ids") or []],
                          int(body.get("target_library_id") or 0))
        return {"copied": n}

    @staticmethod
    def tag_add(item_id: int, body: dict) -> dict:
        tag = (body.get("tag") or "").strip()
        if not tag:
            raise ValueError("标签不能为空")
        db.add_tag(item_id, tag)
        return {"tags": db.item_tags(item_id)}

    @staticmethod
    def tag_remove(item_id: int, tag: str) -> dict:
        db.remove_tag(item_id, tag)
        return {"tags": db.item_tags(item_id)}

    @staticmethod
    def tags() -> list:
        return db.all_tags()

    @staticmethod
    def rescan(lid: int) -> dict:
        return importer.rescan_missing(lid)

    @staticmethod
    def missing(lid: int) -> dict:
        """列出本库里原文件已找不到的条目。"""
        rows = db.list_missing(lid)
        return {"count": len(rows), "files": rows}

    @staticmethod
    def purge_missing(lid: int) -> dict:
        """移除本库全部失效索引（只动数据库，不碰磁盘）。"""
        n = db.purge_missing(lid)
        return {"removed": n}

    @staticmethod
    def item_detail(item_id: int) -> dict:
        it = db.get_item(item_id)
        if not it:
            raise ValueError("条目不存在")
        it.pop("vec", None)          # 向量不上前端
        it["tags"] = db.item_tags(item_id)
        return it

    # ---- 系统集成 ----
    @staticmethod
    def pick(mode: str = "dir", kind: str = "") -> dict:
        """弹 Windows 原生选择框（浏览器拿不到拖入文件的真实路径）。

        kind 用于过滤文件类型："" = 全部；"text"/"image"/"audio" = 预设分组；
        "md,txt" = 指定扩展名。
        """
        script = Path(__file__).resolve().parent / "pick.py"
        try:
            r = subprocess.run([sys.executable, str(script), mode, str(Path.home()), kind],
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return {"paths": []}
        out = (r.stdout or "").strip().splitlines()
        if not out:
            return {"paths": [], "stderr": (r.stderr or "")[-200:]}
        try:
            return json.loads(out[-1])
        except Exception:
            return {"paths": [], "stderr": (r.stderr or "")[-200:]}

    @staticmethod
    def reveal(path: str) -> dict:
        """在资源管理器中定位文件。"""
        p = Path(path)
        if not p.exists():
            raise ValueError("文件不存在")
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(p)])
        return {"ok": True}

    @staticmethod
    def open_path(path: str) -> dict:
        """用系统默认程序打开文件（听歌/看图）。

        比 reveal 更严：只允许打开**已索引过**的文件，避免被当成
        「任意路径都能拉起的启动器」。
        """
        p = Path(path)
        if not p.exists():
            raise ValueError("文件不存在")
        if not db.path_is_indexed(str(p)):
            raise ValueError("该路径不在任何库的索引里，拒绝打开")
        if os.name == "nt":
            os.startfile(str(p))          # noqa: S606 - 仅对已索引文件调用
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True}


# ------------------------------------------------------------------ HTTP

# ---------------------------------------------------------------- 编码嗅探

def to_utf8(data: bytes) -> bytes:
    """把常见编码的文本字节转成 UTF-8，避免浏览器把全文显示成乱码。

    识别顺序：BOM → 本来就是 UTF-8 → \\x00 密度高（无 BOM 的 UTF-16）→ GBK 系。
    全都不是就原样返回（不假装能解）。
    """
    if not data:
        return data
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"),
                     (b"\xff\xfe\x00\x00", "utf-32"),
                     (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"),
                     (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(enc).encode("utf-8")
            except Exception:
                break
    try:
        data.decode("utf-8")
        return data
    except UnicodeDecodeError:
        pass
    if data.count(b"\x00") > len(data) * 0.25:      # 无 BOM 的 UTF-16
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return data.decode(enc).encode("utf-8")
            except Exception:
                continue
    for enc in ("gbk", "gb18030", "big5"):           # 中文老文件
        try:
            return data.decode(enc).encode("utf-8")
        except Exception:
            continue
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "Prism/1.0"

    # ---- 工具 ----
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code: int = 400):
        self._json({"error": str(msg)}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def log_message(self, fmt, *args):
        pass    # 静音：不把每条请求刷到控制台

    # ---- 路由 ----
    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        parts = [p for p in path.split("/") if p]

        if not parts or parts[0] != "api":
            return self._static(path)

        try:
            # /api/xxx
            seg = parts[1:]
            m = self.command
            if m == "HEAD":          # HEAD 走 GET 的路由（_send 里会跳过 body）
                m = "GET"

            if seg == ["health"]:
                return self._json(Api.health())
            if seg == ["stats"]:
                return self._json(Api.stats())

            if seg == ["libraries"]:
                if m == "GET":
                    return self._json(Api.libraries())
                if m == "POST":
                    return self._json(Api.library_create(self._body()))

            if len(seg) >= 2 and seg[0] == "libraries":
                lid = int(seg[1])
                if len(seg) == 2 and m in ("PATCH", "POST"):
                    return self._json(Api.library_update(lid, self._body()))
                if len(seg) == 2 and m == "DELETE":
                    return self._json(Api.library_delete(lid))
                if len(seg) == 3 and seg[2] == "items" and m == "GET":
                    return self._json(Api.library_items(
                        lid, int(qs.get("limit", ["200"])[0]), int(qs.get("offset", ["0"])[0])))
                if len(seg) == 3 and seg[2] == "rescan" and m == "POST":
                    return self._json(Api.rescan(lid))
                if len(seg) == 3 and seg[2] == "missing" and m == "GET":
                    return self._json(Api.missing(lid))
                if len(seg) == 3 and seg[2] == "purge-missing" and m == "POST":
                    return self._json(Api.purge_missing(lid))

            if seg == ["import"] and m == "POST":
                _tidy()                     # 每次导入前顺手清理 tmp 和过期任务记录
                return self._json(Api.import_start(self._body()))
            if seg == ["jobs"] and m == "GET":
                return self._json(Api.jobs())
            if len(seg) == 2 and seg[0] == "jobs" and m == "GET":
                return self._json(Api.job(seg[1]))
            if len(seg) == 3 and seg[0] == "jobs" and seg[2] == "cancel" and m == "POST":
                return self._json(importer.cancel_job(seg[1]))
            if len(seg) == 3 and seg[0] == "jobs" and seg[2] == "retry" and m == "POST":
                return self._json(importer.retry_failed(seg[1]))

            if seg == ["search"] and m == "POST":
                return self._json(Api.search_text(self._body()))
            if seg == ["search", "blob"] and m == "POST":
                return self._json(Api.search_blob(self._body()))
            if seg == ["search", "image"] and m == "POST":
                return self._json(Api.search_image(self._body()))
            if seg == ["similar"] and m == "POST":
                return self._json(Api.similar(self._body()))
            if seg == ["duplicates"] and m == "POST":
                return self._json(Api.duplicates(self._body()))

            if seg == ["items", "move"] and m == "POST":
                return self._json(Api.items_move(self._body()))
            if seg == ["items", "copy"] and m == "POST":
                return self._json(Api.items_copy(self._body()))

            if len(seg) >= 2 and seg[0] == "items":
                item_id = int(seg[1])
                if len(seg) == 2 and m == "DELETE":
                    return self._json(Api.item_delete(item_id))
                if len(seg) == 3 and seg[2] == "tags":
                    if m == "POST":
                        return self._json(Api.tag_add(item_id, self._body()))
                    if m == "DELETE":
                        return self._json(Api.tag_remove(item_id, qs.get("tag", [""])[0]))

            if seg == ["tags"] and m == "GET":
                return self._json(Api.tags())

            if len(seg) == 2 and seg[0] == "item" and m == "GET":
                return self._json(Api.item_detail(int(seg[1])))

            if seg == ["pick"] and m == "POST":
                return self._json(Api.pick(qs.get("mode", ["dir"])[0],
                                           qs.get("kind", [""])[0]))
            if seg == ["reveal"] and m == "POST":
                return self._json(Api.reveal(self._body().get("path", "")))
            if seg == ["open"] and m == "POST":
                return self._json(Api.open_path(self._body().get("path", "")))

            if seg == ["settings"]:
                if m == "GET":
                    return self._json(Api.settings_get())
                if m in ("POST", "PATCH"):
                    return self._json(Api.settings_save(self._body()))
            if seg == ["settings", "test"] and m == "POST":
                return self._json(Api.settings_test(self._body()))
            if seg == ["settings", "reset"] and m == "POST":
                return self._json(Api.settings_reset())

            if seg == ["files"] and m == "DELETE":
                return self._json(Api.file_delete(self._body()))

            if len(seg) >= 3 and seg[0] == "libraries" and seg[2] == "files":
                flid = int(seg[1])
                if len(seg) == 3 and m == "GET":
                    return self._json(Api.library_files(
                        flid, int(qs.get("limit", ["300"])[0]),
                        int(qs.get("offset", ["0"])[0]),
                        qs.get("modality", [""])[0] or None))
                if len(seg) == 4 and seg[3] == "chunks" and m == "GET":
                    return self._json(Api.file_chunks(flid, qs.get("path", [""])[0]))

            if seg == ["file"] and m == "GET":
                return self._serve_file(qs.get("path", [""])[0])

            return self._err("未知接口: " + path, 404)

        except ValueError as e:
            db.rollback()          # 语句报错后必须回滚，否则写锁会一直握着 → 整库被锁死
            return self._err(e)
        except Exception as e:
            db.rollback()
            return self._err(f"内部错误: {type(e).__name__}: {str(e)[:200]}", 500)

    # ---- 静态前端 ----
    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._err("非法路径", 403)
        if not target.is_file():
            # 前端是单页应用：未知路径回落到 index.html
            target = WEB_DIR / "index.html"
            if not target.is_file():
                return self._err("前端文件缺失", 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ---- 源文件代转发（只允许已索引的文件）----
    def _serve_file(self, raw_path: str):
        if not raw_path:
            return self._err("缺少 path 参数", 400)
        p = Path(raw_path)
        if not p.is_file():
            return self._err("文件不存在", 404)
        # 库里的 path 可能来自旧索引（正斜杠）或新导入（反斜杠），两种都查
        variants = {str(p.resolve()), str(p.resolve()).replace("\\", "/"),
                    raw_path, raw_path.replace("/", "\\")}
        q = " OR ".join(["path=?"] * len(variants))
        row = db.conn().execute(
            f"SELECT 1 FROM items WHERE {q} LIMIT 1", tuple(variants)).fetchone()
        if not row:
            return self._err("该文件不在任何库中（安全限制）", 403)

        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        is_text = (ctype.startswith("text/")
                   or ctype in ("application/json", "application/javascript"))

        total = p.stat().st_size
        start, end = 0, total - 1

        # ---- Range 支持：没有它，HTML5 播放器拖不动进度条 ----
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng)
        partial = False
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), total - 1)
            else:                                   # bytes=-N（最后 N 字节）
                start = max(0, total - int(m.group(2)))
            if start > end or start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            partial = True

        length = end - start + 1
        with open(p, "rb") as fh:
            fh.seek(start)
            data = fh.read(length)

        if is_text and not partial:
            data = to_utf8(data)

        # 超大文件保护：只对「文本」生效 —— 音频/图片要能完整播放、完整预览
        truncated = False
        if not partial and is_text:
            limit = 4 * 1024 * 1024
            if len(data) > limit:
                data = data[:limit]
                truncated = True
                length = len(data)

        if is_text:
            ctype += "; charset=utf-8"

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{start + len(data) - 1}/{total}")
        if truncated:
            self.send_header("X-Prism-Truncated", "1")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        self._route()

    do_HEAD = do_GET

    def do_POST(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_DELETE(self):
        self._route()


def _cleanup_tmp(max_age: int | None = None, max_bytes: int | None = None) -> dict:
    """清理 data/tmp 里的音频临时段，两条线：

    1. **超时线**：修改时间超过 max_age 的，清掉（默认 2 小时）
    2. **超量线**：目录总量超过 max_bytes 时，按修改时间从旧到新清到线下（默认 500 MB）

    正常导入时段文件用完即删；只有进程被杀才会残留，所以这两条线是兜底。
    """
    max_age = max_age if max_age is not None else getattr(config, "TMP_MAX_AGE_SEC", 7200)
    max_bytes = max_bytes if max_bytes is not None else getattr(config, "TMP_MAX_BYTES", 500 * 1024 * 1024)

    d = config.DATA_DIR / "tmp"
    removed_age = removed_size = 0
    freed = 0
    if not d.is_dir():
        return {"removed": 0, "freed": 0}

    now = time.time()
    entries = []
    for f in d.glob("a_*"):
        try:
            st = f.stat()
        except OSError:
            continue
        if now - st.st_mtime > max_age:          # 超时线
            try:
                f.unlink()
                removed_age += 1
                freed += st.st_size
            except OSError:
                pass
        else:
            entries.append((st.st_mtime, st.st_size, f))

    total = sum(s for _, s, _ in entries)
    if total > max_bytes:                        # 超量线：从旧到新删
        for mt, sz, f in sorted(entries):
            if total <= max_bytes:
                break
            try:
                f.unlink()
                total -= sz
                freed += sz
                removed_size += 1
            except OSError:
                pass

    try:
        if not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass
    return {"removed": removed_age + removed_size, "by_age": removed_age,
            "by_size": removed_size, "freed": freed}


def _tidy(verbose: bool = False) -> dict:
    """启动时/导入前调一次：清 tmp + 裁任务记录。"""
    info = _cleanup_tmp()
    try:
        info["jobs_removed"] = db.prune_jobs_by_age(getattr(config, "JOBS_MAX_AGE_DAYS", 30))
        db.prune_jobs(getattr(config, "JOBS_KEEP", 200))
    except Exception:
        pass
    if verbose and info["removed"]:
        print(f"   Temp     : 已清理 {info['removed']} 个残留段"
              f"（超时 {info.get('by_age',0)} / 超量 {info.get('by_size',0)}），"
              f"释放 {info['freed']/1048576:.1f} MB", flush=True)
    return info


def main():
    db.init()
    info = _tidy()
    ok, msg = embed_health()
    banner = [
        "",
        "  ============================================================",
        "   Prism  ·  棱镜   —  local multimodal knowledge base",
        "  ============================================================",
        f"   Web UI   : http://{HOST}:{PORT}",
        f"   Embed    : {'OK  ' if ok else 'DOWN'}  {msg}",
        f"   Database : {db.DB_PATH}",
        f"   Data dir : {db.DB_PATH.parent}",
        f"   Temp     : {'已清理 %d 个残留段（%d 小时超时线 / %.0f MB 容量线）' % (info['removed'], getattr(config,'TMP_MAX_AGE_SEC',7200)//3600, getattr(config,'TMP_MAX_BYTES',5e8)/1048576) if info['removed'] else '干净（%d 小时超时线 / %.0f MB 容量线）' % (getattr(config,'TMP_MAX_AGE_SEC',7200)//3600, getattr(config,'TMP_MAX_BYTES',5e8)/1048576)}",
        "   Stop     : close this window, or press Ctrl+C",
        "  ============================================================",
        "",
    ]
    print("\n".join(banner), flush=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  [Prism stopped]")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
