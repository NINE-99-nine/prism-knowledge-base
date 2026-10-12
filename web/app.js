/* ==========================================================================
   Prism · 棱镜 — 前端逻辑（原生 JS，零框架）
   ========================================================================== */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

/* 分段控件（.seg）：一组按钮里只有一个 .on，值取它的 data-v */
function segValue(id) {
  const on = $("#" + id)?.querySelector(".seg-i.on");
  return on ? (on.dataset.v || "") : "";
}
function segSet(id, v) {
  const el = $("#" + id);
  if (!el) return;
  el.querySelectorAll(".seg-i").forEach((b) =>
    b.classList.toggle("on", (b.dataset.v || "") === String(v)));
}
function segBind(id, onChange) {
  const el = $("#" + id);
  if (!el) return;
  el.querySelectorAll(".seg-i").forEach((b) => {
    b.onclick = () => {
      segSet(id, b.dataset.v || "");
      if (onChange) onChange(b.dataset.v || "");
    };
  });
}

/* ---- 主题：跟随系统 / 日间 / 夜间 ---- */
const THEME_KEY = "prism.theme";
const THEME_CN = { system: "跟随系统", light: "日间", night: "夜间" };
/* 图标按「模式」给，不是按「结果」—— 否则「跟随系统(现为夜间)」和手动「夜间」长得一模一样，分不出来 */
const THEME_ICON = { system: "◐", light: "☀", night: "☾" };
function applyTheme() {
  const pref = state.theme || "system";
  const sysDark = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  const eff = pref === "system" ? (sysDark ? "night" : "light") : pref;
  document.documentElement.dataset.theme = eff;
  const b = $("#btn-theme");
  if (b) {
    b.textContent = THEME_ICON[pref] || "◐";
    b.title = pref === "system"
      ? `主题：跟随系统（系统现在是${THEME_CN[sysDark ? "night" : "light"]}）—— 点击切换`
      : `主题：${THEME_CN[pref]} —— 点击切换（跟随系统 → 日间 → 夜间）`;
  }
  /* 桌面窗口里顺便把 Windows 原生标题栏染成同一个色调（见 server/desktop.py）
     —— 不然它跟着系统强调色走，会是一条突兀的蓝，跟界面割裂。
     pywebview 的 api 代理是**异步注入**的，boot 那一刻可能还不存在，所以补几次。 */
  const syncCaption = () => {
    try { window.pywebview?.api?.set_caption?.(eff === "night"); } catch (e) {}
  };
  syncCaption();
  [500, 1500, 3000].forEach((ms) => setTimeout(syncCaption, ms));
}

const state = {
  libs: [],
  currentLib: null,
  view: "search",
  jobTimer: null,
  topK: Number(localStorage.getItem("prism.topK") ?? 24),        // 只影响界面，接口默认值不变
  browseMode: localStorage.getItem("prism.browseMode") || "list", // list | grid
  browsePer: Number(localStorage.getItem("prism.browsePer") || 100),
  browsePage: 1,
  /* 结果历史（Ctrl+Z 返回 / Ctrl+Shift+Z 前进）
     存「数据快照」而不是 DOM —— 卡片上的点击事件是 JS 绑的，
     直接还原 innerHTML 会把事件全丢掉（点了没反应）。 */
  hist: [],
  histIdx: -1,
  lastResults: null,      // 最近一次检索结果的原始数组
  lastDupes: null,        // 最近一次近似重复的原始数组
  blobName: "",           // 拖拽检索用的文件名（恢复查询条用）
  theme: localStorage.getItem(THEME_KEY) || "system",   // 跟随系统 / light / night
  tags: [],               // 当前库用到的标签（带计数），来自 /api/libraries/<id>/tags
  tagSel: new Set(),      // 标签条上选中的标签 —— 多选＝同时满足
  autoQ: "",              // 标签条自动填进检索框的词（不是用户自己打的，可以安全清掉）
  includeExt: (() => {                                    // 导入格式白名单（空 = 全收）
    try { const v = JSON.parse(localStorage.getItem("prism.includeExt") || "[]"); return Array.isArray(v) ? v : []; }
    catch (e) { return []; }
  })(),
};

/* ---------------------------------------------------------------- 工具 */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const txt = await res.text();
  let data;
  try { data = txt ? JSON.parse(txt) : {}; } catch { data = { error: txt }; }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

function fmtSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`;
}

function baseName(p) { return (p || "").split(/[\\/]/).pop() || ""; }

/* 显示用文件名：SKILL.md / README.md / index.js 这类"容器文件名"换成父目录名，
   否则一屏全是同名条目，根本分不清（技能库里 146 个文件全叫 SKILL.md） */
function displayName(p) {
  const base = baseName(p);
  if (/^(skill|readme|index|__init__|main)\.(md|markdown|txt|py|js|ts|json)$/i.test(base)) {
    const parts = String(p || "").split(/[\\/]/).filter(Boolean);
    if (parts.length >= 2) return parts[parts.length - 2];
  }
  return base;
}

/* 路径压缩显示：只留末尾几段，太长的话前面用省略号 */
function shortPath(p) {
  const parts = String(p || "").split(/[\\/]/).filter(Boolean);
  if (parts.length <= 3) return String(p || "");
  return "…\\" + parts.slice(-3).join("\\");
}

function fileUrl(path) { return "/api/file?path=" + encodeURIComponent(path); }

const MODALITY_CN = { image: "图片", video: "视频", text: "文本", audio: "音频", other: "其他" };
const MODE_CN = { mixed: "混合", semantic: "语义", keyword: "关键词" };

/* 按扩展名给个文件类型图标 —— 用于浏览的「图标」模式（像 Windows 文件夹） */
const FILE_ICONS = {
  md: "📄", markdown: "📄", txt: "📄", rst: "📄", log: "📄",
  json: "🧾", yaml: "🧾", yml: "🧾", toml: "🧾", ini: "🧾", cfg: "🧾", csv: "🧾",
  py: "🐍", js: "📜", ts: "📜", jsx: "📜", tsx: "📜", html: "🌐", css: "🎨",
  sh: "⚙", bat: "⚙", sql: "🗃", java: "☕", c: "📘", cpp: "📘", h: "📘", go: "📘", rs: "📘",
  mp3: "🎵", wav: "🎵", flac: "🎵", m4a: "🎵", ogg: "🎵", opus: "🎵", aac: "🎵", wma: "🎵",
  zip: "📦", rar: "📦", "7z": "📦", pdf: "📕", docx: "📘", xlsx: "📗", pptx: "📙",
};
function fileIcon(path) {
  const ext = (String(path || "").split(".").pop() || "").toLowerCase();
  return FILE_ICONS[ext] || "📄";
}
function extLabel(path) {
  return (String(path || "").split(".").pop() || "").toUpperCase().slice(0, 5);
}

/* ---------------------------------------------------------------- 健康与库 */

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    const st = $("#status");
    const ok = h.embed && h.embed.ok;
    st.innerHTML = `<i class="dot ${ok ? "ok" : "bad"}"></i><span>嵌入服务 ${ok ? "OK" : "未连接"}</span>`;
    const s = h.stats || {};
    $("#stats").innerHTML =
      `库 ${s.libraries ?? 0}<br>条目 ${s.items ?? 0}<br>标签 ${s.tags ?? 0}` +
      (s.missing ? `<br>失效 ${s.missing}` : "");
  } catch (e) {
    $("#status").innerHTML = `<i class="dot bad"></i><span>服务异常</span>`;
  }
}

async function loadLibraries() {
  state.libs = await api("/api/libraries");
  const ul = $("#lib-list");
  ul.innerHTML = "";
  if (!state.libs.length) {
    ul.innerHTML = `<li class="empty">还没有库 —— 点右上角 ＋ 新建</li>`;
    return;
  }
  for (const lib of state.libs) {
    const li = document.createElement("li");
    li.className = "lib-item" + (state.currentLib === lib.id ? " on" : "");
    const meta = lib.file_count !== undefined
      ? `${lib.file_count} 文件 · ${lib.item_count} 段 · ${lib.primary_type}`
      : `${lib.item_count} 条 · ${lib.primary_type}`;
    li.innerHTML = `
      <div class="lib-top">
        <div class="lib-name">${escapeHtml(lib.name)}</div>
        <button class="lib-edit" title="编辑（改名 / 描述 / 删除）">⋯</button>
      </div>
      ${lib.description ? `<div class="lib-desc" title="${escapeHtml(lib.description)}">${escapeHtml(lib.description)}</div>` : ""}
      <div class="lib-meta">${meta}${lib.missing_count
        ? ` · <button class="lib-missing" title="有 ${lib.missing_count} 个文件找不到原文件 —— 点这里处理">⚠ 失效 ${lib.missing_count}</button>`
        : ""}</div>`;
    li.onclick = () => selectLibrary(lib.id);
    li.oncontextmenu = (e) => { e.preventDefault(); editLibrary(lib); };
    li.querySelector(".lib-edit").onclick = (e) => { e.stopPropagation(); editLibrary(lib); };
    const mb = li.querySelector(".lib-missing");
    if (mb) mb.onclick = (e) => { e.stopPropagation(); missingDialog(lib); };
    ul.appendChild(li);
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 任何未捕获的脚本错误都提示出来 —— 免得界面静默失效、只能靠猜 */
window.addEventListener("error", (e) => {
  try { toast("页面出错: " + (e.message || e.error), 6000); } catch (_) {}
});
window.addEventListener("unhandledrejection", (e) => {
  try { toast("请求出错: " + ((e.reason && e.reason.message) || e.reason), 6000); } catch (_) {}
});

async function selectLibrary(id) {
  state.currentLib = id;
  state.browsePage = 1;          // 换库回到第一页
  state.tagSel.clear();          // 换库清空标签选择（各库标签不一样，留着会悬空）
  state.autoQ = "";
  clearResults();                // 检索结果清掉（输入框的文字保留）
  await loadLibraries();
  await loadTagBar();            // 标签条跟着切库变
  if (state.view === "browse") await loadBrowse();
  if (state.view === "dupes") await runDupes();
}

/* ------------------------------------------------- 标签条 */

/* 拉当前库的标签（带计数）。换库时调。 */
async function loadTagBar() {
  const box = $("#sb-tags");
  if (!box) return;
  if (!state.currentLib) { state.tags = []; renderTagBar(); return; }
  try {
    const d = await api(`/api/libraries/${state.currentLib}/tags`);
    state.tags = d.tags || [];
  } catch (e) {
    state.tags = [];
  }
  renderTagBar();
}

function renderTagBar() {
  const box = $("#sb-tags");
  if (!box) return;
  const list = state.tags || [];
  // 选中的标签如果不在这个库里了，自动摘掉（免得筛选条件悬空、结果永远为空）
  for (const t of [...state.tagSel]) {
    if (!list.some((x) => x.name === t)) state.tagSel.delete(t);
  }
  if (!list.length) {                 // 这个库还没用过标签 → 整行不出现，不占地方
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML =
    `<span class="sb-tags-label">🏷 标签</span>` +
    list.map((t) => {
      const on = state.tagSel.has(t.name);
      return `<button class="tag-chip${on ? " on" : ""}" data-tag="${escapeHtml(t.name)}" ` +
             `title="${on ? "点一下取消" : "只看带这个标签的文件"}">` +
             `${escapeHtml(t.name)}<i>${t.count}</i></button>`;
    }).join("") +
    (state.tagSel.size ? `<button class="tag-chip tag-chip-clear" id="tag-clear">清除筛选 ✕</button>` : "");

  box.querySelectorAll(".tag-chip[data-tag]").forEach((b) => {
    b.onclick = () => {
      const t = b.dataset.tag;
      if (state.tagSel.has(t)) state.tagSel.delete(t);
      else state.tagSel.add(t);
      renderTagBar();
      applyTagFilter();
    };
  });
  const clr = $("#tag-clear");
  if (clr) clr.onclick = () => { state.tagSel.clear(); renderTagBar(); applyTagFilter(); };
}

/* 标签选择变了之后重跑检索。
   检索框空着时（或者里面是上次自动填的标签名）→ 拿刚点的标签名当检索词 ——
   标签现在参与关键词检索，所以「点标签 = 列出所有带这个标签的文件」。 */
function applyTagFilter() {
  const q = $("#q");
  if (!q) return;
  const userTyped = q.value.trim() && q.value !== state.autoQ;

  if (state.tagSel.size) {
    if (!userTyped) {
      state.autoQ = [...state.tagSel].slice(-1)[0];
      q.value = state.autoQ;
    }
    doSearch();
  } else if (state.autoQ && q.value === state.autoQ) {
    q.value = ""; state.autoQ = ""; clearResults();      // 筛选全清了，把自动填的词也收回
  } else if (userTyped) {
    doSearch();                                          // 用户自己的词还在，重搜一次（不带标签）
  }
}

/* 清空检索结果，不动输入框 */
function clearResults() {
  const box = $("#results");
  if (box) box.innerHTML = "";
}

/* ------------------------------------------------- 结果历史（Ctrl+Z / Ctrl+Shift+Z） */

/* 快照 = 「这一步之后看到的东西」。histPush 在**渲染完成之后**调用，
   压进去的是新状态；所以 hist[0] 是初始态，前进/后退就是挪 histIdx。 */
function snapshot(label) {
  return {
    label,
    view: state.view,
    lib: state.currentLib,
    q: $("#q") ? $("#q").value : "",
    browsePage: state.browsePage,
    results: state.lastResults,
    dupes: state.lastDupes,
    blobName: state.blobName,
    tagSel: [...state.tagSel],          // 标签筛选也算"这一步看到的东西"
  };
}

function histPush(label) {
  state.hist = state.hist.slice(0, state.histIdx + 1);   // 砍掉前进分支
  state.hist.push(snapshot(label));
  if (state.hist.length > 60) state.hist.shift();        // 只留最近 60 步
  state.histIdx = state.hist.length - 1;
  histSync();
}

function histSync() {
  const b = $("#btn-back");
  if (!b) return;
  const can = state.histIdx > 0;
  b.disabled = !can;
  const prev = can ? state.hist[state.histIdx - 1] : null;
  b.title = can ? `返回：${prev.label || "上一步"}（Ctrl+Z）` : "没有可返回的步骤";
}

function histApply(s) {
  pauseAllAudio();
  state.currentLib = s.lib;
  state.browsePage = s.browsePage;
  state.lastResults = s.results;
  state.lastDupes = s.dupes;
  state.blobName = s.blobName || "";
  state.tagSel = new Set(s.tagSel || []);   // 标签筛选一起还原
  renderTagBar();
  const q = $("#q");
  if (q) q.value = s.q ?? "";

  const v = s.view || "search";
  state.view = v;
  $$(".tab").forEach((t) => t.classList.toggle("on", t.dataset.view === v));
  $("#view-search").classList.toggle("hidden", v !== "search");
  $("#view-browse").classList.toggle("hidden", v !== "browse");
  $("#view-dupes").classList.toggle("hidden", v !== "dupes");

  const bq = $("#blob-query");
  if (v === "search") {
    if (s.blobName && s.results && s.results.length) {
      if (bq) {
        bq.style.display = "flex";
        bq.innerHTML = `<span class="bq-tag">FILE</span>
          <span class="bq-name" title="${escapeHtml(s.blobName)}">${escapeHtml(s.blobName)}</span>
          <span class="bq-meta">命中 ${s.results.length} 条</span>
          <button class="btn-mini js-clear-blob" title="清除，回到文字检索">✕</button>`;
        bq.querySelector(".js-clear-blob").onclick = () => {
          bq.style.display = "none"; bq.innerHTML = ""; state.blobName = "";
        };
      }
    } else if (bq) {
      bq.style.display = "none"; bq.innerHTML = "";
    }
    if (s.results && s.results.length) renderCards(s.results);
    else clearResults();
  } else if (v === "browse") {
    loadBrowse();
  } else if (v === "dupes") {
    if (s.dupes) renderDupes(s.dupes);
    else runDupes();
  }
  histSync();
  loadLibraries();
}

function histBack() {
  if (state.histIdx <= 0) return toast("已经是最初的状态了");
  state.histIdx--;
  histApply(state.hist[state.histIdx]);
}

function histForward() {
  if (state.histIdx >= state.hist.length - 1) return;
  state.histIdx++;
  histApply(state.hist[state.histIdx]);
}

/* 刷新：连输入框一起清 —— 输入框里的文字本身也可能社死（Ctrl+Z 可撤回） */
function refreshSearch() {
  pauseAllAudio();
  histPush("清空前");                 // 先记住「清空前的样子」，这样 Ctrl+Z 能连输入框一起找回来
  const q = $("#q");
  if (q) q.value = "";
  const bq = $("#blob-query");
  if (bq) { bq.style.display = "none"; bq.innerHTML = ""; }
  state.lastResults = null;
  state.blobName = "";
  state.lastQueryKind = "text";
  state.lastFileName = "";
  clearResults();
  histPush("清空检索");
}

/* 删库之后的善后：结果/浏览/历史全部归位，不留悬空的 currentLib */
async function resetAfterLibGone() {
  state.lastResults = null;
  state.lastDupes = null;
  state.blobName = "";
  state.browsePage = 1;
  const bq = $("#blob-query");
  if (bq) { bq.style.display = "none"; bq.innerHTML = ""; }
  clearResults();
  const dl = $("#dupes-list");
  if (dl) dl.innerHTML = "";
  if (state.libs.length) {
    await selectLibrary(state.libs[0].id);      // 自动落到第一个库，避免 currentLib = null 悬空
  } else {
    state.currentLib = null;
    const bl = $("#browse-list");
    if (bl) bl.innerHTML = `<div class="empty">一个库都没有了 —— 点左上角 ＋ 新建</div>`;
    $("#browse-pager")?.classList.add("hidden");
  }
  state.hist = [];
  state.histIdx = -1;
  histPush("删库");
}

function currentLibName() {
  const l = state.libs.find((x) => x.id === state.currentLib);
  return l ? l.name : "";
}

/* ---------------------------------------------------------------- 视图切换 */

function setView(v) {
  if (v !== state.view) pauseAllAudio();      // 切视图时停掉正在播的音频
  state.view = v;
  $$(".tab").forEach((t) => t.classList.toggle("on", t.dataset.view === v));
  $("#view-search").classList.toggle("hidden", v !== "search");
  $("#view-browse").classList.toggle("hidden", v !== "browse");
  $("#view-dupes").classList.toggle("hidden", v !== "dupes");
  if (v === "browse") loadBrowse();
  if (v === "dupes") runDupes();
}

/* ---------------------------------------------------------------- 检索 */

// ---- 拖入文件检索：把文件内容变成向量，去库里找相似的东西（任意模态互搜）----
async function doSearchByBlob(file) {
  if (!file) return;
  if (!state.currentLib) return toast("先在左侧选一个库");
  const MB = 1024 * 1024;
  if (file.size > 60 * MB) {
    return toast(`文件太大（${(file.size / MB).toFixed(1)} MB）。拖拽检索单次上限约 60 MB —— ` +
                 `更大的文件请走左侧「选择文件夹」导入后再检索。`);
  }

  const box = $("#results");
  state.lastQueryKind = "file";
  state.lastFileName = file.name;
  box.innerHTML = `<div class="empty">正在读取 <b>${escapeHtml(file.name)}</b> 并检索…</div>`;

  // 用 FileReader 取 base64（dataURL 去掉前缀）—— 比手拼 chunk 安全，大文件不会爆栈
  const dataUrl = await new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result);
    fr.onerror = () => rej(fr.error);
    fr.readAsDataURL(file);
  });
  const b64 = String(dataUrl).split(",")[1] || "";
  if (!b64) return toast("读取文件失败");

  const topK = Number($("#top-k").value);
  try {
    const d = await api("/api/search/blob", {
      method: "POST",
      body: JSON.stringify({
        library_id: state.currentLib,
        filename: file.name,
        data: b64,
        top_k: topK,
        modality: segValue("seg-modality") || null,
        tags: [...state.tagSel],
      }),
    });
    renderFileQueryBanner(file, d);
    state.blobName = file.name;
    if (!d.results.length) {
      state.lastResults = null;
      $("#results").innerHTML = `<div class="empty">没有找到相似的内容。</div>`;
    } else {
      renderCards(d.results);
    }
    histPush(`以文件检索「${file.name}」`);
  } catch (e) {
    box.innerHTML = `<div class="empty">检索失败：${escapeHtml(String(e.message || e))}</div>`;
    toast("以文件检索失败");
  }
}

function renderFileQueryBanner(file, d) {
  const el = $("#blob-query");
  if (!el) return;
  const kind = (file.type || "").split("/")[0] || "文件";
  el.style.display = "flex";
  el.innerHTML = `<span class="bq-tag">${kind.toUpperCase()}</span>
    <span class="bq-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
    <span class="bq-meta">${(file.size / 1024).toFixed(0)} KB · 命中 ${d.count} 条</span>
    <button class="btn-mini js-clear-blob" title="清除，回到文字检索">✕</button>`;
  el.querySelector(".js-clear-blob").addEventListener("click", () => {
    el.style.display = "none";
    el.innerHTML = "";
    state.lastQueryKind = "text";
  });
}

function bindBlobSearch() {
  const q = $("#q");
  const bar = document.querySelector(".searchbar") || q.parentElement;
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };

  ["dragenter", "dragover"].forEach((ev) =>
    bar.addEventListener(ev, (e) => { stop(e); bar.classList.add("drag-over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    bar.addEventListener(ev, (e) => { stop(e); bar.classList.remove("drag-over"); }));

  bar.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) doSearchByBlob(f);
  });

  const inp = $("#blob-file");
  const btn = $("#btn-blob");
  if (btn && inp) {
    btn.addEventListener("click", () => inp.click());
    inp.addEventListener("change", () => {
      if (inp.files && inp.files[0]) doSearchByBlob(inp.files[0]);
      inp.value = "";
    });
  }
}

async function doSearch() {
  const q = $("#q").value.trim();
  if (!q) return toast("请输入检索内容");
  if (!state.currentLib) return toast("先在左侧选一个库");
  const box = $("#results");
  const topK = Number($("#top-k").value);
  state.topK = topK;
  localStorage.setItem("prism.topK", String(topK));   // 只存前端，接口默认值不受影响
  const mode = segValue("seg-mode") || "mixed";
  state.searchMode = mode;
  localStorage.setItem("prism.searchMode", mode);     // 记住上次用的模式，下次打开一致
  box.innerHTML = `<div class="empty">检索中…</div>`;
  try {
    const d = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        library_id: state.currentLib,
        query: q,
        top_k: topK,
        mode: mode,
        modality: segValue("seg-modality") || null,
        group: $("#toggle-group").classList.contains("on"),
        tags: [...state.tagSel],              // 标签多选＝同时满足（空数组＝不过滤）
      }),
    });
    state.blobName = "";
    if (!d.results.length) {
      state.lastResults = null;
      box.innerHTML = `<div class="empty">没有命中。<br>
        <span style="font-size:0.72rem">当前是「${MODE_CN[mode] || mode}」模式${mode !== "semantic"
          ? "" : "，它按意思找 —— 搜确切名字可以切到「关键词」或「混合」"}。</span></div>`;
    } else {
      renderCards(d.results);
    }
    histPush(`检索「${q}」`);
  } catch (e) {
    box.innerHTML = `<div class="empty">检索失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderCards(items) {
  const box = $("#results");
  state.lastResults = items;      // 存原始数组，供历史恢复（不能恢复 innerHTML，会丢事件）
  box.innerHTML = "";
  for (const it of items) {
    const v = it.virt;                    // 虚拟文件（内部自建条目）—— 有它就不是真文件
    const card = document.createElement("div");
    card.className = "card" + (it.grouped ? " grouped" : "") + (v ? " card-virt" : "");
    const score = (it.score * 100).toFixed(1) + "%";
    let media = "";
    if (v) {
      // 虚拟文件：显示封面图（没封面就用描述）。封面是本地真实文件的路径，走 /api/file 取。
      const cover = v.cover ? fileUrl(v.cover) : "";
      media = `<div class="badge virt">VIRTUAL</div>` +
              (cover ? `<img class="thumb" loading="lazy" src="${cover}" alt="">` : "") +
              (v.description
                ? `<div class="excerpt">${escapeHtml(v.description.slice(0, 200))}</div>` : "");
    } else if (it.modality === "image") {
      media = `<img class="thumb" loading="lazy" src="${fileUrl(it.path)}" alt="">`;
    } else if (it.modality === "video") {
      media = `<div class="badge">VIDEO</div><video class="thumb" controls preload="metadata" src="${fileUrl(it.path)}"></video>`;
    } else if (it.modality === "audio") {
      media = `<div class="badge">AUDIO</div><audio controls preload="none" src="${fileUrl(it.path)}"></audio>`;
    } else {
      const n = it.grouped && it.hits > 1 ? 160 : 220;
      media = `<div class="excerpt">${escapeHtml((it.text || "").slice(0, n))}</div>`;
    }
    const tagHtml = (it.tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("");

    // 文件级结果：显示「命中 N 处」+ 可展开的其余命中段
    let hitsHtml = "";
    if (it.grouped && it.hits > 1) {
      const chunks = (it.chunks || []).slice(1);   // 第 1 条已在上面 excerpt 里露过脸
      hitsHtml = `
        <div class="hits">
          <span class="hits-badge">命中 ${it.hits} 处</span>
          ${chunks.length ? `<button class="btn-mini js-fold">展开其余 ${chunks.length} 段</button>` : ""}
        </div>
        <div class="chunk-list hidden">${chunks.map((c) => `
          <div class="chunk">
            <span class="chunk-idx">段 ${c.chunk_index + 1}</span>
            <span class="chunk-score">${(c.score * 100).toFixed(1)}%</span>
            <div class="chunk-text">${escapeHtml((c.text || "").slice(0, 150))}…</div>
          </div>`).join("")}${
          it.hits > (it.chunks || []).length
            ? `<div class="chunk-more">该文件共命中 ${it.hits} 处，这里显示分数最高的前 ${(it.chunks || []).length} 处</div>`
            : ""}</div>`;
    }

    const rel = v ? (v.related || []) : [];
    card.innerHTML = `
      <span class="score">${score}</span>
      ${media}
      <div class="name">${v
        ? `<span class="virt-tag">虚拟</span>${escapeHtml(v.title || "未命名虚拟文件")}`
        : escapeHtml(baseName(it.path)) + (!it.grouped && it.chunk_index ? ` · 段 ${it.chunk_index + 1}` : "")}</div>
      <div class="path" title="${escapeHtml(v ? (rel[0] || "") : it.path)}">${v
        ? (rel.length
            ? `关联 ${rel.length} 项 · ${escapeHtml(baseName(rel[0]))}`
            : "还没关联本地文件（点「编辑」加）")
        : escapeHtml(it.path)}</div>
      ${hitsHtml}
      ${tagHtml ? `<div class="tags">${tagHtml}</div>` : ""}
      <div class="card-actions">
        ${v ? `<button class="btn-mini js-vedit">编辑</button>` : ""}
        ${!v && it.modality === "text" ? `<button class="btn-mini js-full">查看全文</button>` : ""}
        <button class="btn-mini js-locate">定位</button>
      </div>`;

    const fold = card.querySelector(".js-fold");
    if (fold) {
      fold.onclick = (e) => {
        e.stopPropagation();
        const list = card.querySelector(".chunk-list");
        list.classList.toggle("hidden");
        fold.textContent = list.classList.contains("hidden")
          ? `展开其余 ${(it.chunks || []).length - 1} 段`
          : "收起片段";
      };
    }
    const fullBtn = card.querySelector(".js-full");
    if (fullBtn) {
      fullBtn.onclick = (e) => {
        e.stopPropagation();
        openFullText(it.path);
      };
    }
    const locBtn = card.querySelector(".js-locate");
    if (locBtn) {
      locBtn.onclick = (e) => {
        e.stopPropagation();
        // 虚拟文件本身不是真实文件 —— 定位的是它**关联的那个本地大文件**
        if (v) {
          if (!rel.length) return toast("这条虚拟文件还没关联本地文件，点「编辑」加上");
          api("/api/reveal", { method: "POST", body: JSON.stringify({ path: rel[0] }) })
            .catch((err) => toast("无法打开：" + err.message));
          return;
        }
        api("/api/reveal", { method: "POST", body: JSON.stringify({ path: it.path }) })
          .catch((err) => toast("无法打开：" + err.message));
      };
    }
    const vEdit = card.querySelector(".js-vedit");
    if (vEdit) {
      vEdit.onclick = (e) => { e.stopPropagation(); openVirtualEditor(v.vid); };
    }
    card.onclick = (e) => {
      if (e.target.tagName === "AUDIO") return;
      if (v) return openVirtualDrawer(v.vid);   // 和普通文件一样：左键开右侧预览
      openDrawer(it.id);
    };
    box.appendChild(card);
  }
}

/* ---------------------------------------------------------------- 全文查看 */

async function openFullText(path) {
  const modal = $("#text-modal");
  $("#text-modal-title").textContent = baseName(path);
  $("#text-modal-meta").textContent = "加载中…";
  $("#text-modal-body").textContent = "";
  modal.classList.remove("hidden");
  try {
    const res = await fetch(fileUrl(path));
    if (!res.ok) {
      const msg = res.status === 403 ? "这个文件不在任何库的索引里（安全限制）" : `HTTP ${res.status}`;
      throw new Error(msg);
    }
    const truncated = res.headers.get("X-Prism-Truncated") === "1";
    const text = await res.text();
    $("#text-modal-body").textContent = text || "（空文件）";
    $("#text-modal-meta").textContent =
      `${text.length.toLocaleString()} 字符${truncated ? " · 文件过大，只显示前 4 MB" : ""}`;
  } catch (e) {
    $("#text-modal-body").textContent = "读取失败：" + e.message;
    $("#text-modal-meta").textContent = "";
  }
}

/* ---------------------------------------------------------------- 浏览 */

async function loadBrowse() {
  const list = $("#browse-list");
  const pager = $("#browse-pager");
  if (!state.currentLib) {
    list.className = "browse-list";
    list.innerHTML = `<div class="empty">先在左侧选一个库</div>`;
    pager.classList.add("hidden");
    return;
  }
  const lib = state.libs.find((l) => l.id === state.currentLib);
  $("#browse-title").textContent = lib ? `库：${lib.name}` : "库";
  const sub = $("#browse-sub");
  if (sub) sub.textContent = lib?.description || "";
  list.className = "browse-list" + (state.browseMode === "grid" ? " grid-mode" : "");
  list.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const m = segValue("seg-browse-modality");
    const per = state.browsePer;
    const offset = (state.browsePage - 1) * per;
    const d = await api(`/api/libraries/${state.currentLib}/files?limit=${per}&offset=${offset}${m ? "&modality=" + m : ""}`);
    if (!d.files.length) {
      list.innerHTML = `<div class="empty">${state.browsePage > 1 ? "这一页没有内容" : "这个库还是空的 —— 拖入或选择文件来导入"}</div>`;
      renderPager(pager, d.total);
      return;
    }
    list.innerHTML = (state.browseMode === "grid" ? d.files.map(gridCell) : d.files.map(listRow)).join("");
    bindFileRows(list, d.files);
    renderPager(pager, d.total);
  } catch (e) {
    list.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

/* 列表模式：一行一个文件（含内容开头预览） */
function listRow(f, i) {
  return `
    <div class="file-block">
      <div class="row file-row ${f.status === "missing" ? "missing" : ""}" data-i="${i}">
        <div class="row-icon">${f.modality === "image" ? "🖼" : (f.modality === "video" ? "🎬" : fileIcon(f.path))}</div>
        <div class="row-main">
          <div class="rname">${f.virt_title
            ? `<span class="virt-tag">虚拟</span>${escapeHtml(f.virt_title)}`
            : escapeHtml(displayName(f.path))}</div>
          ${f.preview ? `<div class="rpreview">${escapeHtml(f.preview.replace(/\s+/g, " ").slice(0, 260))}</div>` : ""}
          <div class="rpath" title="${escapeHtml(f.path)}">${f.virt_title
            ? escapeHtml((f.virt_description || "").slice(0, 80))
            : escapeHtml(shortPath(f.path))}</div>
        </div>
        <div class="rmeta">${MODALITY_CN[f.modality] || f.modality} · ${f.chunks > 1 ? f.chunks + " 段 · " : ""}${fmtSize(f.size)}</div>
        <div class="row-acts">
          <button class="btn-mini js-expand" title="看这个文件被切成了哪些段">段 (${f.chunks})</button>
          ${f.modality === "text" ? `<button class="btn-mini js-fulltext" title="看完整原文">全文</button>` : ""}
          <button class="btn-mini js-reveal" title="在资源管理器中定位">定位</button>
          <button class="btn-mini js-remove" title="只移除索引，不删源文件">移除</button>
        </div>
      </div>
      <div class="chunk-list hidden" data-i="${i}"></div>
    </div>`;
}

/* 图标模式：网格。图片显缩略图；其它显示「左上角类型角标 + 内容开头预览」
   （类型角标不占整格，把位置让给内容 —— 像 Windows 的平铺视图） */
/* 伪路径 → vid（不是虚拟文件就返回 null） */
function virtualVid(path) {
  const p = String(path || "");
  return p.startsWith("prism://virtual/") ? Number(p.split("/").pop()) : null;
}

function gridCell(f, i) {
  const vid = virtualVid(f.path);
  let media;
  if (vid) {
    // 虚拟文件：用封面当缩略图（和图片文件一样）。没有封面就退回描述文本。
    media = f.virt_cover
      ? `<img class="cell-thumb" loading="lazy" src="${fileUrl(f.virt_cover)}" alt="">`
      : `<div class="cell-text">
           <span class="cell-badge">虚拟</span>
           <div class="cell-content">${escapeHtml((f.virt_description || "（没写描述）").slice(0, 220))}</div>
         </div>`;
  } else if (f.modality === "video") {
    media = `<div class="cell-video"><video controls preload="metadata" src="${fileUrl(f.path)}"></video></div>`;
  } else if (f.modality === "image") {
    media = `<img class="cell-thumb" loading="lazy" src="${fileUrl(f.path)}" alt="">`;
  } else if (f.modality === "audio") {
    media = `<div class="cell-audio">
               <span class="cell-badge">${fileIcon(f.path)} ${escapeHtml(extLabel(f.path))}</span>
               <div class="cell-playtext">${escapeHtml(f.preview || "音频")}</div>
             </div>`;
  } else {
    media = `<div class="cell-text">
               <span class="cell-badge">${fileIcon(f.path)} ${escapeHtml(extLabel(f.path))}</span>
               <div class="cell-content">${escapeHtml((f.preview || "（无文本内容）").replace(/\s+/g, " ").slice(0, 220))}</div>
             </div>`;
  }
  return `
    <div class="cell ${f.status === "missing" ? "missing" : ""}${vid ? " cell-virt" : ""}" data-i="${i}" title="${escapeHtml(f.path)}">
      <div class="cell-media">${media}</div>
      <div class="cell-name" title="${escapeHtml(f.path)}">${vid
        ? `<span class="virt-tag">虚拟</span>${escapeHtml(f.virt_title || "未命名")}`
        : escapeHtml(displayName(f.path))}</div>
      <div class="cell-meta">${vid
        ? `虚拟文件 · ${f.chunks} 段 · ${fmtSize(f.size)}`
        : `${MODALITY_CN[f.modality] || f.modality}${f.chunks > 1 ? " · " + f.chunks + " 段" : ""} · ${fmtSize(f.size)}`}</div>
      <div class="cell-acts">
        <button class="btn-mini js-expand" title="看段落">段</button>
        ${!vid && f.modality === "text" ? `<button class="btn-mini js-fulltext" title="看完整原文">全文</button>` : ""}
        <button class="btn-mini js-reveal" title="定位">定位</button>
        <button class="btn-mini js-remove" title="移除索引">移除</button>
      </div>
      <div class="chunk-list hidden" data-i="${i}"></div>
    </div>`;
}

/* 两种模式共用的事件绑定 */
function bindFileRows(list, files) {
  list.querySelectorAll(".file-row, .cell").forEach((row) => {
    const i = Number(row.dataset.i);
    const f = files[i];
    if (!f) return;
    const box = list.querySelector(`.chunk-list[data-i="${i}"]`);

    // 虚拟文件：定位按钮改成打开预览（定位一个伪路径没有意义）
    const vid = virtualVid(f.path);
    if (vid) {
      const rv = row.querySelector(".js-reveal");
      if (rv) rv.onclick = (e) => { e.stopPropagation(); openVirtualDrawer(vid); };
    }

    const ex = row.querySelector(".js-expand");
    if (ex) {
      ex.onclick = async (e) => {
        e.stopPropagation();
        if (!box.classList.contains("hidden")) {
          box.classList.add("hidden");
          ex.textContent = `段 (${f.chunks})`;
          return;
        }
        box.classList.remove("hidden");
        ex.textContent = "收起";
        if (!box.dataset.loaded) {
          box.innerHTML = `<div class="empty">加载中…</div>`;
          try {
            const r = await api(`/api/libraries/${state.currentLib}/files/chunks?path=${encodeURIComponent(f.path)}`);
            box.innerHTML = r.chunks.map((c) => `
              <div class="chunk">
                <span class="chunk-idx">段 ${c.chunk_index + 1}</span>
                <span class="chunk-score">${c.text_len} 字</span>
                <div class="chunk-text">${escapeHtml(c.preview || "（图片/音频，无文本）")}</div>
              </div>`).join("") || `<div class="empty">没有段</div>`;
            box.dataset.loaded = "1";
          } catch (err) {
            box.innerHTML = `<div class="empty">加载失败：${escapeHtml(err.message)}</div>`;
          }
        }
      };
    }

    const full = row.querySelector(".js-fulltext");
    if (full) full.onclick = (e) => { e.stopPropagation(); openFullText(f.path); };

    const rev = row.querySelector(".js-reveal");
    if (rev) {
      rev.onclick = (e) => {
        e.stopPropagation();
        api("/api/reveal", { method: "POST", body: JSON.stringify({ path: f.path }) })
          .catch((err) => toast("无法打开：" + err.message));
      };
    }

    const rm = row.querySelector(".js-remove");
    if (rm) {
      rm.onclick = (e) => {
        e.stopPropagation();
        confirmDialog(
          "移除这个文件的索引？",
          `${baseName(f.path)}\n\n会把该文件在本库的全部 ${f.chunks} 条索引记录删掉。\n源文件不会被删除。`,
          async () => {
            const r = await api("/api/files", {
              method: "DELETE",
              body: JSON.stringify({ library_id: state.currentLib, path: f.path }),
            });
            toast(`已移除 ${r.removed} 条索引`);
            await refreshHealth(); await loadLibraries(); await loadBrowse();
          },
          true
        );
      };
    }

    // 虚拟文件走预览抽屉（伪路径不是真文件，普通抽屉会显示「路径 prism://virtual/5」）
    row.onclick = () => (vid ? openVirtualDrawer(vid) : openDrawer(f.id));
  });
}

/* 分页条 */
function renderPager(pager, total) {
  const per = state.browsePer;
  const pages = Math.max(1, Math.ceil(total / per));
  if (state.browsePage > pages) state.browsePage = pages;
  pager.classList.remove("hidden");
  pager.innerHTML = `
    <div class="pager-info">共 ${total} 个文件 · 第 ${state.browsePage} / ${pages} 页</div>
    <div class="pager-ctl">
      <select id="pager-per" title="每页显示多少个文件">
        ${[50, 100, 200, 500].map((n) => `<option value="${n}"${n === per ? " selected" : ""}>每页 ${n}</option>`).join("")}
      </select>
      <button class="btn-mini" id="pager-first" ${state.browsePage <= 1 ? "disabled" : ""}>« 首页</button>
      <button class="btn-mini" id="pager-prev" ${state.browsePage <= 1 ? "disabled" : ""}>‹ 上一页</button>
      <span class="pager-jump">
        第 <input id="pager-go" type="number" min="1" max="${pages}" value="${state.browsePage}"> / ${pages} 页
        <button class="btn-mini" id="pager-jump">跳转</button>
      </span>
      <button class="btn-mini" id="pager-next" ${state.browsePage >= pages ? "disabled" : ""}>下一页 ›</button>
      <button class="btn-mini" id="pager-last" ${state.browsePage >= pages ? "disabled" : ""}>末页 »</button>
    </div>`;

  const go = (p) => {
    state.browsePage = Math.min(Math.max(1, p), pages);
    loadBrowse();
  };
  pager.querySelector("#pager-per").onchange = (e) => {
    state.browsePer = Number(e.target.value);
    localStorage.setItem("prism.browsePer", String(state.browsePer));
    state.browsePage = 1;
    loadBrowse();
  };
  pager.querySelector("#pager-first").onclick = () => go(1);
  pager.querySelector("#pager-prev").onclick = () => go(state.browsePage - 1);
  pager.querySelector("#pager-next").onclick = () => go(state.browsePage + 1);
  pager.querySelector("#pager-last").onclick = () => go(pages);
  pager.querySelector("#pager-jump").onclick = () => go(Number(pager.querySelector("#pager-go").value));
}

function setBrowseMode(mode) {
  state.browseMode = mode;
  localStorage.setItem("prism.browseMode", mode);
  $("#btn-mode-list").classList.toggle("on", mode === "list");
  $("#btn-mode-grid").classList.toggle("on", mode === "grid");
  if (state.view === "browse") loadBrowse();
}

/* ---------------------------------------------------------------- 近似重复 */

/* 点「近似重复」里的一行 → 并排预览两个文件，确认是不是真重复 */
async function showDup(pair) {
  const one = (x, side) => {
    const name = displayName(x.path);
    const u = fileUrl(x.path);
    const kind = /\.(png|jpe?g|webp|gif|bmp)$/i.test(x.path) ? "image"
               : /\.(mp4|webm|mov|mkv|avi|m4v|flv|wmv|mpg|mpeg)$/i.test(x.path) ? "video"
               : /\.(mp3|flac|wav|m4a|ogg|aac)$/i.test(x.path) ? "audio" : "text";
    return `<div class="dup-side">
      <div class="dup-side-head">
        <span class="dup-tag">${side}</span>
        <b>${escapeHtml(name)}</b>
        <span class="rmeta">第 ${(x.chunk_index ?? 0) + 1} 段</span>
      </div>
      <div class="dup-path">
        <span title="${escapeHtml(x.path)}">${escapeHtml(shortPath(x.path))}</span>
        <button class="btn-mini js-dup-reveal" data-path="${escapeHtml(x.path)}"
                title="在资源管理器中定位这个文件">定位</button>
        <button class="btn-mini js-dup-open" data-path="${escapeHtml(x.path)}"
                title="用系统默认程序打开">打开</button>
      </div>
      <div class="dup-body" id="dup-${side}">
        ${kind === "image" ? `<img src="${u}" alt="">`
          : kind === "video" ? `<video controls preload="metadata" src="${u}"></video>`
          : kind === "audio" ? `<audio controls preload="metadata" src="${u}"></audio>`
          : `<div class="empty">读取中…</div>`}
      </div>
    </div>`;
  };
  const box = modal({
    title: "近似重复详情 —— 相似度 " + (pair.score * 100).toFixed(2) + "%",
    body: `<div class="dup-grid">${one(pair.a, "A")}${one(pair.b, "B")}</div>
     <div class="dup-note">${pair.same_file
        ? "两侧是<strong>同一个文件的相邻段落</strong>，内容天然相似，通常不是问题。"
        : "两侧是<strong>两个不同文件</strong>。相似度高不代表内容重复 —— 尤其是音频/图片，同一个嵌入空间里的题材相近就会很接近，请以实际内容为准。"}</div>`,
    actions: [{ label: "关闭" }],
    width: "min(980px, 94vw)",
  });

  // 两侧各自的「定位 / 打开」
  // 注意 modal() 返回的是 { el, body, close }，不是 DOM 元素本身
  box.body.querySelectorAll(".js-dup-reveal").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      api("/api/reveal", { method: "POST", body: JSON.stringify({ path: b.dataset.path }) })
        .catch((err) => toast("定位失败：" + err.message));
    };
  });
  box.body.querySelectorAll(".js-dup-open").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      api("/api/open", { method: "POST", body: JSON.stringify({ path: b.dataset.path }) })
        .catch((err) => toast("打开失败：" + err.message));
    };
  });

  // 文本类型：把两边的开头拉出来显示
  for (const side of ["A", "B"]) {
    const x = side === "A" ? pair.a : pair.b;
    if (/\.(png|jpe?g|webp|gif|bmp|mp3|flac|wav|m4a|ogg|aac)$/i.test(x.path)) continue;
    try {
      const d = await api("/api/files/chunks?path=" + encodeURIComponent(x.path));
      const chunks = (d.chunks || []).slice(0, 2);
      const el = document.getElementById("dup-" + side);
      if (el) el.innerHTML = `<pre class="dup-text">${escapeHtml(chunks.map(c => c.text).join("\n\n…\n\n").slice(0, 900))}</pre>`;
    } catch (e) {
      const el = document.getElementById("dup-" + side);
      if (el) el.innerHTML = `<div class="empty">读取失败：${escapeHtml(e.message)}</div>`;
    }
  }
}

