"""Prism · 棱镜 — 原生路径选择器

浏览器出于安全限制，拿不到拖入文件的绝对路径（那是 Electron 才有的能力）。
所以这里用 tkinter 弹一个 Windows 原生对话框，把用户选中的路径打印成 JSON。

独立进程调用，避免阻塞服务主线程。
"""
import json
import sys
from pathlib import Path

# tkinter 是 Python 标准库（Windows 官方安装版自带）
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as e:                                    # pragma: no cover
    print(json.dumps({"error": f"tkinter 不可用: {e}"}, ensure_ascii=False))
    sys.exit(1)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "dir"   # dir | files
    default = sys.argv[2] if len(sys.argv) > 2 else str(Path.home())

    # ---- 文件类型过滤（第 3 个参数）----
    # 传 "md,txt" 这种扩展名列表 → 只显示这些；
    # 传 "text" / "image" / "audio" → 用预设分组；留空 → 全部文件
    kind = (sys.argv[3] if len(sys.argv) > 3 else "").strip().lower()

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()

    # 默认目录不存在就退回用户主目录（否则对话框可能显示成空）
    d = Path(default)
    if not d.is_dir():
        d = Path.home()

    if mode == "files":
        ftypes = _filetypes(kind)
        paths = filedialog.askopenfilenames(
            title="选择要导入的文件（可多选）",
            initialdir=str(d),
            filetypes=ftypes)
        result = list(paths)
    else:
        p = filedialog.askdirectory(
            title="选择要导入的文件夹",
            initialdir=str(d), mustexist=True)
        result = [p] if p else []

    root.destroy()

    # 🔴🔴 输出必须 UTF-8 且 ASCII 安全 —— 这是「选了文件却像没反应」的真正原因。
    # Windows 上 sys.stdout 默认是 GBK；路径里只要有 GBK 编不出的字符
    # （♡ ☆ 𝐈𝐑𝐈𝐒 这类符号 / 数学字母），print 就抛 UnicodeEncodeError、
    # 进程当场崩，**用户选中的路径全部丢失**，前端只看到空结果。
    # 两道保险：stdout 改 UTF-8；JSON 用 ensure_ascii=True 把非 ASCII 全转成 \uXXXX。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print(json.dumps({"paths": result}, ensure_ascii=True))


# 预设分组（跟 config.py 里支持的扩展名保持一致）
GROUPS = {
    "text":  ".txt .md .markdown .rst .json .yaml .yml .toml .ini .cfg .csv .log "
             ".py .js .ts .jsx .tsx .html .css .sh .bat .java .c .cpp .h .go .rs .sql",
    "image": ".png .jpg .jpeg .webp .bmp .gif .tiff .avif",
    "video": ".mp4 .webm .mov .mkv .avi .m4v .flv .wmv .mpg .mpeg",
    "audio": ".mp3 .wav .flac .m4a .ogg .opus .aac .wma",
}


def _filetypes(kind: str):
    """把参数变成 tkinter 的 filetypes。

    始终把「全部文件」放在第一位 —— 否则某些 Windows 版本会显示成空列表。
    """
    types = [("全部文件", "*.*")]
    if not kind:
        return types
    if kind in GROUPS:
        pats = " ".join("*" + e for e in GROUPS[kind].split())
        label = {"text": "文本 / 代码", "image": "图片", "video": "视频", "audio": "音频"}[kind]
        types.append((label, pats))
        return types
    # 自定义扩展名，逗号分隔，如 "md,txt"
    exts = [e.strip().lstrip(".").lower() for e in kind.replace(";", ",").split(",") if e.strip()]
    if exts:
        types.append((f"指定类型 ({', '.join('.' + e for e in exts)})",
                      " ".join("*." + e for e in exts)))
    return types


if __name__ == "__main__":
    main()
