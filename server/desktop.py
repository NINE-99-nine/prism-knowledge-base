"""Prism · 棱镜 — 桌面窗口启动器

═══ 为什么需要它 ═══
网页拿不到拖入文件的**绝对路径**（W3C 安全限制：否则任何网页都能靠拖放扫你硬盘目录），
而 Prism 是「只索引、不复制」—— 入库必须要路径。

原生窗口（WebView2）能拿到 `CoreWebView2File.Path`，pywebview 把它挂成
`file['pywebviewFullPath']`。见 `.venv/Lib/site-packages/webview/util.py:299`。

═══ 分工（两种拖放，互不打架）═══
  · 拖到**检索栏**  → 页面自己的 HTML5 drop 接手（拖入检索：上传内容算向量搜相似）
  · 拖到**别处**    → 本文件接手（拖入即入库：拿绝对路径调 /api/import）

页面的 `window.__prismDropTarget` 记录落点，这里读它来决定归谁。
"""
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import config                       # noqa: E402
import app as prism_app             # noqa: E402

URL = f"http://{config.HOST}:{config.PORT}"
WINDOW_TITLE = "Prism · 棱镜"

# 窗口几何（位置/尺寸/是否最大化）的存档 —— 落在 Prism 自己的 data 里，不碰 C 盘
GEO_NAME = "window.json"
_ICON_NAMES = ("棱镜Prism_图标.ico", "prism.ico", "icon.ico", "favicon.ico")
_MAXIMIZED = [False]
_GEO_DIRTY = [False]        # UI 线程只打标记，写盘交给后台线程（见 _geo_flusher）
_GEO_WINDOW = [None]
_STATE: dict = {}           # 小状态（比如标题栏当前是深是浅，避免重复设置）
_WIN = [None]               # pywebview 的 window 引用 —— 放模块级，绝不挂到 Api 上


def _geo_path() -> Path:
    return Path(config.DATA_DIR) / GEO_NAME


def _find_icon() -> str | None:
    """在项目目录里找 .ico 当窗口图标。

    动态扫描 —— 以后把 ico 换成别的文件也会自动生效，不用改代码。
    找不到就返回 None（pywebview 会退回 Python 自带图标，也就是任务栏那个 Python logo）。
    """
    root = Path(__file__).resolve().parent.parent          # prism/
    for name in _ICON_NAMES:
        p = root / name
        if p.is_file():
            return str(p)
    hits = sorted(root.glob("*.ico"))
    return str(hits[0]) if hits else None