/* 渲染单独拆出来 —— 历史恢复时直接重画，不必再跑一遍 O(n²) 检测 */
function renderDupes(pairs) {
  const list = $("#dupes-list");
  const th = parseFloat($("#dup-threshold")?.value || "0.97");
  state.lastDupes = pairs || [];
  if (!pairs || !pairs.length) {
    list.innerHTML = `<div class="empty">没有发现相似度 ≥ ${th} 的配对<br>
      <span style="font-size:0.7rem">（同一个文件的不同段落默认不参与比较）</span></div>`;
    return;
  }
  list.innerHTML = `<div class="dup-hint">共 ${pairs.length} 对 · 点击任意一行并排查看两个文件</div>` +
    pairs.map((p, i) => `
    <div class="row file-row dup-row" data-dup="${i}">
      <div class="row-icon">${p.same_file ? "🗂" : "≈"}</div>
      <div class="row-main">
        <div class="rname">${escapeHtml(displayName(p.a.path))}<span class="dup-sep">≈</span>${escapeHtml(displayName(p.b.path))}</div>
        <div class="rpath">A: ${escapeHtml(shortPath(p.a.path))}${p.same_file ? "" : "　B: " + escapeHtml(shortPath(p.b.path))}</div>
        ${p.same_file ? `<div class="rpreview">同一个文件的第 ${(p.a.chunk_index ?? 0) + 1} 与第 ${(p.b.chunk_index ?? 0) + 1} 段</div>` : ""}
      </div>
      <div class="rmeta">${(p.score * 100).toFixed(2)}%</div>
    </div>`).join("");
  list.querySelectorAll(".dup-row").forEach((el) => {
    el.onclick = () => showDup(pairs[+el.dataset.dup]);
  });
}

