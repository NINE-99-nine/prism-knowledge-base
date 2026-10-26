"""Prism · 棱镜 — 后台保洁线程

两件事，都是「系统自己的活」，不需要任何人点按钮：

1. **合并草稿并截断**（WAL checkpoint TRUNCATE）
   草稿本（prism.db-wal）默认只增不减，删完东西它还可能常驻几 MB。
   定期合并回主文件并截断，磁盘占用才是真实体积。

2. **按阈值压实**（VACUUM INTO + 原子替换）
   「即时回收」已经把整页空间还给文件系统了；剩下的只有**页内碎片**
   （一页里只剩两三条，整页空不出来）。这种碎片只能整库重写一次才能收回，
   而整库重写的代价与库的大小成正比 —— 所以：
     · 只在「删除量 / 现存行数 ≥ COMPACT_RATIO(默认 30%)」时才做
     · 单例、后台、导入期间自动让路
     · 写新文件 → rename 替换，不复制数据
   这样磁盘写入量由「删除量」和「碎片比例」决定，与删除次数无关。

线程是 daemon，随进程退出；异常一律吞掉（保洁失败不该影响检索/导入）。
"""
from __future__ import annotations

import threading
import time

import db

INTERVAL = 300          # 每 5 分钟看一次
_IDLE_AFTER = 20        # 距上次写入超过 20 秒才动手（避开正在导入的窗口）

_started = False
_lock = threading.Lock()
_stop = threading.Event()
last_run: float = 0.0
last_actions: list[str] = []


def _import_running() -> bool:
    """有导入任务在跑就让路 —— 别跟嵌入/写入抢 IO。"""
    try:
        import importer
        with importer._lock:                      # noqa: SLF001 - 同进程内部状态
            for j in importer._jobs.values():
                if j.get("state") in ("running", "cancelling"):
                    return True
    except Exception:
        pass
    return False


def pass_once(force: bool = False) -> list[str]:
    """跑一轮保洁。返回这一轮做了什么（给 /api/storage 显示）。"""
    global last_run, last_actions
    acts: list[str] = []
    if not force and _import_running():
        return ["导入进行中，跳过"]
    for lid in db.all_library_ids():
        try:
            u = db.lib_usage(lid)
            if u["wal_bytes"] > 512 * 1024:       # 草稿本超过 512 KB 才值得合并
                db.reclaim(lid)
                acts.append(f"库{lid}：草稿 {u['wal_bytes']/1048576:.1f}MB → 已合并")
            if db.needs_compact(lid):
                r = db.compact(lid)
                if r.get("ok"):
                    acts.append(f"库{lid}：压实 {r['before']/1048576:.1f}MB → "
                                f"{r['after']/1048576:.1f}MB（省 {r['saved']/1048576:.1f}MB）")
                else:
                    acts.append(f"库{lid}：压实失败 {r.get('reason')}")
        except Exception as e:                    # noqa: BLE001
            acts.append(f"库{lid}：{type(e).__name__}: {e}")
    last_run = time.time()
    last_actions = acts[-20:]
    return acts


def _loop() -> None:
    while not _stop.wait(INTERVAL):
        try:
            pass_once()
        except Exception:
            pass


def start() -> bool:
    global _started
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_loop, name="prism-housekeeping", daemon=True).start()
    return True