def _load_geo() -> dict:
    try:
        d = json.loads(_geo_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _save_geo(window) -> None:
    """记下窗口位置和大小。最大化时只记「最大化」标志，别拿屏幕尺寸覆盖还原尺寸。

    ⚠️ 拖动/缩放窗口时 resized / moved 会**每像素触发一次** —— 直接写磁盘等于在
    UI 线程上做同步文件 I/O，窗口会明显卡顿甚至被 Windows 判定「无响应」。
    所以这里只是**打标记**，真正的写盘交给后台的 debounce 线程去做。
    """
    _GEO_DIRTY[0] = True


def _geo_flusher() -> None:
    """后台线程：攒够 1 秒没新动静再落盘，把写盘从 UI 线程上摘出去。"""
    while True:
        time.sleep(1.0)
        if not _GEO_DIRTY[0]:
            continue
        _GEO_DIRTY[0] = False
        try:
            w = _GEO_WINDOW[0]
            if w is None:
                continue
            old = _load_geo()
            if _MAXIMIZED[0]:
                geo = {
                    "maximized": True,
                    "x": old.get("x"), "y": old.get("y"),
                    "width": old.get("width") or w.width,
                    "height": old.get("height") or w.height,
                }
            else:
                if w.width < 200 or w.height < 150:
                    continue                                 # 最小化/异常值，不覆盖
                geo = {"maximized": False, "x": w.x, "y": w.y,
                       "width": w.width, "height": w.height}
            _geo_path().write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")
        except Exception:                                    # noqa: BLE001
            pass


def _save_geo_now(window) -> None:
    """关窗前同步存一次（这时候卡一下无所谓，而且必须存下来）。"""
    _GEO_WINDOW[0] = window
    _GEO_DIRTY[0] = True
    _geo_flusher_once()


def _geo_flusher_once() -> None:
    try:
        w = _GEO_WINDOW[0]
        if w is None:
            return
        old = _load_geo()
        if _MAXIMIZED[0]:
            geo = {"maximized": True, "x": old.get("x"), "y": old.get("y"),
                   "width": old.get("width") or w.width, "height": old.get("height") or w.height}
        else:
            geo = {"maximized": False, "x": w.x, "y": w.y, "width": w.width, "height": w.height}
        _geo_path().write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")
        _GEO_DIRTY[0] = False
    except Exception:                                        # noqa: BLE001
        pass


def _set_caption(window, dark: bool) -> bool:
    """把 Windows 原生标题栏染成跟界面一致的深色。

    为什么要这么做：默认标题栏跟着系统强调色走（就是那条蓝），跟界面割裂。
    图3 那种「深色底 + 扁平白色细线按钮」其实就是 Win11 原生标题栏在深色下的样子 ——
    所以染色就能得到同样效果，而且**原生按钮、边缘拖动缩放、Win11 贴边布局全都不用牺牲**。
    （纯无边框 frameless 会把这些一起干掉，pywebview 没实现 WM_NCHITTEST。）
    """
    try:
        import ctypes

        # .NET 的 Handle 是 IntPtr，不能直接 int() —— 得走 ToInt64()
        try:
            hwnd = int(window.native.Handle.ToInt64())
        except AttributeError:
            hwnd = int(window.native.Handle)
        dwm = ctypes.windll.dwmapi
        # 签名要写全 —— 不写 argtypes 时 hwnd 会被当 32 位截断，调用会失败
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20     # 标准「深色标题栏」开关
        DWMWA_CAPTION_COLOR = 35
        DWMWA_TEXT_COLOR = 36
        # 🔴 底色对齐界面的 --paper-2（顶栏用的就是这个），标题栏和页面之间**看不出一条缝**
        #    夜间 #101827 / 日间 #fdfdfc —— 跟 CSS 里的变量一致，改主题时一起变
        caption = 0x00271810 if dark else 0x00FCFDFD        # COLORREF 是 0x00BBGGRR
        text = 0x00EFE6DD if dark else 0x001A1C1C           # 夜间 #dde6ef / 日间 #1c1c1a

        for attr, color in ((DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0),
                            (DWMWA_CAPTION_COLOR, caption),
                            (DWMWA_TEXT_COLOR, text)):
            val = ctypes.c_int(color)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
        print(f"[desktop] 标题栏染色 {'深色' if dark else '浅色'} 已应用（hwnd={hwnd}）", flush=True)
        return True
    except Exception as e:                                   # noqa: BLE001
        print(f"[desktop] 标题栏配色没设上（不影响使用）：{type(e).__name__}: {e}", flush=True)
        return False


class Api:
    """暴露给页面的小接口。页面的 applyTheme() 会调 set_caption 同步标题栏颜色。

    🔴🔴 这里**绝对不能**把 pywebview 的 window 存成实例属性！
    pywebview 启动时会用 util.get_functions() **递归 dir() 遍历 js_api 对象的所有属性**
    来生成暴露给 JS 的函数表。一旦 self.window 指到 window 对象上，它就会顺着
    window → native(.NET 对象) → AccessibilityObject → Bounds → Empty → Empty → ...
    一路钻下去（`.Empty` 每次返回新对象，永远钻不完）→ **无限递归 → 页面卡死**，
    控制台刷 `window.native.AccessibilityObject.Bounds.Empty.Empty...: maximum recursion depth exceeded`。
    所以窗口引用放**模块级变量**（dir(api) 看不到），Api 上只留方法。
    """

    def __init__(self) -> None:
        pass

    def set_caption(self, dark: bool) -> bool:
        w = _WIN[0]
        if w is None:
            return False
        dark = bool(dark)
        if _STATE.get("caption_dark") == dark:      # 值没变就别重复设（页面会补调几次）
            return True
        _STATE["caption_dark"] = dark
        ok = _set_caption(w, dark)
        print(f"[desktop] 标题栏配色 → {'深色' if dark else '浅色'}", flush=True)
        return ok


def _wait_port(host: str, port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.6):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_server() -> bool:
    """HTTP 服务放后台线程（GUI 必须占主线程）。已在跑就直接用现成的。"""
    if _wait_port(config.HOST, config.PORT, timeout=0.4):
        print(f"  已经在运行，直接连 {URL}", flush=True)
        return True
    threading.Thread(target=prism_app.main, name="prism-http", daemon=True).start()
    return _wait_port(config.HOST, config.PORT)


def _is_elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                        # noqa: BLE001
        return False


def make_drop_handler(window):
    def on_drop(event):
        files = ((event or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        print(f"[drop] 拖入 {len(files)} 个文件，拿到 {len(paths)} 条绝对路径", flush=True)
        for p in paths:
            print(f"       {p}", flush=True)
        try:
            window.evaluate_js("window.__prismNative = true;")   # 补种保险标记
            print(f"[drop] 原生判定自检 → {window.evaluate_js(NATIVE_PROBE_JS)}", flush=True)
        except Exception:                                    # noqa: BLE001
            pass

        try:
            target = window.evaluate_js("window.__prismDropTarget || ''")
        except Exception:                                    # noqa: BLE001
            target = ""

        if target == "search":
            print("[drop] 落点在检索栏 → 交给页面的「拖入检索」", flush=True)
            return

        if not paths:
            brief = [{"name": f.get("name"), "type": f.get("type"), "size": f.get("size")}
                     for f in files[:5]]
            print(f"[drop] 一条路径都没拿到。dataTransfer.files = {brief}", flush=True)
            if _is_elevated():
                msg = "拖拽被拦了：本程序是以管理员身份运行的，Windows 不允许从资源管理器拖东西进来"
            elif not files:
                msg = "没拿到任何东西 —— 如果是文件夹，试试用「选择文件夹」按钮"
            else:
                msg = "拿到了文件但读不到路径 —— 请用「选择文件夹」按钮，或改用文件"
            try:
                window.evaluate_js(f"toast({json.dumps(msg, ensure_ascii=False)})")
            except Exception:                                # noqa: BLE001
                pass
            return

        js = ("window.__prismNativeDrop && window.__prismNativeDrop("
              + json.dumps(paths, ensure_ascii=False) + ")")
        try:
            print(f"[drop] 交给页面 → {window.evaluate_js(js)}", flush=True)
        except Exception as e:                               # noqa: BLE001
            print(f"[drop] 页面调用失败：{type(e).__name__}: {e}", flush=True)

    return on_drop


NATIVE_PROBE_JS = ("JSON.stringify({"
                   " pywebview: !!window.pywebview,"
                   " chromeWebview: !!(window.chrome && window.chrome.webview),"
                   " flag: !!window.__prismNative,"
                   " isNative: (typeof isNativeWindow === 'function' ? isNativeWindow() : 'no-fn')"
                   "})")

# 说明：以前这里有个后台线程轮询 evaluate_js 做自检 —— 已删。
# 从非 UI 线程调 pywebview/WebView2 会触发一堆 COM 跨线程报错
# （DefaultBackgroundColor / ZoomFactor …），噪声会把真错误淹掉。
# 自检改到 on_drop 里做 —— 那儿本来就在 UI 线程上，且页面肯定已就绪。


def _diag() -> dict:
    """启动自检：把"拖拽能不能用"的四个前提摊开打印出来。

    拖拽失效在这几种情况下都是**静默**的，光看界面看不出来，所以必须自己报：
      · 没装 pywebview          → 退回浏览器，根本没有拖放事件
      · 以管理员身份运行        → Windows 的 UIPI 会拦掉从资源管理器拖过来的东西
      · WebView2 运行时缺失/太旧 → 窗口开不出来或拿不到拖入路径
      · 窗口创建失败            → 进程直接退出
    """
    info = {"pywebview": "", "webview2": "", "elevated": False}
    try:
        import webview
        info["pywebview"] = getattr(webview, "__version__", "") or getattr(
            __import__("importlib.metadata", fromlist=["version"]), "version")("pywebview")
    except Exception as e:                                   # noqa: BLE001
        info["pywebview"] = f"(读不到：{e})"
    if os.name == "nt":
        try:
            import winreg
            key = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
                   r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                info["webview2"] = str(winreg.QueryValueEx(k, "pv")[0])
        except Exception:                                    # noqa: BLE001
            try:
                import winreg
                key = (r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
                       r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    info["webview2"] = str(winreg.QueryValueEx(k, "pv")[0])
            except Exception:                                # noqa: BLE001
                info["webview2"] = "(没找到)"
        try:
            import ctypes
            info["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:                                    # noqa: BLE001
            pass
    return info


def _print_diag(d: dict) -> None:
    print(f"  pywebview : {d['pywebview'] or '(未知)'}", flush=True)
    print(f"  WebView2  : {d['webview2'] or '(未知)'}", flush=True)
    if d["elevated"]:
        print("  ⚠ 本进程以【管理员】身份运行：Windows 会拦掉从资源管理器拖进来的文件/文件夹，"
              "拖拽入库在这种模式下一定失效。请用普通权限（直接双击 prism.bat）启动。", flush=True)


def _set_app_id() -> None:
    """给进程一个自己的 AppUserModelID。

    不设的话，Windows 任务栏会把窗口归到 **python.exe** 名下，于是显示 Python 的图标，
    哪怕我们给窗口换了图标。设了它，任务栏才认这是「Prism」这个独立应用，
    用窗口自己的图标（= 项目里那个 .ico）。
    """
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Prism.Lens")
        print("[desktop] 已设置 AppUserModelID（任务栏会用窗口图标，不再用 Python 的）", flush=True)
    except Exception as e:                                   # noqa: BLE001
        print(f"[desktop] AppUserModelID 设置失败（任务栏可能仍是 Python 图标）：{e}", flush=True)


def _fallback_browser(reason: str) -> None:
    """打不开原生窗口时的退路：开系统浏览器，并把"为什么"和"拖拽会怎样"说清楚。"""
    print(f"  ⚠ {reason} → 改用系统浏览器打开", flush=True)
    print("  ⚠ 浏览器模式下**拖拽入库不可用**（网页拿不到文件的绝对路径）。", flush=True)
    print("      · 入库请用界面上的「选择文件」/「选择文件夹」按钮", flush=True)
    print("      · 想恢复拖拽：把 pywebview 装上（重跑 安装.bat），再双击 prism.bat", flush=True)
    import webbrowser
    webbrowser.open(URL)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> int:
    _set_app_id()
    if not _start_server():
        print("  !! HTTP 服务没起来，窗口没法打开", flush=True)
        return 1
    print(f"  服务就绪 {URL}", flush=True)
    diag = _diag()
    _print_diag(diag)

    try:
        import webview
        from webview.dom import DOMEventHandler
    except ImportError:
        # 没装 pywebview → 退回老样子：开系统浏览器
        _fallback_browser("没有装 pywebview")
        return 0

    icon = _find_icon()
    geo = _load_geo()
    print(f"  图标     : {icon or '（项目目录里没找到 .ico —— 会用 Python 默认图标）'}", flush=True)
    print(f"  记忆窗口 : {_geo_path()}"
          f"{'（还没有存档，本次会用默认尺寸）' if not geo else ''}", flush=True)

    print("  桌面窗口已打开。拖文件进来即可入库（拖到检索栏则是检索）", flush=True)
    kw = dict(
        width=int(geo.get("width") or 1360),
        height=int(geo.get("height") or 900),
        min_size=(940, 620),
        text_select=True,
        maximized=bool(geo.get("maximized")),
    )
    if not geo.get("maximized") and geo.get("x") is not None and geo.get("y") is not None:
        kw["x"], kw["y"] = int(geo["x"]), int(geo["y"])   # 最大化时坐标是屏幕尺寸，不用它
    api = Api()
    try:
        window = webview.create_window(WINDOW_TITLE, URL, js_api=api, **kw)
    except Exception as e:                                   # noqa: BLE001
        _fallback_browser(f"桌面窗口创建失败（{type(e).__name__}: {e}）")
        return 0
    _WIN[0] = window          # 🔴 模块级变量，别挂到 api 上（见 Api 的注释）
    _GEO_WINDOW[0] = window

    _MAXIMIZED[0] = bool(geo.get("maximized"))
    # 后台写盘线程（UI 线程只打标记）
    threading.Thread(target=_geo_flusher, name="geo-flush", daemon=True).start()

    def bind(w):
        # 必须注册至少一个 DOM 监听器，否则 pywebview 根本不捕获拖入路径
        # （prevent_default / stop_propagation 都传 False —— 页面自己的拖放逻辑要照常跑）
        w.dom.document.events.drop += DOMEventHandler(make_drop_handler(w), False, False)
        # 标题栏先按夜间染上（页面 applyTheme 之后会用 js_api 再同步一次）
        w.events.shown += lambda: _set_caption(w, True)
        # 记窗口几何：**只打标记**，写盘交给后台线程（每像素写盘会把 UI 卡死）
        w.events.maximized += lambda: (_MAXIMIZED.__setitem__(0, True), _save_geo(w))
        w.events.restored += lambda: (_MAXIMIZED.__setitem__(0, False), _save_geo(w))
        w.events.resized += lambda: _save_geo(w)
        w.events.moved += lambda: _save_geo(w)
        w.events.closing += lambda: _save_geo_now(w)     # 关窗前必须同步存一次

    # private_mode=False 很关键：pywebview 默认是隐私模式，localStorage 每次全清，
    # 主题选择就记不住。storage_path 指到 Prism 自己的 data 里（不碰 C 盘）。
    try:
        webview.start(bind, window,
                      icon=icon,
                      private_mode=False,
                      storage_path=str(Path(config.DATA_DIR) / "webview"))
    except Exception as e:                                   # noqa: BLE001
        _fallback_browser(f"桌面窗口启动失败（{type(e).__name__}: {e}）")
        return 0
    _save_geo_now(window)
    print("  窗口已关闭，Prism 退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
