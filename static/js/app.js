// ============================================================================
// AgentGate 前端公共脚本
// ============================================================================

// ---- 图标（内联 SVG，跟随文字颜色） -----------------------------------------
const ICON = {
  folder:
    '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1.8 4.2A1.2 1.2 0 0 1 3 3h3.1l1.4 1.5H13a1.2 1.2 0 0 1 1.2 1.2v5.1A1.2 1.2 0 0 1 13 12H3a1.2 1.2 0 0 1-1.2-1.2z" stroke-linejoin="round"/></svg>',
  grid:
    '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5" rx="1.2"/><rect x="9" y="2" width="5" height="5" rx="1.2"/><rect x="2" y="9" width="5" height="5" rx="1.2"/><rect x="9" y="9" width="5" height="5" rx="1.2"/></svg>',
  gear:
    '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.6v1.8M8 12.6v1.8M1.6 8h1.8M12.6 8h1.8M3.6 3.6l1.3 1.3M11.1 11.1l1.3 1.3M12.4 3.6l-1.3 1.3M4.9 11.1l-1.3 1.3" stroke-linecap="round"/></svg>',
  plus:
    '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M8 3.5v9M3.5 8h9"/></svg>',
  power:
    '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M6 2.6H3.4A1.4 1.4 0 0 0 2 4v8a1.4 1.4 0 0 0 1.4 1.4H6"/><path d="M10.4 11.2 13.6 8l-3.2-3.2M13.4 8H6.4"/></svg>',
};

// ---- API 请求 ---------------------------------------------------------------
const API = (path, opts = {}) => {
  const token = localStorage.getItem("ag_token");
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  return fetch(path, Object.assign({}, opts, { headers })).then(async (r) => {
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) {
      const msg = (data && data.detail) || (data && data.error && data.error.message) || ("HTTP " + r.status);
      const err = new Error(msg);
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  });
};

// ---- 鉴权 -------------------------------------------------------------------
function requireAuth() {
  if (!localStorage.getItem("ag_token")) { location.href = "/"; return false; }
  return true;
}

async function loadMe() {
  try { return await API("/api/auth/me"); }
  catch { localStorage.removeItem("ag_token"); location.href = "/"; return null; }
}

function goLogin() {
  localStorage.removeItem("ag_token");
  location.href = "/";
}

// ---- 工具组缓存（侧边栏用） --------------------------------------------------
let _groupsCache = null;
async function loadGroups(force) {
  if (_groupsCache && !force) return _groupsCache;
  try { _groupsCache = await API("/api/toolgroups"); }
  catch { _groupsCache = []; }
  return _groupsCache;
}

async function newToolGroup() {
  const name = (prompt("请输入新工具组名称：") || "").trim();
  if (!name) return;
  try {
    const g = await API("/api/toolgroups", { method: "POST", body: JSON.stringify({ name }) });
    toast("工具组已创建");
    _groupsCache = null;
    location.href = "/toolgroup?id=" + g.id;
  } catch (e) { toast(e.message, "err"); }
}

// ---- 布局骨架：侧边栏 + 主区 -------------------------------------------------
/**
 * @param me     当前用户 {username, is_admin}
 * @param groups 工具组列表
 * @param opts   {active, activeGroup, title, body, actions}
 */
function shell(me, groups, opts) {
  opts = opts || {};
  groups = groups || [];

  const groupItems = groups.length
    ? groups.map(g => `
        <a class="side-item ${opts.activeGroup === g.id ? "active" : ""}" href="/toolgroup?id=${encodeURIComponent(g.id)}" title="${escapeHtml(g.name)}">
          <span class="ico">${ICON.folder}</span>
          <span class="txt">${escapeHtml(g.name)}</span>
        </a>`).join("")
    : `<div class="side-empty">暂无工具组，点上方按钮创建</div>`;

  const adminItem = me.is_admin
    ? `<a class="side-item ${opts.active === "admin" ? "active" : ""}" href="/admin">
         <span class="ico">${ICON.gear}</span><span class="txt">管理</span>
       </a>`
    : "";

  return `
  <div class="app">
    <aside class="sidebar" id="sidebar">
      <div class="side-brand"><span class="logo">A</span><span>AgentGate</span></div>
      <button class="side-new" onclick="newToolGroup()"><span style="display:inline-flex">${ICON.plus}</span>新建工具组</button>
      <nav class="side-nav">
        <div class="side-label"><span>导航</span></div>
        <a class="side-item ${opts.active === "dashboard" ? "active" : ""}" href="/dashboard">
          <span class="ico">${ICON.grid}</span><span class="txt">仪表盘</span>
        </a>
        ${adminItem}
        <div class="side-label"><span>工具组</span><span class="cnt">${groups.length}</span></div>
        ${groupItems}
      </nav>
      <div class="side-foot">
        <span class="avatar">${escapeHtml((me.username || "?")[0].toUpperCase())}</span>
        <span class="uname">${escapeHtml(me.username)}${me.is_admin ? ' <span class="badge ok">管理员</span>' : ""}</span>
        <span class="spacer"></span>
        <button class="icon-btn" title="退出登录" onclick="goLogin()">${ICON.power}</button>
      </div>
    </aside>
    <main class="main">
      <header class="main-top">
        <h1>${opts.title || ""}</h1>
        <span class="spacer"></span>
        ${opts.actions || ""}
      </header>
      <div class="main-body">${opts.body || ""}</div>
    </main>
  </div>`;
}

// 渲染整页
function mount(me, groups, opts) {
  document.getElementById("root").innerHTML = shell(me, groups, opts);
}

// ---- 工具函数 ---------------------------------------------------------------
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(t) {
  if (!t) return "—";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

let _toastEl = null;
function toast(msg, kind = "ok", ms = 2800) {
  if (_toastEl) _toastEl.remove();
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  document.body.appendChild(el);
  _toastEl = el;
  setTimeout(() => { if (el === _toastEl) { el.remove(); _toastEl = null; } }, ms);
}

function copyText(text) {
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast("已复制"), () => toast("复制失败", "err"));
  else toast("浏览器不支持复制", "err");
}

// 代码块（带标题栏 + 复制按钮）
function codeBlock(lang, code, id) {
  const cid = id || ("cb" + Math.random().toString(36).slice(2, 8));
  window._codeStore = window._codeStore || {};
  window._codeStore[cid] = code;
  return `<div class="codeblock">
    <div class="cb-head"><span>${escapeHtml(lang || "code")}</span><span class="spacer"></span>
      <button onclick="copyText(window._codeStore['${cid}'])">复制</button></div>
    <pre id="${cid}">${escapeHtml(code)}</pre>
  </div>`;
}

// ---- 弹窗 -------------------------------------------------------------------
let _modalEl = null;

function showModal(title, bodyHtml) {
  closeModal();
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.onclick = (e) => { if (e.target === mask) closeModal(); };
  mask.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <h3>${escapeHtml(title)}</h3><span class="spacer"></span>
        <button class="icon-btn" title="关闭" onclick="closeModal()">✕</button>
      </div>
      <div class="modal-body">${bodyHtml}</div>
    </div>`;
  document.body.appendChild(mask);
  _modalEl = mask;
}

function closeModal() {
  if (_modalEl) { _modalEl.remove(); _modalEl = null; }
}

document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