async function runDupes(force = false) {
  const list = $("#dupes-list");
  if (!state.currentLib) { list.innerHTML = `<div class="empty">先在左侧选一个库</div>`; return; }
  const th = parseFloat($("#dup-threshold")?.value || "0.97");
  const sameFile = !!$("#dup-same-file")?.checked;
  list.innerHTML = `<div class="empty">检测中…</div>`;
  try {
    const d = await api("/api/duplicates", {
      method: "POST",
      body: JSON.stringify({ library_id: state.currentLib, threshold: th,
                             limit: 100, same_file: sameFile, force }),
    });
    if (d.blocked) {                       // 段数超护栏：先问一声，别一进来就卡死
      renderDupesBlocked(d, th, sameFile);
      state.lastDupes = null;              // 不把「被拦下」当成一份结果存档
      histPush("近似重复（已拦下）");
      return;
    }
    renderDupes(d.pairs);
    histPush(`近似重复 ≥ ${th}`);
  } catch (e) {
    list.innerHTML = `<div class="empty">检测失败：${escapeHtml(e.message)}</div>`;
  }
}

/* 超护栏时的提示：一行话 + 一个小按钮，不弹对话框（界面保持简洁） */
function renderDupesBlocked(d, th, sameFile) {
  const list = $("#dupes-list");
  const secs = d.est_seconds >= 60
    ? `${Math.round(d.est_seconds / 60)} 分钟`
    : `${d.est_seconds} 秒`;
  list.innerHTML = `
    <div class="dup-guard">
      <div class="dg-text">
        这个库要两两比对的段有 <b>${d.total.toLocaleString()}</b> 段，
        超过安全上限 ${d.limit.toLocaleString()} —— 预计约 <b>${secs}</b>，期间界面会一直卡着。
      </div>
      <button class="btn-mini" id="dup-force">仍然检测</button>
    </div>`;
  const b = list.querySelector("#dup-force");
  if (b) b.onclick = () => runDupes(true);
}

/* ---------------------------------------------------------------- 失效条目 */

/* 「原文件已找不到」的条目：一扫、一看、一移除。
   两端点都只动数据库 —— 标记是扫描出来的，移除要人明确点。 */
async function missingDialog(lib) {
  const m = await api(`/api/libraries/${lib.id}/missing`);
  const list = m.count
    ? `<div class="miss-list">${m.files.map((f) => `
        <div class="miss-row" title="${escapeHtml(f.path)}">
          <span class="miss-name">${escapeHtml(displayName(f.path))}</span>
          <span class="rmeta">${escapeHtml(shortPath(f.path))}</span>
        </div>`).join("")}</div>`
    : `<div class="empty">没有失效条目 —— 索引和磁盘是一致的</div>`;

  modal({
    title: `失效索引 · ${lib.name}（${m.count} 个文件）`,
    body: `<div class="dup-note" style="margin-top:0">
        这些条目在数据库里还留着，但<b>原文件已经在磁盘上找不到了</b>。<br>
        · <b>重新扫描</b>：检查整个库，把找不到原文件的标成失效（只标记，不删任何东西）<br>
        · <b>移除失效</b>：把这些失效条目的索引删掉（<b>只动数据库，不会碰磁盘上的文件</b>）<br>
        ⚠️ 如果是外接盘 / 网络盘没挂上导致的，请先接好盘再操作。
      </div>${list}`,
    width: "min(760px, 94vw)",
    actions: [
      { label: "重新扫描", primary: true, onClick: async ({ close }) => {
          const r = await api(`/api/libraries/${lib.id}/rescan`, { method: "POST" });
          const now = await api(`/api/libraries/${lib.id}/missing`);
          // 注意区分：r.missing 是「本次新发现」，now.count 是「库里现有总数」
          toast(`已检查 ${r.checked} 个文件：本次新标记 ${r.missing.length} 个，本库现有 ${now.count} 个失效文件`);
          await refreshHealth(); await loadLibraries();
          close();
          setTimeout(() => missingDialog(lib), 220);   // 重开，显示最新清单
          return false;
        } },
      { label: "移除失效", danger: true, onClick: async () => {
          if (!m.count) return toast("没有失效条目可移除");
          if (!confirm(`确认移除这 ${m.count} 个失效条目的索引？\n\n只删数据库记录，不动磁盘上的任何文件。`)) return false;
          const r = await api(`/api/libraries/${lib.id}/purge-missing`, { method: "POST" });
          toast(`已移除 ${r.removed} 条失效索引`);
          await refreshHealth(); await loadLibraries();
          if (state.view === "browse") await loadBrowse();
        } },
      { label: "关闭" },
    ],
  });
}

/* ---------------------------------------------------------------- 导入 */

