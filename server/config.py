"""Prism · 棱镜 — 全局配置与路径

便携式自包含：所有东西都在 prism 目录内。
    prism/server/  程序代码
    prism/web/     前端
    prism/data/    数据库（唯一的数据落点）
"""
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent   # prism/
DATA_DIR = ROOT / "data"
WEB_DIR  = ROOT / "web"
DB_PATH  = DATA_DIR / "prism.db"

# ---- 服务 ----
HOST = "127.0.0.1"
PORT = 8848

# ---- 嵌入服务（外部依赖：本地 llama.cpp 多模态服务）----
EMBED_API   = "http://127.0.0.1:8084/v1/embeddings"
EMBED_MODEL = "embeddinggemma-2"
EMBED_DIM   = 768
EMBED_TIMEOUT = 180

# ---- 文本分块 ----
CHUNK_SIZE    = 2048  # 每段字符数
CHUNK_OVERLAP = 200   # 段间重叠（约 10%，与原来 500/50 的比例一致）

# ---- 支持的文件类型 ----
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".avif"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aac", ".wma"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg"}
TEXT_EXT  = {".txt", ".md", ".markdown", ".rst", ".json", ".yaml", ".yml",
             ".toml", ".ini", ".cfg", ".csv", ".log",
             ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".sh", ".bat",
             ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sql"}

# ---- 音频处理 ----
# 单段时长与体积上限（base64 会膨胀 ~33%，过大吃内存）
#
# token 预算：模型上下文 2048 token，音频实测约 25 token/秒
#   → 单段理论天花板约 80 秒；取 60 秒 ≈ 1500 token（占 73%），留安全余量。
#   （旧注释写的「68 token/秒、给 4096 留一半余量」是错的：速率高估 2.7 倍，
#     且上下文已从 4096 降到 2048，那句话早已失效。）
# ⚠️ 2026-10-12 实测补充（**只记事实，不改这个值**）：拿一条 5 小时长音频逐档试过
#   60 秒 → 被拒 `input (4053 tokens) is too large`；30 秒 → `Failed to tokenize prompt`；
#   20 秒 → 通过。也就是说 60 秒对高密度源**必定超限**，靠下面的「对半重切」兜底
#   （预期内行为，不是故障）。要调这个值请先想清楚它同时影响导入和检索两条路。
AUDIO_CHUNK_SECONDS = 60
AUDIO_MAX_BYTES     = 25 * 1024 * 1024

# ---- 临时文件清理（两条线，见 app.cleanup_tmp）----
TMP_MAX_AGE_SEC = 2 * 3600                 # tmp 里超过 2 小时的段文件：清
TMP_MAX_BYTES   = 500 * 1024 * 1024        # tmp 总量超过 500 MB：按时间从旧到新清

# ---- 任务记录保留 ----
JOBS_KEEP         = 200                    # 最多保留多少条导入任务记录
JOBS_MAX_AGE_DAYS = 30                     # 超过多少天的任务记录：清


# ---- 近似重复：规模护栏 ----
# O(n²) 两两比对，段数一多会吃掉巨量内存（实测 16,684 段 ≈ 1.1 GB 矩阵、50 秒以上）。
# 超过这个段数就不再自动跑，先问一声（前端点「仍然检测」才带 force=true 重来）。
DUPES_MAX_ITEMS = 8000

# ---- 视频抽帧（EmbeddingGemma-2 的上下文上限是 2048 token，实测每帧 80 token）----
VIDEO_FRAMES      = 16     # 每个视频抽几帧：16×80=1280 token，留足余量
VIDEO_FRAME_WIDTH = 512    # 抽帧宽度；512 与 256/384 同为 80 token/帧，取最大的那个
VIDEO_FFMPEG      = ""     # 留空 = 自动探测（与音频用同一个探测函数）


def classify(path) -> str:
    """按扩展名判断模态：image / video / audio / text / other"""
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in TEXT_EXT:
        return "text"
    return "other"
