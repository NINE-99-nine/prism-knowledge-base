/* ==========================================================================
   Prism · 棱镜 — 前端逻辑（原生 JS，零框架）
   ========================================================================== */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  libs: [],
  currentLib: null,
  view: "search",
  jobTimer: null,
  topK: Number(localStorage.getItem("prism.topK") ?? 24),        // 只影响界面，接口默认值不变
  browseMode: localStorage.getItem("prism.browseMode") || "list", // list | grid
  browsePer: Number(localStorage.getItem("prism.browsePer") || 100),
  browsePage: 1,
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
  clearResults();                // 检索结果清掉（输入框的文字保留）
  await loadLibraries();
  if (state.view === "browse") await loadBrowse();
  if (state.view === "dupes") await runDupes();
}

/* 清空检索结果，不动输入框 */
function clearResults() {
  const box = $("#results");
  if (box) box.innerHTML = "";
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
        modality: $("#modality").value || null,
      }),
    });
    renderFileQueryBanner(file, d);
    if (!d.results.length) {
      $("#results").innerHTML = `<div class="empty">没有找到相似的内容。</div>`;
      return;
    }
    renderCards(d.results);
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
  const mode = $("#search-mode").value || "mixed";
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
        modality: $("#modality").value || null,
        group: $("#group-mode").checked,
      }),
    });
    if (!d.results.length) {
      box.innerHTML = `<div class="empty">没有命中。<br>
        <span style="font-size:0.72rem">当前是「${MODE_CN[mode] || mode}」模式${mode !== "semantic"
          ? "" : "，它按意思找 —— 搜确切名字可以切到「关键词」或「混合」"}。</span></div>`;
      return;
    }
    renderCards(d.results);
  } catch (e) {
    box.innerHTML = `<div class="empty">检索失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderCards(items) {
  const box = $("#results");
  box.innerHTML = "";
  for (const it of items) {
    const card = document.createElement("div");
    card.className = "card" + (it.grouped ? " grouped" : "");
    const score = (it.score * 100).toFixed(1) + "%";
    let media = "";
    if (it.modality === "image") {
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

    card.innerHTML = `
      <span class="score">${score}</span>
      ${media}
      <div class="name">${escapeHtml(baseName(it.path))}${!it.grouped && it.chunk_index ? ` · 段 ${it.chunk_index + 1}` : ""}</div>
      <div class="path" title="${escapeHtml(it.path)}">${escapeHtml(it.path)}</div>
      ${hitsHtml}
      ${tagHtml ? `<div class="tags">${tagHtml}</div>` : ""}
      <div class="card-actions">
        ${it.modality === "text" ? `<button class="btn-mini js-full">查看全文</button>` : ""}
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
        api("/api/reveal", { method: "POST", body: JSON.stringify({ path: it.path }) })
          .catch((err) => toast("无法打开：" + err.message));
      };
    }
    card.onclick = (e) => {
      if (e.target.tagName === "AUDIO") return;
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
    const m = $("#browse-modality").value || "";
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
          <div class="rname">${escapeHtml(displayName(f.path))}</div>
          ${f.preview ? `<div class="rpreview">${escapeHtml(f.preview.replace(/\s+/g, " ").slice(0, 260))}</div>` : ""}
          <div class="rpath" title="${escapeHtml(f.path)}">${escapeHtml(shortPath(f.path))}</div>
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
function gridCell(f, i) {
  let media;
  if (f.modality === "video") {
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
    <div class="cell ${f.status === "missing" ? "missing" : ""}" data-i="${i}" title="${escapeHtml(f.path)}">
      <div class="cell-media">${media}</div>
      <div class="cell-name" title="${escapeHtml(f.path)}">${escapeHtml(displayName(f.path))}</div>
      <div class="cell-meta">${MODALITY_CN[f.modality] || f.modality}${f.chunks > 1 ? " · " + f.chunks + " 段" : ""} · ${fmtSize(f.size)}</div>
      <div class="cell-acts">
        <button class="btn-mini js-expand" title="看段落">段</button>
        ${f.modality === "text" ? `<button class="btn-mini js-fulltext" title="看完整原文">全文</button>` : ""}
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

    row.onclick = () => openDrawer(f.id);
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

async function runDupes() {
  const list = $("#dupes-list");
  if (!state.currentLib) { list.innerHTML = `<div class="empty">先在左侧选一个库</div>`; return; }
  const th = parseFloat($("#dup-threshold")?.value || "0.97");
  const sameFile = !!$("#dup-same-file")?.checked;
  list.innerHTML = `<div class="empty">检测中…</div>`;
  try {
    const d = await api("/api/duplicates", {
      method: "POST",
      body: JSON.stringify({ library_id: state.currentLib, threshold: th,
                             limit: 100, same_file: sameFile }),
    });
    if (!d.pairs.length) {
      list.innerHTML = `<div class="empty">没有发现相似度 ≥ ${th} 的配对<br>
        <span style="font-size:0.7rem">（同一个文件的不同段落默认不参与比较）</span></div>`;
      return;
    }
    list.innerHTML = `<div class="dup-hint">共 ${d.count} 对 · 点击任意一行并排查看两个文件</div>` +
      d.pairs.map((p, i) => `
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
      el.onclick = () => showDup(d.pairs[+el.dataset.dup]);
    });
  } catch (e) {
    list.innerHTML = `<div class="empty">检测失败：${escapeHtml(e.message)}</div>`;
  }
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

async function pickPaths(mode) {
  if (!state.currentLib) return toast("先在左侧选一个库");
  const kind = mode === "files" ? ($("#pick-kind").value || "") : "";
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
      body: JSON.stringify({ library_id: state.currentLib, paths, recursive: true }),
    });
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
      <div class="kv"><b>标签</b><span>${tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join(" ") || "—"}</span></div>
      <div class="tag-input">
        <input id="tag-input" placeholder="加标签后回车">
      </div>
      <div class="actions">
        <button class="btn-ghost" id="act-reveal">在资源管理器中显示</button>
        <button class="btn-ghost" id="act-move">移到别的库…</button>
        <button class="btn-ghost" id="act-similar">找相似的</button>
        <button class="btn-ghost" id="act-delete" style="color:var(--warn)">从本库移除</button>
      </div>`;

    body.querySelector("#tag-input").onkeydown = async (e) => {
      if (e.key !== "Enter") return;
      const tag = e.target.value.trim();
      if (!tag) return;
      await api(`/api/items/${itemId}/tags`, { method: "POST", body: JSON.stringify({ tag }) });
      openDrawer(itemId);
    };
    body.querySelector("#act-move").onclick = () => moveDialog([itemId]);
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
      renderCards(r.results.filter((x) => x.id !== itemId));
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
  modal({
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

function setupDrop() {
  const dz = $("#dropzone");
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("hot"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("hot"); }));

  dz.addEventListener("drop", (e) => {
    const files = Array.from(e.dataTransfer?.files || []);
    if (!files.length) return;
    // 浏览器不给绝对路径 → 引导用原生选择框（同样只差一次点击）
    toast("浏览器拿不到拖入文件的真实路径，已为你打开选择框", 4000);
    if (files.length === 1 && !files[0].type) {
      // 拖的是文件夹
      pickPaths("dir");
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
  $("#btn-clear").onclick = clearResults;
  $("#top-k").value = String(state.topK);
  /* 检索模式：默认「混合」，记住上次的选择（下次打开保持一致） */
  const savedMode = localStorage.getItem("prism.searchMode") || "mixed";
  $("#search-mode").value = savedMode;
  state.searchMode = savedMode;
  $("#search-mode").onchange = () => {
    state.searchMode = $("#search-mode").value;
    localStorage.setItem("prism.searchMode", state.searchMode);
  };
  $("#btn-new-lib").onclick = newLibrary;
  $("#btn-settings").onclick = openSettings;
  /* 近似重复：按钮 + 阈值 + 开关（之前只绑了标签页，按钮是死的） */
  $("#btn-dupes").onclick = () => runDupes();
  $("#dup-threshold").onchange = () => runDupes();
  $("#dup-same-file").onchange = () => runDupes();
  $("#btn-mode-list").onclick = () => setBrowseMode("list");
  $("#btn-mode-grid").onclick = () => setBrowseMode("grid");
  $("#browse-modality").addEventListener("change", () => { state.browsePage = 1; loadBrowse(); });
  setBrowseMode(state.browseMode);   // 恢复上次的显示方式
  $("#btn-pick").onclick = () => pickPaths("files");
  $("#btn-pick-dir").onclick = () => pickPaths("dir");
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
    }
  });

  await refreshHealth();
  await loadLibraries();
  if (state.libs.length) await selectLibrary(state.libs[0].id);
  setView("search");
  setInterval(refreshHealth, 30000);
}

boot();