/* 暂停页面上所有音频播放器（关抽屉/弹层/切视图时用，否则关掉还在响） */
function pauseAllAudio() {
  document.querySelectorAll("audio").forEach((a) => {
    try { a.pause(); } catch (_) {}
  });
}

/* 全局播放互斥：任意一个播放器开始播放，就暂停其余全部。
   用捕获阶段监听，所以预览格、详情弹层、近似重复并排……所有 <audio> 都覆盖，
   不需要在每个创建播放器的地方各写一遍。 */
document.addEventListener("play", (e) => {
  const el = e.target;
  if (!el || el.tagName !== "AUDIO") return;
  document.querySelectorAll("audio").forEach((a) => {
    if (a !== el && !a.paused) {
      try { a.pause(); } catch (_) {}
    }
  });
}, true);

/* ---------------------------------------------------------------- 导入格式筛选 */

/* 快捷分组 —— 必须跟 server/config.py 里的 IMAGE_EXT / VIDEO_EXT / AUDIO_EXT / TEXT_EXT 对齐 */
const EXT_GROUPS = {
  image: ["png", "jpg", "jpeg", "webp", "bmp", "gif", "tiff", "avif"],
  video: ["mp4", "webm", "mov", "mkv", "avi", "m4v", "flv", "wmv", "mpg", "mpeg"],
  audio: ["mp3", "wav", "flac", "m4a", "ogg", "opus", "aac", "wma"],
  text: ["txt", "md", "markdown", "rst", "json", "yaml", "yml", "toml", "ini", "cfg", "csv", "log",
         "py", "js", "ts", "jsx", "tsx", "html", "css", "sh", "bat", "java", "c", "cpp", "h",
         "go", "rs", "sql"],
};
/* 输入框下面给的快捷后缀（点一下直接加） */
const EXT_SUGGEST = ["txt", "md", "json", "csv", "py", "js", "png", "jpg", "webp", "avif",
                     "mp3", "wav", "flac", "mp4", "mkv", "mov"];
const normExt = (e) => String(e).trim().replace(/^\.+/, "").toLowerCase();

function saveExt() {
  localStorage.setItem("prism.includeExt", JSON.stringify(state.includeExt));
}

function renderFormat() {
  const exts = state.includeExt;
  const label = $("#format-label");
  if (label) label.textContent = exts.length ? `${exts.length} 种` : "全部";
  $("#btn-format")?.classList.toggle("on", exts.length > 0);

  // 快捷分组：整组都选了才点亮
  $$(".fp-quick").forEach((b) => {
    const g = EXT_GROUPS[b.dataset.g] || [];
    b.classList.toggle("on", g.length > 0 && g.every((e) => exts.includes(e)));
  });
  // 已选后缀
  const picked = $("#fp-picked");
  if (picked) {
    picked.innerHTML = exts.length
      ? exts.map((e) => `<span class="fp-chip">.${escapeHtml(e)}<button data-x="${escapeHtml(e)}" title="移除">✕</button></span>`).join("")
      : `<span class="fp-hint">还没有选 —— 不选就是全收</span>`;
    picked.querySelectorAll("button[data-x]").forEach((b) => {
      b.onclick = () => { state.includeExt = state.includeExt.filter((x) => x !== b.dataset.x); saveExt(); renderFormat(); };
    });
  }
  // 建议后缀（已用的压暗）
  const sg = $("#fp-suggest");
  if (sg && !sg.dataset.built) {
    sg.innerHTML = EXT_SUGGEST.map((e) => `<button class="fp-sg" data-e="${e}">.${e}</button>`).join("");
    sg.dataset.built = "1";
    sg.querySelectorAll(".fp-sg").forEach((b) => {
      b.onclick = () => addExt(b.dataset.e);
    });
  }
  sg?.querySelectorAll(".fp-sg").forEach((b) => b.classList.toggle("used", exts.includes(b.dataset.e)));
}

function addExt(raw) {
  for (const piece of String(raw).split(/[,;\s]+/)) {
    const e = normExt(piece);
    if (e && !state.includeExt.includes(e)) state.includeExt.push(e);
  }
  state.includeExt.sort();
  saveExt();
  renderFormat();
}

function setupFormatPanel() {
  renderFormat();
  $("#btn-format")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    $("#format-panel")?.classList.toggle("hidden");
  });
  $("#format-panel")?.addEventListener("click", (ev) => ev.stopPropagation());
  document.addEventListener("click", () => $("#format-panel")?.classList.add("hidden"));

  $$(".fp-quick").forEach((b) => {
    b.onclick = () => {
      const g = EXT_GROUPS[b.dataset.g] || [];
      const all = g.every((e) => state.includeExt.includes(e));
      state.includeExt = all
        ? state.includeExt.filter((e) => !g.includes(e))     // 整组已选 → 取消整组
        : Array.from(new Set([...state.includeExt, ...g])).sort();
      saveExt(); renderFormat();
    };
  });

  const inp = $("#fp-input");
  if (inp) {
    inp.onkeydown = (e) => {
      if (e.key !== "Enter") return;
      addExt(inp.value);
      inp.value = "";
    };
    inp.onblur = () => { if (inp.value.trim()) { addExt(inp.value); inp.value = ""; } };
  }
  const clr = $("#fp-clear");
  if (clr) clr.onclick = () => { state.includeExt = []; saveExt(); renderFormat(); };
}

async function pickPaths(mode) {
  if (!state.currentLib) return toast("先在左侧选一个库");
  // 文件对话框里按选定后缀预筛；文件夹对话框没有过滤参数，靠服务端 scan 兜住
  const kind = mode === "files" ? state.includeExt.join(",") : "";
  toast("已弹出选择框 —— 请看屏幕（它可能在 Prism 窗口后面）", 4000);
  try {
    const d = await api(`/api/pick?mode=${mode}&kind=${encodeURIComponent(kind)}`, { method: "POST" });
    if (!d.paths || !d.paths.length) return;
    startImport(d.paths);
  } catch (e) {
    toast("选择失败：" + e.message);
  }
}

async function startImport(paths) {
  try {
    const d = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({
        library_id: state.currentLib,
        paths,
        recursive: true,
        include_ext: state.includeExt,        // 空数组 = 不筛，服务端按 null 处理
      }),
    });
    if (state.includeExt.length) {
      toast(`开始导入（只收 ${state.includeExt.length} 种格式）`);
    }
    watchJob(d.job_id);
  } catch (e) {
    toast("导入失败：" + e.message);
  }
}

function watchJob(jobId) {
  const box = $("#progress");
  box.classList.remove("hidden");
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    let j;
    try {
      j = await api(`/api/jobs/${jobId}`);
    } catch (e) {
      clearInterval(state.jobTimer);
      return;
    }
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    $("#progress-fill").style.width = pct + "%";
    const stateCn = {
      running: "进行中", cancelling: "正在停止…", cancelled: "已中断",
      done: "完成", error: "出错", interrupted: "已中断",
    }[j.state] || j.state;
    $("#progress-text").textContent =
      `${stateCn} · ${j.done}/${j.total}` +
      ` · 失败 ${j.failed_count ?? (j.failed || []).length}` +
      (j.current ? ` · ${j.current}` : "") + (j.note ? ` · ${j.note}` : "");

    const skipped = j.skipped || [];
    const failed = j.failed || [];
    const lines = [];
    if (skipped.length) lines.push(`跳过 ${skipped.length} 项：` + skipped.slice(0, 6).map((s) => `${baseName(s.path)}(${s.reason})`).join("、"));
    if (failed.length) lines.push(`失败 ${failed.length} 项：` + failed.slice(0, 6).map((f) => `${baseName(f.path)}(${f.error})`).join("、"));
    $("#progress-failed").innerHTML = lines.map(escapeHtml).join("<br>");

    // ---- 动作按钮 ----
    const acts = $("#progress-acts");
    acts.innerHTML = "";
    if (j.state === "running" || j.state === "cancelling") {
      const b = document.createElement("button");
      b.className = "btn-danger";
      b.textContent = j.state === "cancelling" ? "正在停止…" : "停止导入";
      b.disabled = j.state === "cancelling";
      b.onclick = async () => {
        b.disabled = true;
        try {
          const r = await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
          toast(r.message || "已请求中断");
        } catch (e) {
          toast("停止失败：" + e.message);
          b.disabled = false;
        }
      };
      acts.appendChild(b);
    } else {
      if (failed.length) {
        const b = document.createElement("button");
        b.className = "btn-mini";
        b.textContent = `重试失败的 ${failed.length} 项`;
        b.onclick = async () => {
          try {
            const r = await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
            if (r.ok) { toast(`重新导入 ${r.count} 项`); watchJob(r.job_id); }
            else toast(r.message || "没有可重试的项");
          } catch (e) { toast("重试失败：" + e.message); }
        };
        acts.appendChild(b);
      }
      const b2 = document.createElement("button");
      b2.className = "btn-mini";
      b2.textContent = "关闭";
      b2.onclick = () => box.classList.add("hidden");
      acts.appendChild(b2);
    }

    if (j.state !== "running" && j.state !== "cancelling") {
      clearInterval(state.jobTimer);
      await refreshHealth();
      await loadLibraries();
      if (state.view === "browse") await loadBrowse();
    }
  }, 900);
}

/* ---------------------------------------------------------------- 抽屉（条目详情） */

