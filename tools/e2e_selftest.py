"""Prism 端到端自测 —— 全部在临时库上跑，不碰任何真实数据

用法（服务要在跑）：
    .venv/Scripts/python.exe tools/e2e_selftest.py

覆盖：标签分页/置顶、名称与时间排序、包含+排除筛选、批量打标/移除、
      导入时整批打标、跨库移动/复制（连标签）、删除与页级回收、删库=删文件。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

BASE = "http://127.0.0.1:8848"
SCRATCH = Path(os.environ.get("TMPDIR") or ".") / "prism-e2e"
FAIL: list[str] = []


def api(path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} → {e.code} {e.read().decode()[:300]}")


def check(name: str, cond: bool, detail: str = ""):
    print(("  [OK] " if cond else "  [× ] ") + name + (f" —— {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def mkfile(name: str, text: str) -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    p = SCRATCH / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def wait_job(jid: str, timeout: float = 180.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = api(f"/api/jobs/{jid}")
        if j.get("state") not in ("running", "cancelling"):
            return j
        time.sleep(0.4)
    return {"state": "timeout"}


def main() -> int:
    # ---------- 0. 现场基线（只读）
    h0 = api("/api/health")
    base_items = h0["stats"]["items"]
    base_tags = h0["stats"]["tags"]
    print(f"基线：{base_items} 条索引 · {base_tags} 个标签 · "
          f"磁盘 {h0['stats']['disk_bytes'] / 1048576:.2f} MB")

    # ---------- 1. 真实库只读检查（有库才做，不依赖任何具体库名）
    print("\n[1] 只读检查：标签分页 / 排序")
    libs = api("/api/libraries")
    probe_lib = next((l for l in libs if l.get("file_count", 0) > 1), None)
    if probe_lib:
        lid = probe_lib["id"]
        t = api(f"/api/libraries/{lid}/tags?limit=24")
        check("标签分页：置顶区存在", isinstance(t.get("pinned"), list))
        check("标签分页：只返回一页但总数正确",
              len(t["tags"]) <= 24 and t["total"] >= len(t["tags"]),
              f"返回 {len(t['tags'])} / 共 {t['total']}")
        asc = api(f"/api/libraries/{lid}/files?sort=name&order=asc&limit=10")
        desc = api(f"/api/libraries/{lid}/files?sort=name&order=desc&limit=10")
        check("名称升序/降序结果不同",
              [f["path"] for f in asc["files"]] != [f["path"] for f in desc["files"]])
        added = api(f"/api/libraries/{lid}/files?sort=added&order=desc&limit=3")
        check("默认排序仍是最新在前",
              len(added["files"]) < 2 or added["files"][0]["id"] >= added["files"][-1]["id"])
    else:
        print("  （库里没有足够数据，跳过）")

    # ---------- 2. 建两个临时库
    print("\n[2] 建临时库 A / B")
    stamp = int(time.time())
    lidA = api("/api/libraries", "POST", {"name": f"selftest-A-{stamp}", "primary_type": "text"})["id"]
    lidB = api("/api/libraries", "POST", {"name": f"selftest-B-{stamp}", "primary_type": "text"})["id"]
    check("两个临时库建好", lidA > 0 and lidB > 0, f"id={lidA}/{lidB}")
    libfileA = ROOT / "data" / "libs" / f"lib_{lidA}.db"
    check("库文件已落地", libfileA.exists(), str(libfileA))

    # ---------- 3. 导入 + 导入时打标签
    print("\n[3] 导入 3 个文件，带标签「测试甲,测试乙」")
    files = [mkfile(f"prism_e2e_{i}.txt", f"这是第 {i} 个自测文件，内容 zebra {i}") for i in (1, 2, 3)]
    job = api("/api/import", "POST", {"library_id": lidA, "paths": files,
                                      "tags": ["测试甲", "测试乙"]})
    j = wait_job(job["job_id"])
    check("导入完成", j.get("state") == "done",
          f"state={j.get('state')} done={j.get('done')} note={j.get('note')}")
    lst = api(f"/api/libraries/{lidA}/files?limit=50")
    check("3 个文件都进了库", lst["total"] == 3, f"total={lst['total']}")
    check("行内带出了标签",
          all(set(f.get("tags") or []) == {"测试甲", "测试乙"} for f in lst["files"]))

    # ---------- 4. 批量打标 / 移除
    print("\n[4] 批量打标签 / 移除标签")
    paths = [f["path"] for f in lst["files"]]
    r = api("/api/tags/batch", "POST", {"library_id": lidA, "paths": paths[:2], "add": ["批量标"]})
    check("批量打标命中 2 个文件", r["files"] == 2 and r["added"] == 2, str(r))
    cnt = {x["name"]: x["count"] for x in api(f"/api/libraries/{lidA}/tags")["tags"]}
    check("标签计数 = 2", cnt.get("批量标") == 2, str(cnt))
    api("/api/tags/batch", "POST", {"library_id": lidA, "paths": [paths[0]], "remove": ["批量标"]})
    cnt = {x["name"]: x["count"] for x in api(f"/api/libraries/{lidA}/tags")["tags"]}
    check("移除后计数 = 1", cnt.get("批量标") == 1, str(cnt))

    # ---------- 5. 正反筛选
    print("\n[5] 包含 + 排除")
    both = api(f"/api/libraries/{lidA}/files?tags=" + urllib.parse.quote("测试甲,测试乙"))
    excluded = api(f"/api/libraries/{lidA}/files?tags=" + urllib.parse.quote("测试甲") +
                   "&not_tags=" + urllib.parse.quote("批量标"))
    check("同时包含两个标签 = 3", both["total"] == 3, f"{both['total']}")
    check("排除掉已打标那个 = 2", excluded["total"] == 2, f"{excluded['total']}")
    s = api("/api/search", "POST", {"library_id": lidA, "query": "zebra", "mode": "keyword",
                                    "top_k": 10, "tags": ["测试甲"], "not_tags": ["批量标"]})
    check("检索侧排除生效",
          all("批量标" not in (h.get("tags") or []) for h in s["results"]),
          f"{len(s['results'])} 条")

    # ---------- 6. 置顶
    print("\n[6] 标签置顶")
    api("/api/tags/pin", "POST", {"library_id": lidA, "name": "批量标", "pinned": True})
    t = api(f"/api/libraries/{lidA}/tags?limit=2")
    check("置顶标签出现在 pinned 区", any(x["name"] == "批量标" for x in t["pinned"]), str(t["pinned"]))
    pin_names = [x["name"] for x in t["pinned"]]
    summary = pin_names + [x["name"] for x in t["tags"] if x["name"] not in pin_names]
    check("摘要行里置顶排最前（界面合并规则）", summary[0] == "批量标", str(summary[:3]))

    # ---------- 7. 移动 / 复制（连标签）
    print("\n[7] 移动到 B / 复制到 B")
    r = api("/api/items/move", "POST", {"library_id": lidA, "paths": [paths[0]],
                                        "target_library_id": lidB})
    check("移动 1 个文件", r["moved"] == 1, str(r))
    b_files = api(f"/api/libraries/{lidB}/files")
    check("B 库收到文件", b_files["total"] == 1, f"{b_files['total']}")
    check("标签跟着搬过去了", bool(b_files["files"] and b_files["files"][0].get("tags")))
    r = api("/api/items/copy", "POST", {"library_id": lidA, "paths": [paths[1]],
                                        "target_library_id": lidB})
    check("复制 1 个文件", r["copied"] == 1, str(r))
    check("A 库还剩 2 个", api(f"/api/libraries/{lidA}/files")["total"] == 2)
    check("B 库有 2 个", api(f"/api/libraries/{lidB}/files")["total"] == 2)

    # ---------- 8. 删除
    print("\n[8] 删除索引")
    left = [f["path"] for f in api(f"/api/libraries/{lidA}/files")["files"]]
    before_rows = api(f"/api/libraries/{lidA}/files")["total"]
    api("/api/files", "DELETE", {"library_id": lidA, "path": left[0]})
    check("文件真的少了", api(f"/api/libraries/{lidA}/files")["total"] == before_rows - 1)

    # ---------- 9. 删库 = 删文件
    print("\n[9] 删临时库 = 删文件")
    api(f"/api/libraries/{lidA}", "DELETE")
    api(f"/api/libraries/{lidB}", "DELETE")
    time.sleep(0.5)
    check("A 库文件已不存在", not libfileA.exists())
    check("B 库文件已不存在", not (ROOT / "data" / "libs" / f"lib_{lidB}.db").exists())
    check("库里不再有临时库",
          all(l["id"] not in (lidA, lidB) for l in api("/api/libraries")))

    # ---------- 10. 真实数据没被动过（别人可能正在导入，所以只要求「没减少」）
    print("\n[10] 真实库数据没被动过")
    h1 = api("/api/health")
    check("索引条数没有减少", h1["stats"]["items"] >= base_items,
          f"{base_items} → {h1['stats']['items']}（并发导入只会变多）")

    # ---------- 11. 页级回收实证（直接开临时库文件，不走 HTTP）
    print("\n[11] 页级回收实证：删掉半库，文件应当自己变小")
    probe = ROOT / "data" / "_probe_reclaim.db"
    probe.unlink(missing_ok=True)
    import db as pdb
    pdb._fresh_db(probe, pdb.LIB_SCHEMA)          # noqa: SLF001
    c = sqlite3.connect(str(probe))
    c.execute("BEGIN")
    for i in range(4000):
        c.execute("INSERT INTO items(library_id,path,chunk_index,modality,chunk_text,added_at) "
                  "VALUES (1,?,0,'text',?,'x')", (f"p{i}", "y" * 300))
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # 先合并草稿，否则量到的是未落盘的主文件
    c.commit()
    big = probe.stat().st_size
    c.execute("DELETE FROM items WHERE path < 'p2000'")
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.commit()
    c.close()
    small = probe.stat().st_size
    check("删除后文件自动缩小（即时回收生效）", small < big, f"{big/1024:.0f}KB → {small/1024:.0f}KB")
    check("变小幅度合理（删了一半以上）", small < big * 0.75, f"{(1 - small/big)*100:.0f}% 被收回")
    probe.unlink(missing_ok=True)

    print("\n=========== 结果 ===========")
    if FAIL:
        print(f"失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