async function openDrawer(itemId) {
  const d = $("#drawer"), body = $("#drawer-body");
  d.classList.remove("hidden");
  body.innerHTML = `<div class="empty">加载中…</div>`;
  try {
    const item = await api(`/api/item/${itemId}`);
    const tags = (item.tags || []);
    let media = "";
    if (item.modality === "image") {
      media = `<img src="${fileUrl(item.path)}" alt="">`;
    } else if (item.modality === "video") {
      media = `<video controls preload="metadata" style="max-width:100%" src="${fileUrl(item.path)}"></video>`;
    } else if (item.modality === "audio") {
      media = `<audio controls style="width:100%" src="${fileUrl(item.path)}"></audio>`;
    }
    body.innerHTML = `
      ${media}
      <div class="kv"><b>文件</b><span>${escapeHtml(baseName(item.path))}</span></div>
      <div class="kv"><b>类型</b><span>${MODALITY_CN[item.modality] || item.modality}${item.chunk_index ? ` · 第 ${item.chunk_index + 1} 段` : ""}</span></div>
      <div class="kv"><b>体积</b><span>${fmtSize(item.size)}</span></div>
      <div class="kv"><b>状态</b><span>${item.status === "missing" ? "源文件已失效" : "正常"}</span></div>
      <div class="kv"><b>路径</b><span style="font-family:var(--mono);font-size:.72rem">${escapeHtml(item.path)}</span></div>
      ${item.chunk_text ? `<pre>${escapeHtml(item.chunk_text.slice(0, 2000))}</pre>` : ""}
      <div class="kv"><b>标签</b><span>${tags.map((t) => `<span class="tag tag-del" data-tag="${escapeHtml(t)}" title="点一下去掉这个标签">${escapeHtml(t)}<i>✕</i></span>`).join(" ") || "—"}</span></div>
      <div class="tag-input">
        <input id="tag-input" placeholder="加标签后回车">
      </div>
      <div class="actions">
        <button class="btn-ghost" id="act-reveal">在资源管理器中显示</button>
        <button class="btn-ghost" id="act-move">移到别的库…</button>
        <button class="btn-ghost" id="act-link">关联到虚拟文件…</button>
        <button class="btn-ghost" id="act-similar">找相似的</button>
        <button class="btn-ghost" id="act-delete" style="color:var(--warn)">从本库移除</button>
      </div>`;

    body.querySelector("#tag-input").onkeydown = async (e) => {
      if (e.key !== "Enter") return;
      const tag = e.target.value.trim();
      if (!tag) return;
      await api(`/api/items/${itemId}/tags`, { method: "POST", body: JSON.stringify({ tag }) });
      await loadTagBar();                    // 标签条跟着更新（新标签/计数变化）
      openDrawer(itemId);
    };

    // 点标签上的 ✕ 去掉它（之前只能加不能删）
    body.querySelectorAll(".tag-del").forEach((el) => {
      el.onclick = async () => {
        const t = el.dataset.tag;
        if (!t) return;
        try {
          await api(`/api/items/${itemId}/tags?tag=${encodeURIComponent(t)}`, { method: "DELETE" });
          await loadTagBar();
          if (state.view === "browse") await loadBrowse();
          toast(`已去掉标签「${t}」`);
          openDrawer(itemId);
        } catch (err) {
          toast("删标签失败：" + (err.message || err));
        }
      };
    });
    body.querySelector("#act-move").onclick = () => moveDialog([itemId]);
    body.querySelector("#act-link").onclick = () => linkToVirtual(item.path);
    body.querySelector("#act-delete").onclick = async () => {
      if (!confirm("从本库移除这条索引？（不会删除源文件）")) return;
      await api(`/api/items/${itemId}`, { method: "DELETE" });
      d.classList.add("hidden");
      await refreshHealth(); await loadLibraries();
      if (state.view === "browse") await loadBrowse();
      toast("已移除索引");
    };
    body.querySelector("#act-similar").onclick = async () => {
      const r = await api("/api/similar", {
        method: "POST",
        body: JSON.stringify({ library_id: state.currentLib, item_id: itemId, top_k: 12 }),
      });
      d.classList.add("hidden");
      setView("search");
      const rs = r.results.filter((x) => x.id !== itemId);
      state.blobName = "";
      const bq = $("#blob-query");
      if (bq) { bq.style.display = "none"; bq.innerHTML = ""; }
      if (rs.length) renderCards(rs); else clearResults();
      histPush(`找相似的：${baseName(item.path)}`);
    };
    body.querySelector("#act-reveal").onclick = () => {
      api("/api/reveal", { method: "POST", body: JSON.stringify({ path: item.path }) })
        .catch((e) => toast("无法打开：" + e.message));
    };
  } catch (e) {
    body.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

/* ---------------------------------------------------------------- 通用弹层 */

function modal({ title, body, actions, width }) {
  const wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.innerHTML = `
    <div class="modal-box"${width ? ` style="width:${width}"` : ""}>
      <div class="modal-head">
        <span class="label">${escapeHtml(title)}</span>
        <button class="btn-ghost js-x">✕</button>
      </div>
      <div class="modal-body js-body"></div>
      <div class="modal-foot js-foot"></div>
    </div>`;
  document.body.appendChild(wrap);

  const bodyEl = wrap.querySelector(".js-body");
  const footEl = wrap.querySelector(".js-foot");
  if (typeof body === "string") bodyEl.innerHTML = body;
  else if (body) bodyEl.appendChild(body);

  const close = () => { pauseAllAudio(); wrap.remove(); };
  wrap.querySelector(".js-x").onclick = close;
  wrap.onclick = (e) => { if (e.target === wrap) close(); };

  for (const a of actions || []) {
    const b = document.createElement("button");
    b.className = a.danger ? "btn-danger" : (a.primary ? "btn" : "btn-ghost");
    b.textContent = a.label;
    b.onclick = async () => {
      if (!a.onClick) return close();
      const r = await a.onClick({ close, body: bodyEl });
      if (r !== false) close();
    };
    footEl.appendChild(b);
  }
  const first = wrap.querySelector("input, textarea, select");
  if (first) setTimeout(() => first.focus(), 60);
  return { el: wrap, body: bodyEl, close };
}

function confirmDialog(title, message, onYes, danger = false) {
  return modal({
    title,
    body: `<div class="modal-text">${escapeHtml(message).replace(/\n/g, "<br>")}</div>`,
    actions: [
      { label: danger ? "确认移除" : "确认", danger, primary: !danger, onClick: onYes },
      { label: "取消" },
    ],
  });
}

/* ---------------------------------------------------------------- 虚拟文件 */

const VKIND_CN = { image: "配图", audio: "音频", video: "视频" };
/* 用具体后缀而不是分组名 —— pick.py 的 kind 参数两种写法都认，但后缀写死更可控 */
const VKIND_EXT = {
  image: "jpg,jpeg,png,webp,gif,bmp,avif,tif,tiff",
  audio: "mp3,wav,flac,m4a,aac,ogg,opus",
  video: "mp4,mkv,avi,mov,webm,flv,wmv",
};

/* 路径比较：Windows 大小写不敏感，斜杠方向也可能不同 */
function samePath(a, b) {
  const norm = (s) => String(s || "").replace(/\\/g, "/").toLowerCase();
  return norm(a) === norm(b);
}

/* 是不是虚拟文件的伪路径 prism://virtual/<id> */
function db_isVirtual(path) {
  return String(path || "").startsWith("prism://virtual/");
}

/* 弹出原生选择框挑文件 / 文件夹（浏览器拿不到真实路径，必须走后端）。
   拿不到东西时**要说清为什么** —— 否则用户看到的就是「点了没反应」。 */
async function pickFiles(kind, mode) {
  const md = mode || "files";
  toast(md === "dir" ? "已弹出「选择文件夹」—— 请看屏幕（它可能在 Prism 窗口后面）"
                     : "已弹出「选择文件」—— 请看屏幕（它可能在 Prism 窗口后面）", 4000);
  try {
    const d = await api(`/api/pick?mode=${md}&kind=${encodeURIComponent(kind || "")}`, { method: "POST" });
    const paths = d.paths || [];
    if (!paths.length) {
      toast(d.stderr
        ? ("选择框报了错：" + String(d.stderr).slice(0, 140))
        : "没选到任何东西（对话框可能被挡在窗口后面，或者你点了取消）", 5000);
    }
    return paths;
  } catch (e) {
    toast("选择失败：" + e.message, 5000);
    return [];
  }
}

/* 虚拟文件的右侧预览抽屉。

   和普通条目一样是「左键点开看详情」，不是直接进编辑 —— 四类内容都在这里预览：
   封面大图 / 描述文本 / 音频 / 视频。**「编辑」按钮放最下面**（要往下滑才看到）。
*/
async function openVirtualDrawer(vid) {
  const d = $("#drawer"), body = $("#drawer-body");
  d.classList.remove("hidden");
  body.innerHTML = `<div class="empty">加载中…</div>`;

  let v;
  try {
    v = await api(`/api/virtual/${vid}`);
  } catch (e) {
    body.innerHTML = `<div class="empty">读取失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  const assets = v.assets || [];
  const imgs = assets.filter((a) => a.kind === "image");
  const auds = assets.filter((a) => a.kind === "audio");
  const vids = assets.filter((a) => a.kind === "video");
  const rel = v.related || [];
  const relDir = v.related_is_dir || [];
  const cover = v.cover || (imgs[0] && imgs[0].src) || "";

  const p = [];
  if (cover) p.push(`<img class="drawer-cover" src="${fileUrl(cover)}" alt="">`);
  p.push(`<div class="kv"><b>类型</b><span><span class="virt-tag">虚拟文件</span>
      ${assets.length} 个附加媒体 · 关联 ${rel.length} 项</span></div>`);
  p.push(`<div class="kv"><b>标题</b><span>${escapeHtml(v.title || "未命名")}</span></div>`);
  if (v.description) p.push(`<div class="kv"><b>描述</b><span></span></div><pre>${escapeHtml(v.description)}</pre>`);

  p.push(`<div class="kv"><b>标签</b><span>${
    (v.tags || []).map((t) => `<span class="tag tag-del" data-tag="${escapeHtml(t)}" title="点一下去掉这个标签">${escapeHtml(t)}<i>✕</i></span>`).join(" ")
    || "—"}</span></div>
    <div class="tag-input"><input id="vd-tag" placeholder="加标签后回车"></div>`);

  for (const a of imgs) {
    p.push(`<div class="kv"><b>配图</b><span>${escapeHtml(a.label)}${samePath(cover, a.src) ? " · 封面" : ""}</span></div>
      <img class="drawer-img" src="${fileUrl(a.src)}" alt="">`);
  }
  for (const a of auds) {
    p.push(`<div class="kv"><b>音频</b><span>${escapeHtml(a.label)}${a.segments > 1 ? ` · 取样 ${a.segments} 段` : ""}</span></div>
      <audio controls preload="none" src="${fileUrl(a.src)}"></audio>`);
  }
  for (const a of vids) {
    p.push(`<div class="kv"><b>视频</b><span>${escapeHtml(a.label)}</span></div>
      <video controls preload="metadata" style="max-width:100%" src="${fileUrl(a.src)}"></video>`);
  }

  p.push(`<div class="kv"><b>关联</b><span></span></div>
    <div class="pick-list">${
      rel.map((q, i) => `<button class="pick-item js-vd-locate" data-path="${escapeHtml(q)}">
          <span>${relDir[i] ? "📁" : "📄"} ${escapeHtml(baseName(q))}</span>
          <span style="color:var(--ink-3);font-size:.7rem;flex-shrink:0">${relDir[i] ? "文件夹" : "文件"}</span>
        </button>`).join("") || `<div class="vf-empty">还没关联任何东西</div>`}</div>`);

  p.push(`<div class="actions">
    <button class="btn-ghost" id="vd-similar">找相似的</button>
    <button class="btn-ghost" id="vd-edit">✎ 编辑这条虚拟文件</button>
  </div>`);
  body.innerHTML = p.join("");

  // ---- 交互 ----
  const saveTags = async (tags) => {
    try {
      const r = await api(`/api/virtual/${vid}/tags`, { method: "POST", body: JSON.stringify({ tags }) });
      toast("标签已更新");
      await loadTagBar();
      if ($("#q").value.trim()) doSearch();
      return r.tags || [];
    } catch (e) {
      toast("标签没存上：" + e.message);
      return v.tags || [];
    }
  };
  body.querySelector("#vd-tag").onkeydown = async (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const t = e.target.value.trim();
    if (!t) return;
    const next = [...(v.tags || [])];
    if (!next.some((x) => x.toLowerCase() === t.toLowerCase())) next.push(t);
    await saveTags(next);
    openVirtualDrawer(vid);
  };
  body.querySelectorAll(".tag-del").forEach((el) => {
    el.onclick = async () => {
      await saveTags((v.tags || []).filter((x) => x !== el.dataset.tag));
      openVirtualDrawer(vid);
    };
  });
  body.querySelectorAll(".js-vd-locate").forEach((b) => {
    b.onclick = () => api("/api/reveal", { method: "POST", body: JSON.stringify({ path: b.dataset.path }) })
      .catch((err) => toast("无法打开：" + err.message));
  });
  body.querySelector("#vd-edit").onclick = () => openVirtualEditor(vid);

  /* 找相似的 —— 走的是**文件级**比较（服务端把这条虚拟文件的全部片段都拿来比，
     取"最像的那一处"作为两文件相似度），所以四种模态一起参与，不用特殊处理。 */
  body.querySelector("#vd-similar").onclick = async () => {
    // 用「标题片段」的 id 当入口 —— 它自己没有向量，服务端会自动改用该文件的全部向量
    const entry = v.tag_item_id || (v.assets && v.assets[0] && v.assets[0].item_id);
    if (!entry) return toast("这条虚拟文件还没有可比较的片段");
    try {
      const r = await api("/api/similar", {
        method: "POST",
        body: JSON.stringify({ library_id: state.currentLib, item_id: entry, top_k: 12 }),
      });
      d.classList.add("hidden");
      setView("search");
      const rs = r.results || [];
      state.blobName = "";
      const bq = $("#blob-query");
      if (bq) { bq.style.display = "none"; bq.innerHTML = ""; }
      if (rs.length) renderCards(rs); else clearResults();
      histPush(`找相似的：${v.title || "虚拟文件"}`);
    } catch (e) {
      toast("找相似失败：" + e.message);
    }
  };
}

/* 新建 / 编辑一个虚拟文件。vid 省略＝新建。 */
async function openVirtualEditor(vid) {
  if (!state.currentLib) return toast("先在左侧选一个库");

  let d = null;
  if (vid) {
    try { d = await api(`/api/virtual/${vid}`); }
    catch (e) { return toast("读取失败：" + e.message); }
  }

  // 草稿：改来改去都在这上面，点「保存」才提交
  const rel0 = d ? (d.related || []) : [];
  const relDir0 = d ? (d.related_is_dir || []) : [];
  const draft = {
    title: d ? (d.title || "") : "",
    description: d ? (d.description || "") : "",
    cover: d ? (d.cover || "") : "",
    // 关联项分文件和文件夹（文件夹「定位」是打开它，文件是选中高亮）
    related: rel0.map((p, i) => ({ path: p, isDir: !!relDir0[i] })),
    tags: d ? [...(d.tags || [])] : [],
    assets: d ? (d.assets || []).map((a) => ({ kind: a.kind, src: a.src, label: a.label })) : [],
  };

  const form = document.createElement("div");
  form.className = "form vform";

  /* 把输入框里的改动收回草稿。定义在 render 外面 —— render 和 addRelated 都要用，
     放在 render 里面就只有它自己能看见（addRelated 会 ReferenceError）。 */
  const sync = () => {
    draft.title = form.querySelector("#vf-title").value;
    draft.description = form.querySelector("#vf-desc").value;
    draft.tagDraft = form.querySelector("#vf-tag").value;
  };

  /* 描述框自动撑高 —— 长描述不该被框子截掉下半截。
     🔴 必须等 form **插进文档之后再调**：元素脱离文档时没有布局，
     `scrollHeight` 恒为 0，算出来的高度永远是下限，对长文本完全无效
     （这是上一版「加了 autoGrow 却还是被截」的原因）。
     所以 render 里调一次（拿到下限就够了），modal() 挂上去之后再调一次（这次才是真高度）。 */
  const autoGrow = () => {
    const ta = form.querySelector("#vf-desc");
    if (!ta) return;
    ta.style.height = "auto";                     // 先归零，否则只会变长不会变短
    ta.style.height = Math.max(ta.scrollHeight, 110) + "px";
  };

  const render = () => {
    const group = (k) => {
      const items = draft.assets.map((a, i) => ({ ...a, i })).filter((a) => a.kind === k);
      return `
        <div class="vf-group">
          <div class="vf-head">
            <span>${VKIND_CN[k]}</span>
            <button class="btn-mini js-vadd" data-k="${k}">＋ 添加</button>
          </div>
          <div class="vf-list">
            ${items.map((a) => {
              const isCover = k === "image" && draft.cover && samePath(draft.cover, a.src);
              return `<div class="vf-item">
                <span class="vf-name" title="${escapeHtml(a.src)}">${escapeHtml(a.label || baseName(a.src))}</span>
                ${k === "image"
                  ? `<button class="btn-mini js-vcover${isCover ? " on" : ""}" data-i="${a.i}">${isCover ? "★ 封面" : "设为封面"}</button>`
                  : ""}
                <button class="btn-mini js-vdel" data-i="${a.i}" title="移除">✕</button>
              </div>`;
            }).join("") || `<div class="vf-empty">还没有</div>`}
          </div>
        </div>`;
    };

    form.innerHTML = `
      <label><span>标题</span>
        <input id="vf-title" type="text" placeholder="比如：指环王1 —— 护戒使者（标题触发关键词检索）">
      </label>
      <label><span>描述</span>
        <textarea id="vf-desc" rows="5" placeholder="剧情梗概 / 介绍 —— 走语义检索（可选。框子会长到刚好放下全部文字）"></textarea>
      </label>
      ${["image", "audio", "video"].map(group).join("")}
      <div class="vf-group">
        <div class="vf-head"><span>标签</span></div>
        <div class="vf-tags">
          ${draft.tags.map((t, i) => `<span class="tag tag-del" data-ti="${i}" title="点一下去掉这个标签">${escapeHtml(t)}<i>✕</i></span>`).join("") ||
            `<span class="vf-none">还没有标签</span>`}
        </div>
        <input id="vf-tag" type="text" placeholder="打完标签按回车加入（和文件标签一样，会参与关键词检索）">
      </div>
      <div class="vf-group">
        <div class="vf-head">
          <span>关联的本地文件 / 文件夹</span>
          <span class="vf-head-btns">
            <button class="btn-mini js-vadd-rel">＋ 文件</button>
            <button class="btn-mini js-vadd-reldir">＋ 文件夹</button>
          </span>
        </div>
        <div class="vf-list">
          ${draft.related.map((r, i) => `<div class="vf-item">
            <span class="vf-name" title="${escapeHtml(r.path)}">${r.isDir ? "📁" : "📄"} ${escapeHtml(baseName(r.path))}</span>
            <button class="btn-mini js-vdel-rel" data-i="${i}" title="移除">✕</button>
          </div>`).join("") ||
            `<div class="vf-empty">还没有 —— 加上之后，检索到这条时「定位」会打开它。<br>
             文件夹也可以（比如整个项目目录），这样能把本地项目也纳进检索。</div>`}
        </div>
      </div>`;

    // 用 .value 而不是写进 HTML，免得标题里的引号把属性截断
    form.querySelector("#vf-title").value = draft.title;
    form.querySelector("#vf-desc").value = draft.description;
    form.querySelector("#vf-tag").value = draft.tagDraft || "";
    autoGrow();                                   // 此时还没挂进文档，只拿到下限

    form.querySelector("#vf-title").oninput = sync;
    form.querySelector("#vf-desc").oninput = () => { sync(); autoGrow(); };
    form.querySelector("#vf-tag").oninput = sync;

    form.querySelectorAll(".js-vadd").forEach((b) => {
      b.onclick = async () => {
        sync();
        const k = b.dataset.k;
        const paths = await pickFiles(VKIND_EXT[k]);
        for (const p of paths) {
          if (!draft.assets.some((a) => a.kind === k && samePath(a.src, p))) {
            draft.assets.push({ kind: k, src: p, label: baseName(p) });
          }
        }
        if (k === "image" && !draft.cover && paths.length) draft.cover = paths[0];
        render();
      };
    });
    form.querySelectorAll(".js-vcover").forEach((b) => {
      b.onclick = () => { sync(); draft.cover = draft.assets[+b.dataset.i].src; render(); };
    });
    form.querySelectorAll(".js-vdel").forEach((b) => {
      b.onclick = () => {
        sync();
        const a = draft.assets[+b.dataset.i];
        if (a && samePath(draft.cover, a.src)) draft.cover = "";   // 删掉的正好是封面
        draft.assets.splice(+b.dataset.i, 1);
        render();
      };
    });
    form.querySelector(".js-vadd-rel").onclick = () => addRelated(false);
    form.querySelector(".js-vadd-reldir").onclick = () => addRelated(true);
    form.querySelectorAll(".js-vdel-rel").forEach((b) => {
      b.onclick = () => { sync(); draft.related.splice(+b.dataset.i, 1); render(); };
    });

    /* 标签：回车加入、点 ✕ 去掉 —— 和普通文件用的是同一套标签 */
    form.querySelector("#vf-tag").onkeydown = (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const t = e.target.value.trim();
      if (!t) return;
      sync();
      if (!draft.tags.some((x) => x.toLowerCase() === t.toLowerCase())) draft.tags.push(t);
      draft.tagDraft = "";
      render();
      form.querySelector("#vf-tag")?.focus();
    };
    form.querySelectorAll(".vf-tags .tag-del").forEach((el) => {
      el.onclick = () => { sync(); draft.tags.splice(+el.dataset.ti, 1); render(); };
    });
  };

  /* 关联本地文件 / 文件夹。文件夹也支持 —— 这样整个项目目录都能纳进来 */
  const addRelated = async (isDir) => {
    sync();
    const paths = await pickFiles("", isDir ? "dir" : "files");
    let n = 0;
    for (const p of paths) {
      if (!draft.related.some((q) => samePath(q.path, p))) {
        draft.related.push({ path: p, isDir });
        n += 1;
      }
    }
    if (paths.length && n === 0) toast("这些已经关联过了");
    else if (n) toast(`已添加 ${n} 项`);
    render();
  };

  render();

  const box = modal({
    title: vid ? "编辑虚拟文件" : "新建虚拟文件",
    width: "52rem",
    body: form,
    actions: [
      {
        label: vid ? "保存" : "创建",
        primary: true,
        onClick: async ({ close }) => {
          draft.title = form.querySelector("#vf-title").value.trim();
          draft.description = form.querySelector("#vf-desc").value.trim();
          if (!draft.title) { toast("标题不能为空 —— 它是关键词检索的触发词"); return false; }
          const payload = {
            title: draft.title,
            description: draft.description,
            cover: draft.cover,
            related: draft.related.map((r) => r.path),      // 文件和文件夹一视同仁
            assets: draft.assets.map((a) => ({ kind: a.kind, src: a.src, label: a.label })),
          };
          try {
            const r = vid
              ? await api(`/api/virtual/${vid}`, { method: "PATCH", body: JSON.stringify(payload) })
              : await api(`/api/libraries/${state.currentLib}/virtual`, { method: "POST", body: JSON.stringify(payload) });
            const newVid = vid || r.vid;
            // 标签单独存（挂在标题片段上，不跟着主保存走）
            if (newVid) {
              try {
                await api(`/api/virtual/${newVid}/tags`, {
                  method: "POST", body: JSON.stringify({ tags: draft.tags }),
                });
              } catch (e2) {
                toast("标签没存上：" + e2.message, 5000);
              }
            }
            close();
            const skipped = r.skipped || [];
            toast(skipped.length
              ? `已保存，但有 ${skipped.length} 个没嵌进去：${skipped.join("；")}`
              : (vid ? "已保存" : `已创建（${r.chunks} 个片段，嵌入 ${r.embedded} 个）`), 6000);
            await loadTagBar();                         // 标签条跟着更新
            if ($("#q").value.trim()) doSearch();       // 有查询就重跑一遍，立刻看到效果
          } catch (e) {
            toast("保存失败：" + e.message);
            return false;
          }
        },
      },
      ...(vid ? [{
        label: "删除", danger: true,
        onClick: async ({ close }) => {
          if (!confirm("删除这条虚拟文件？\n只删索引和它的附加信息，关联的本地文件不会被碰。")) return false;
          try {
            await api(`/api/virtual/${vid}`, { method: "DELETE" });
            close();
            toast("已删除虚拟文件");
            if ($("#q").value.trim()) doSearch();
          } catch (e) {
            toast("删除失败：" + e.message);
            return false;
          }
        },
      }] : []),
      { label: "取消" },
    ],
  });
  // 🔴 modal() 把 form 挂进文档了，这时候 scrollHeight 才是真的 —— 再撑一次
  autoGrow();
  requestAnimationFrame(autoGrow);
  return box;
}

/* 把一个真实文件「关联到某条虚拟文件」（问题2 的 B：不用翻对话框，从结果直接挂上去） */
async function linkToVirtual(path) {
  if (db_isVirtual(path)) return toast("这本来就是虚拟文件");
  let list = [];
  try {
    const d = await api(`/api/libraries/${state.currentLib}/virtual`);
    list = d.items || [];
  } catch (e) {
    return toast("读取虚拟文件失败：" + e.message);
  }
  if (!list.length) return toast("这个库还没有虚拟文件 —— 先在左侧「＋ 虚拟文件」建一条");

  const body = document.createElement("div");
  body.className = "pick-list";
  body.innerHTML = list.map((v) => `
    <button class="pick-item js-pick-v" data-vid="${v.id}">
      <span>${escapeHtml(v.title || "未命名")}</span>
      <span style="color:var(--ink-3);font-size:.72rem;flex-shrink:0">${(v.related || []).length} 个关联文件</span>
    </button>`).join("");

  const box = modal({ title: "关联到哪条虚拟文件？", body, actions: [{ label: "取消" }] });
  body.querySelectorAll(".js-pick-v").forEach((b) => {
    b.onclick = async () => {
      const vid = Number(b.dataset.vid);
      const v = list.find((x) => x.id === vid);
      const rel = [...(v.related || [])];
      if (rel.some((q) => samePath(q, path))) {
        box.close();
        return toast("它已经关联在这条虚拟文件上了");
      }
      rel.push(path);
      try {
        await api(`/api/virtual/${vid}`, { method: "PATCH", body: JSON.stringify({ related: rel }) });
        box.close();
        toast(`已关联到「${v.title}」`);
      } catch (e) {
        toast("关联失败：" + e.message);
      }
    };
  });
}

/* ---------------------------------------------------------------- 库管理 */

async function newLibrary() {
  const form = document.createElement("div");
  form.className = "form";
  form.innerHTML = `
    <label><span>名称</span><input id="nl-name" type="text" placeholder="比如：我的素材"></label>
    <label><span>主要类型</span>
      <select id="nl-type">
        <option value="mixed">mixed —— 混装（图片/文本/音频都有）</option>
        <option value="image">image —— 图片为主</option>
        <option value="video">video —— 视频为主</option>
        <option value="text">text —— 文本为主</option>
        <option value="audio">audio —— 音频为主</option>
      </select>
    </label>
    <label><span>描述</span><textarea id="nl-desc" rows="2" placeholder="这个库打算放什么？（可选）"></textarea></label>`;
  modal({
    title: "新建库",
    body: form,
    actions: [
      {
        label: "创建", primary: true,
        onClick: async () => {
          const name = form.querySelector("#nl-name").value.trim();
          if (!name) { toast("名称不能为空"); return false; }
          try {
            const d = await api("/api/libraries", {
              method: "POST",
              body: JSON.stringify({
                name,
                primary_type: form.querySelector("#nl-type").value,
                description: form.querySelector("#nl-desc").value.trim(),
              }),
            });
            await loadLibraries();
            await selectLibrary(d.id);
            toast(`库「${name}」已创建`);
          } catch (e) {
            toast("创建失败：" + e.message);
            return false;
          }
        },
      },
      { label: "取消" },
    ],
  });
}

async function editLibrary(lib) {
  const form = document.createElement("div");
  form.className = "form";
  form.innerHTML = `
    <label><span>名称</span><input id="el-name" type="text" value="${escapeHtml(lib.name)}"></label>
    <label><span>主要类型</span>
      <select id="el-type">
        ${["mixed", "image", "video", "text", "audio"].map((t) =>
          `<option value="${t}"${lib.primary_type === t ? " selected" : ""}>${t}</option>`).join("")}
      </select>
    </label>
    <label><span>描述</span><textarea id="el-desc" rows="3" placeholder="这个库里放了什么？">${escapeHtml(lib.description || "")}</textarea></label>
    <div class="form-meta">
      ${lib.file_count ?? "?"} 个文件 · ${lib.item_count ?? "?"} 段索引${lib.created_at ? ` · 建于 ${String(lib.created_at).slice(0, 10)}` : ""}
    </div>`;
  const box = modal({
    title: "编辑库",
    body: form,
    actions: [
      {
        label: "保存", primary: true,
        onClick: async () => {
          const name = form.querySelector("#el-name").value.trim();
          if (!name) { toast("名称不能为空"); return false; }
          try {
            await api(`/api/libraries/${lib.id}`, {
              method: "POST",
              body: JSON.stringify({
                name,
                primary_type: form.querySelector("#el-type").value,
                description: form.querySelector("#el-desc").value.trim(),
              }),
            });
            await loadLibraries();
            if (state.view === "browse") await loadBrowse();
            toast("已保存");
          } catch (e) {
            toast("保存失败：" + e.message);
            return false;
          }
        },
      },
      {
        label: "删除这个库", danger: true,
        onClick: () => {
          confirmDialog(
            `删除库「${lib.name}」？`,
            `会移除该库的全部索引记录（${lib.item_count ?? "?"} 条）。\n源文件不会被删除，之后还可以重新导入。`,
            async () => {
              await api(`/api/libraries/${lib.id}`, { method: "DELETE" });
              if (state.currentLib === lib.id) state.currentLib = null;
              await refreshHealth(); await loadLibraries();
              box.close();                     // ← 弹层必须自己关掉（原来这里一直挂着）
              await resetAfterLibGone();
              toast("库已删除");
            },
            true
          );
          return false;
        },
      },
      { label: "取消" },
    ],
  });
}

async function moveDialog(itemIds) {
  const others = state.libs.filter((l) => l.id !== state.currentLib);
  if (!others.length) return toast("没有别的库可移动");
  const box = document.createElement("div");
  box.className = "pick-list";
  box.innerHTML = others.map((l) =>
    `<button class="pick-item" data-id="${l.id}">
       <span>${escapeHtml(l.name)}</span>
       <em>${l.file_count ?? l.item_count ?? 0} 个文件</em>
     </button>`).join("");
  const m = modal({ title: `移动 ${itemIds.length} 条索引到…`, body: box, actions: [{ label: "取消" }] });
  box.querySelectorAll(".pick-item").forEach((b) => {
    b.onclick = async () => {
      const target = Number(b.dataset.id);
      const d = await api("/api/items/move", {
        method: "POST",
        body: JSON.stringify({ item_ids: itemIds, target_library_id: target }),
      });
      m.close();
      toast(`已移动 ${d.moved} 条`);
      $("#drawer").classList.add("hidden");
      await refreshHealth(); await loadLibraries();
      if (state.view === "browse") await loadBrowse();
    };
  });
}

/* ---------------------------------------------------------------- 设置 */

async function openSettings() {
  let d;
  try {
    d = await api("/api/settings");
  } catch (e) {
    return toast("读取设置失败：" + e.message);
  }
  const c = d.current;
  const form = document.createElement("div");
  form.className = "form";
  form.innerHTML = `
    <label><span>嵌入服务地址</span>
      <input id="st-api" type="text" value="${escapeHtml(c.embed_api)}" placeholder="http://127.0.0.1:8084/v1/embeddings"></label>
    <label><span>模型名</span>
      <input id="st-model" type="text" value="${escapeHtml(c.embed_model)}" placeholder="embeddinggemma-2"></label>
    <div class="form-row">
      <label><span>向量维度</span><input id="st-dim" type="number" value="${c.embed_dim}" min="16" step="1"></label>
      <label><span>超时（秒）</span><input id="st-timeout" type="number" value="${c.embed_timeout}" min="5" step="1"></label>
    </div>
    <div class="form-row">
      <label><span>音频每段时长（秒）</span><input id="st-audiochunk" type="number" value="${c.audio_chunk_seconds || 60}" min="10" step="10"></label>
      <label><span>ffmpeg 路径</span><input id="st-ffmpeg" type="text" value="${escapeHtml(c.ffmpeg_path || "")}" placeholder="留空 = 自动探测"></label>
    </div>
    <div class="form-note">
      任何提供 <code>POST /v1/embeddings</code> 的服务都能接（llama.cpp / Ollama / 其他 OpenAI 兼容实现）。
      <br>⚠ 改<strong>维度</strong>后，用旧维度建的索引在检索时会被跳过（不会报错，但查不到）。
      <br>🎵 音频会自动按「每段时长」切开再嵌入 —— 嵌入模型的上下文有限，整段长音频会直接失败。
      <br>切分需要 <strong>ffmpeg</strong>；留空则自动探测，探不到时在上面填路径。
    </div>
    <div id="st-result" class="form-result hidden"></div>
    <div class="form-meta">配置文件：${escapeHtml(d.file)}${d.file_exists ? "" : "（尚未创建，保存后生成）"}</div>`;

  const res = form.querySelector("#st-result");
  const vals = () => ({
    embed_api: form.querySelector("#st-api").value.trim(),
    embed_model: form.querySelector("#st-model").value.trim(),
    embed_dim: Number(form.querySelector("#st-dim").value),
    embed_timeout: Number(form.querySelector("#st-timeout").value),
    audio_chunk_seconds: Number(form.querySelector("#st-audiochunk").value),
    ffmpeg_path: form.querySelector("#st-ffmpeg").value.trim(),
  });

  modal({
    title: "设置 · 嵌入服务",
    body: form,
    actions: [
      {
        label: "测试连接",
        onClick: async () => {
          res.classList.remove("hidden");
          res.className = "form-result";
          res.textContent = "正在测试…";
          try {
            const t = await api("/api/settings/test", { method: "POST", body: JSON.stringify(vals()) });
            res.className = "form-result " + (t.ok ? (t.match ? "ok" : "warn") : "bad");
            res.textContent = t.message;
          } catch (e) {
            res.className = "form-result bad";
            res.textContent = "测试失败：" + e.message;
          }
          return false;       // 不关闭弹层
        },
      },
      {
        label: "保存", primary: true,
        onClick: async () => {
          try {
            const r = await api("/api/settings", { method: "POST", body: JSON.stringify(vals()) });
            const n = Object.keys(r.changed || {}).length;
            toast(n ? `已保存 ${n} 项，立即生效` : "没有改动");
            await refreshHealth();
          } catch (e) {
            toast("保存失败：" + e.message);
            return false;
          }
        },
      },
      {
        label: "恢复默认",
        onClick: async () => {
          await api("/api/settings/reset", { method: "POST" });
          toast("已恢复为默认配置");
          await refreshHealth();
        },
      },
      { label: "关闭" },
    ],
  });
}

/* ---------------------------------------------------------------- 拖拽 */

/* 拖放落点。原生窗口（pywebview）靠它分工：
     拖到检索栏 → 页面自己的 drop 处理（拖入检索：上传内容算向量）
     拖到别处   → Python 侧接手（拖入即入库：拿绝对路径，见 server/desktop.py）
   普通浏览器里这个变量没人读（浏览器拿不到路径，只能退回选择框）。 */
window.__prismDropTarget = "";

/* 原生窗口拿到绝对路径后回调这里（由 server/desktop.py 调用）*/
window.__prismNativeDrop = (paths) => {
  const target = window.__prismDropTarget || "";
  window.__prismDropTarget = "";
  if (target === "search") return "search-handled";     // 检索栏归页面自己管
  if (!state.currentLib) { toast("先在左侧选一个库，再把文件拖进来"); return "no-lib"; }
  const list = (paths || []).filter(Boolean);
  if (!list.length) { toast("没拿到可用路径"); return "no-paths"; }
  startImport(list);
  toast(`已接收 ${list.length} 个文件，开始入库…`);
  return "importing";
};

/* 是否跑在原生窗口（WebView2）里。
   ⚠️ 别用 window.pywebview 判定 —— 那个对象只在传了 js_api 时才注入，默认可能没有。
   pywebview 在 Windows 上自己就是靠 window.chrome.webview 通信的
   （.venv/Lib/site-packages/webview/js/api.js:97），所以它才是可靠判据。
   __prismNative 是 desktop.py 载入后补设的保险。 */
function isNativeWindow() {
  return !!(window.__prismNative
            || (window.chrome && window.chrome.webview)
            || window.pywebview);
}

function setupDrop() {
  const dz = $("#dropzone");
  const bar = document.querySelector(".searchbar");

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("hot");
      window.__prismDropTarget = "import";
    }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("hot"); }));

  if (bar) {
    ["dragenter", "dragover"].forEach((ev) =>
      bar.addEventListener(ev, () => { window.__prismDropTarget = "search"; }));
    bar.addEventListener("dragleave", () => {
      if (window.__prismDropTarget === "search") window.__prismDropTarget = "";
    });
  }

  dz.addEventListener("drop", (e) => {
    const files = Array.from(e.dataTransfer?.files || []);
    if (!files.length) return;
    /* 判据要在这里现算 —— setupDrop 跑在 boot 时，
       Python 侧可能还没来得及补设 __prismNative */
    if (isNativeWindow()) {
      // 原生窗口：路径由 Python 侧拿到并回调 __prismNativeDrop，这里不用做任何事
      window.__prismDropTarget = "";
      return;
    }
    // 普通浏览器：拿不到绝对路径 → 只能引导用原生选择框
    toast("浏览器拿不到拖入文件的真实路径，已为你打开选择框", 4000);
    if (files.length === 1 && !files[0].type) {
      pickPaths("dir");          // 拖的是文件夹
    } else {
      pickPaths("files");
    }
  });
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  setupDrop();
  $$(".tab").forEach((t) => (t.onclick = () => setView(t.dataset.view)));
  $("#btn-search").onclick = doSearch;
  $("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  bindBlobSearch();
  $("#btn-clear").onclick = refreshSearch;
  $("#btn-back").onclick = histBack;
  $("#top-k").value = String(state.topK);
  /* 选项行：分段控件（类型 / 方式）+ 圆点开关（按文件） */
  const savedMode = localStorage.getItem("prism.searchMode") || "mixed";
  segSet("seg-mode", savedMode);
  state.searchMode = savedMode;
  segBind("seg-mode", (v) => {
    state.searchMode = v;
    localStorage.setItem("prism.searchMode", v);
  });
  segBind("seg-modality");
  const savedGroup = localStorage.getItem("prism.group") !== "0";   // 默认开
  $("#toggle-group").classList.toggle("on", savedGroup);
  $("#toggle-group").onclick = () => {
    const el = $("#toggle-group");
    el.classList.toggle("on");
    localStorage.setItem("prism.group", el.classList.contains("on") ? "1" : "0");
  };
  /* 主题：跟随系统 → 日间 → 夜间 循环 */
  applyTheme();
  $("#btn-theme").onclick = () => {
    const order = ["system", "light", "night"];
    state.theme = order[(order.indexOf(state.theme || "system") + 1) % order.length];
    localStorage.setItem(THEME_KEY, state.theme);
    applyTheme();
    toast(`主题：${THEME_CN[state.theme]}`);
  };
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)")
      .addEventListener?.("change", () => { if ((state.theme || "system") === "system") applyTheme(); });
  }
  $("#btn-new-lib").onclick = newLibrary;
  $("#btn-virtual").onclick = () => openVirtualEditor();
  $("#btn-settings").onclick = openSettings;
  /* 近似重复：按钮 + 阈值 + 开关（之前只绑了标签页，按钮是死的） */
  $("#btn-dupes").onclick = () => runDupes();
  $("#dup-threshold").onchange = () => runDupes();
  $("#dup-same-file").onchange = () => runDupes();
  $("#btn-mode-list").onclick = () => setBrowseMode("list");
  $("#btn-mode-grid").onclick = () => setBrowseMode("grid");
  segBind("seg-browse-modality", () => { state.browsePage = 1; loadBrowse(); });
  setBrowseMode(state.browseMode);   // 恢复上次的显示方式
  $("#btn-pick").onclick = () => pickPaths("files");
  $("#btn-pick-dir").onclick = () => pickPaths("dir");
  setupFormatPanel();          // 导入格式筛选面板（快捷分组 + 自定义后缀 + 已选清单）
  $("#btn-rescan").onclick = async () => {
    if (!state.currentLib) return toast("先选库");
    const r = await api(`/api/libraries/${state.currentLib}/rescan`, { method: "POST" });
    toast(`检查完成：缺失 ${r.missing.length} 个`);
    await loadBrowse(); await loadLibraries();
  };
  $("#drawer-close").onclick = () => { pauseAllAudio(); $("#drawer").classList.add("hidden"); };
  $("#text-modal-close").onclick = () => { pauseAllAudio(); $("#text-modal").classList.add("hidden"); };
  $("#text-modal").onclick = (e) => {
    if (e.target.id === "text-modal") { pauseAllAudio(); $("#text-modal").classList.add("hidden"); }
  };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      pauseAllAudio();
      $("#text-modal").classList.add("hidden");
      $("#drawer").classList.add("hidden");
      return;
    }
    /* Ctrl+Z 返回 / Ctrl+Shift+Z 前进。
       焦点在输入框里时不接管 —— 那儿保留系统原生的文字撤销。 */
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "z" || e.key === "Z")) {
      const t = e.target;
      const typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable);
      if (typing) return;                        // 让输入框自己撤销文字
      e.preventDefault();
      if (e.shiftKey) histForward(); else histBack();
    }
  });

  await refreshHealth();
  await loadLibraries();
  if (state.libs.length) await selectLibrary(state.libs[0].id);
  setView("search");
  /* 历史从「打开」这一态开始：hist[0] = 初始态，此时「返回」是灰的 */
  state.hist = [];
  state.histIdx = -1;
  histPush("打开");
  setInterval(refreshHealth, 30000);
}

boot();
