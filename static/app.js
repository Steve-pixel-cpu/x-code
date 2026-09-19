"use strict";
/* ============================================================
 * 入口守卫: 网页入口已关闭, 仅允许 x-code 桌面壳打开
 * （桌面壳的 preload 会注入 window.xcodeDesktop 标记;
 *   浏览器直接访问 127.0.0.1:8000 只会看到提示, 应用不初始化）
 * ============================================================ */
if (!window.xcodeDesktop) {
  document.documentElement.innerHTML =
    '<head><meta charset="UTF-8"><title>x-code</title></head>' +
    '<body style="margin:0;background:#101014">' +
    '<div style="height:100vh;display:flex;flex-direction:column;gap:10px;' +
    'align-items:center;justify-content:center;font-family:system-ui,' +
    '"Microsoft YaHei",sans-serif;color:#a0a1ab;font-size:15px">' +
    '<img src="/static/icon.png" alt="" style="width:56px;height:56px;' +
    'border-radius:14px;object-fit:cover">' +
    "<div>请通过 x-code 桌面应用打开</div></div></body>";
  throw new Error("x-code: 网页入口已关闭, 请使用桌面应用");
}
/* ============================================================
 * 状态
 * ============================================================ */
const $ = id => document.getElementById(id);
const state = {
  sessionId: null,          // 当前"可见"的会话 id（null = 草稿/无）
  sessions: [],             // 全量会话列表（loadSessions 填充）
  sideTab: "project",       // 侧栏列表模式: project | group
  draft: false,             // 草稿态: 已点"新建"但还没发首条消息（不建条目）
  draftInput: "",           // 草稿态未发送的输入（跟随会话切换）
  draftDir: null,           // 草稿态预选的项目目录（侧栏项目行 + 进入时带上）
  serverWorkspace: null,    // 服务进程工作区名（无会话目录时的兜底展示）
  runs: {},                 // sessionId → 运行态（多会话并行: 各自 WS/流式指针/审批）
};

/* 一个会话的运行态。多会话并行的核心: 每个会话有自己的 WebSocket、
   流式 DOM 指针、权限审批、未读数。切换会话只是换"可见"的 id,
   后台会话的 WS 与轮次照常跑; 回到前台时按需重拉历史对齐。 */
function runOf(id) {
  if (!state.runs[id]) {
    state.runs[id] = {
      ws: null,               // 该会话的 WebSocket
      busy: false,            // 一轮对话进行中（含排队等待槽位）
      queued: false,          // 在全局并发队列中等待槽位
      pendingPerm: null,      // 待审批的 permission_request
      activeToolCard: null,   // 当前流式中的工具卡片（配对 tool_result）
      liveToolCards: {},      // 流式中全部待完成工具卡: tool_use id → 卡片（防乱序/丢事件漏配）
      curBubble: null,        // 当前流式中的正文气泡
      curThinking: null,      // 当前流式中的思考行 { el, t0 }
      toolResultIndex: {},    // 历史回放: tool_use id → 卡片（等结果块配对）
      reconnectTimer: null,   // WS 断线重连定时器
      reconnectAttempts: 0,   // 连续重连次数（成功后归零）
      currentWorkdir: null,   // 该会话的工作目录（messages 接口返回）
      pendingSends: [],       // WS 建立期间待发的消息（onopen 后冲刷）
      queue: [],              // 待发送消息（本轮进行中追加, 停在输入框上方卡片）
      unread: 0,              // 后台完成/待审批的未读计数
      loaded: false,          // 历史是否已加载过（首次切入必拉）
      loading: false,         // 历史加载进行中（防并发重复拉取）
      everConnected: false,   // 该会话 WS 是否成功连过（区分首次连接与断线重连）
      awaiting: false,        // 忙碌中且正处于等待模型输出的空窗（await_output 起止）
    };
  }
  return state.runs[id];
}
const curRun = () => (state.sessionId ? runOf(state.sessionId) : null);

/* 手动添加的项目 / 项目折叠状态: localStorage 持久化 */
state.customProjects = JSON.parse(localStorage.getItem("xc-projects") || "[]");
state.collapsedProjects = new Set(JSON.parse(localStorage.getItem("xc-collapsed") || "[]"));
state.draftInput = "";   // 草稿态未发送的输入

/* 输入框内容跟随会话: 切走前保存, 切回后恢复 */
function saveCurrentInput() {
  const v = $("input").value;
  if (state.draft) state.draftInput = v;
  else if (state.sessionId) runOf(state.sessionId).inputDraft = v;
}
function restoreCurrentInput() {
  const v = state.draft ? state.draftInput
    : (state.sessionId ? runOf(state.sessionId).inputDraft : "");
  $("input").value = v || "";
  autoGrow($("input"));
  updateSendBtn();
  renderQueueCards();   // 待发送卡片跟着会话走
}

/* ============================================================
 * 会话消息列: 每个会话一个常驻 DOM 列, 切换会话只做显隐。
 * 后台会话的流式事件继续写进自己的隐藏列, 切回原样恢复——
 * 不再"回前台重拉历史", 流式进行中的回复也不丢字、不冻结。
 * ============================================================ */
let pinnedCol = null;   // 历史回放期间固定写入列（JS 单线程, 用完置回）
let pinnedRun = null;   // 回放期间 toolResultIndex 写入的目标 run

function colOf(id) {
  let col = document.getElementById("msg-col-" + id);
  if (!col) {
    col = document.createElement("div");
    col.id = "msg-col-" + id;
    col.className = "msg-col";
    $("messages").appendChild(col);
  }
  return col;
}
function showCol(id) {
  document.querySelectorAll("#messages .msg-col").forEach(c => {
    c.classList.toggle("on", c.id === "msg-col-" + id);
  });
}

/* ============================================================
 * 主题: dark | light | system（跟随系统），localStorage 持久化
 * ============================================================ */
const THEME_KEY = "xc-theme";
const mqDark = window.matchMedia("(prefers-color-scheme: dark)");
function themePref() { return localStorage.getItem(THEME_KEY) || "system"; }
function resolvedTheme() {
  const pref = themePref();
  return pref === "system" ? (mqDark.matches ? "dark" : "light") : pref;
}
function applyTheme() {
  document.documentElement.dataset.theme = resolvedTheme();
}
// 跟随系统时, 系统深浅切换实时生效
mqDark.addEventListener("change", () => {
  if (themePref() === "system") applyTheme();
});
applyTheme();

/* ============================================================
 * markdown 渲染: marked.js 优先，加载失败降级为转义纯文本;
 * 代码块外加语言标签/复制按钮，hljs 可用时做语法高亮
 * ============================================================ */
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}
function renderMd(text) {
  if (window.marked && window.marked.parse) {
    try {
      let src = text;
      // 1) 保护代码段（围栏/行内）: 公式解析不碰 `$` 出现在代码里的情况
      const codeSlots = [];
      src = src.replace(/```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`/g, m => {
        codeSlots.push(m);
        return "\u0000CODE" + (codeSlots.length - 1) + "\u0000";
      });
      // 2) 抽出公式（$$...$$ 块级 / $...$ 行内）为占位符, 免遭 markdown 转义
      const mathSlots = [];
      if (src.includes("$")) {
        src = src.replace(/\$\$([\s\S]+?)\$\$/g, (_, tex) => {
          mathSlots.push({ tex: tex.trim(), display: true });
          return "\u0000MATH" + (mathSlots.length - 1) + "\u0000";
        });
        src = src.replace(/\$([^\s$][^$\n]*?)\$/g, (_, tex) => {
          mathSlots.push({ tex: tex.trim(), display: false });
          return "\u0000MATH" + (mathSlots.length - 1) + "\u0000";
        });
      }
      // 3) 还原代码段, 交给 marked 正常解析
      src = src.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => codeSlots[+i]);
      let html = window.marked.parse(src, { breaks: true, gfm: true });
      // 4) 解析完成后用 KaTeX 还原公式（KaTeX 未加载/解析失败则回退原文）
      html = html.replace(/\u0000MATH(\d+)\u0000/g, (_, i) => {
        const slot = mathSlots[+i];
        if (!window.katex) return "$" + slot.tex + "$";
        try {
          return window.katex.renderToString(slot.tex, {
            displayMode: slot.display, throwOnError: false,
          });
        } catch (e) {
          return "$" + slot.tex + "$";
        }
      });
      return html;
    } catch (e) { /* 落到降级 */ }
  }
  return "<p>" + escapeHtml(text).replace(/\n/g, "<br>") + "</p>";
}

const COPY_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>';
const CHECK_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 12.5l5 5 10-11"/></svg>';

function decorateCode(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll("pre:not([data-dec])").forEach(pre => {
    pre.setAttribute("data-dec", "1");
    const code = pre.querySelector("code");
    let lang = "";
    if (code) {
      const m = /language-([\w+-]+)/.exec(code.className || "");
      if (m) lang = m[1];
      if (window.hljs && pre.textContent.length < 60000) {
        try { window.hljs.highlightElement(code); } catch (e) { /* 高亮失败静默 */ }
      }
    }
    const wrap = document.createElement("div");
    wrap.className = "code-wrap";
    const head = document.createElement("div");
    head.className = "code-head";
    head.innerHTML =
      '<span class="code-lang"></span>' +
      '<button class="code-copy" type="button">' + COPY_SVG + '<span>复制</span></button>';
    head.querySelector(".code-lang").textContent = lang || "代码";
    pre.replaceWith(wrap);
    wrap.appendChild(head);
    wrap.appendChild(pre);
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e2) { /* ignore */ }
    ta.remove();
    return ok;
  }
}

/* 复制按钮事件委托（流式重渲染会让节点反复重建，不能逐个绑） */
document.addEventListener("click", ev => {
  const btn = ev.target.closest(".code-copy");
  if (!btn) return;
  const wrap = btn.closest(".code-wrap");
  const pre = wrap && wrap.querySelector("pre");
  if (!pre) return;
  copyText(pre.innerText).then(ok => {
    if (!ok) { toast("复制失败"); return; }
    btn.classList.add("copied");
    btn.innerHTML = CHECK_SVG + "<span>已复制</span>";
    setTimeout(() => {
      btn.classList.remove("copied");
      btn.innerHTML = COPY_SVG + "<span>复制</span>";
    }, 1400);
  });
});

/* ============================================================
 * 轻提示
 * ============================================================ */
let toastTimer = null;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 1600);
}

/* ============================================================
 * 滚动: 贴底自动跟随; 用户上翻时不拽人, 悬浮钮一键回底
 * ============================================================ */
let nearBottom = true;
$("messages").addEventListener("scroll", () => {
  const el = $("messages");
  nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  $("scroll-btn").classList.toggle("show", !nearBottom);
});
$("scroll-btn").onclick = () => {
  nearBottom = true;
  $("messages").scrollTo({ top: $("messages").scrollHeight, behavior: "smooth" });
};
function scrollToBottom(force) {
  if (!force && !nearBottom) return;
  const el = $("messages");
  el.scrollTop = el.scrollHeight;
}

/* ============================================================
 * 侧栏会话列表（项目 / 分组 两种模式 + 搜索过滤）
 * ============================================================ */
function sessionDate(id) {
  // 会话 id 是 %Y%m%d-%H%M%S 的 UTC 时间戳（可能有 "w" 后缀防撞），
  // 必须按 UTC 解析再转本地，否则所有会话都会显示成多出时区差的"旧"会话
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id);
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null;
}
function relativeTime(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id);
  if (!m) return "";
  const then = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
  const diff = (Date.now() - then.getTime()) / 1000;
  if (diff < 90) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + "分钟";
  if (diff < 86400) return Math.floor(diff / 3600) + "小时";
  if (diff < 172800) return "1天";
  if (diff < 604800) return Math.floor(diff / 86400) + "天";
  return `${+m[2]}/${+m[3]}`;
}
function sessionBucket(id) {
  const d = sessionDate(id);
  if (!d) return 3;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - d) / 86400000);
  if (days <= 0) return 0;      // 今天
  if (days === 1) return 1;     // 昨天
  if (days < 7) return 2;       // 7 天内
  return 3;                     // 更早
}
const BUCKET_LABELS = ["今天", "昨天", "7 天内", "更早"];
const FOLDER_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/></svg>';
const PLUS_SMALL_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';
const UNTITLED_TITLE = "未命名任务";

function displayTitle(s) {
  return (!s || s.title === "(未命名)") ? UNTITLED_TITLE : s.title;
}

async function loadSessions() {
  try {
    const r = await fetch("/api/sessions");
    const data = await r.json();
    state.sessions = data.sessions;
    renderSessionList();
  } catch (e) { console.error("加载会话列表失败", e); }
}

const TRASH_SMALL_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9.5 7V5h5v2M6.5 7l1 13h9l1-13"/></svg>';
const PENCIL_SMALL_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20l4.5-1L20 7.5 16.5 4 5 15.5 4 20z"/></svg>';
/* 会话状态图标: 运行中转圈 / 未读红点 / 空闲对话气泡 */
const SPINNER_SVG = '<svg class="s-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M21 12a9 9 0 11-9-9"/></svg>';
const CHAT_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M21 11.5a8.5 8.5 0 01-8.5 8.5c-1.6 0-3.1-.4-4.3-1.2L3 20l1.2-5.2A8.5 8.5 0 1121 11.5z"/></svg>';
const DOT_RED_SVG = '<svg width="9" height="9" viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="currentColor"/></svg>';
function sessionStateIcon(s) {
  const run = state.runs[s.id];
  if (run && run.busy) return SPINNER_SVG;               // 运行中
  if (run && run.unread > 0) return DOT_RED_SVG;         // 有未读
  return CHAT_SVG;                                        // 空闲
}

async function renameSession(id, title) {
  try {
    const r = await fetch(`/api/sessions/${id}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || r.status);
    }
    const s = state.sessions.find(x => x.id === id);
    if (s) s.title = title;
    renderSessionList();
    if (id === state.sessionId) refreshDocTitle();
  } catch (e) {
    toast("重命名失败: " + e.message);
  }
}

/* 通用行内编辑框: Enter/失焦提交，Esc 取消；onDone 无论提交与否都会回调 */
function makeInlineEditor(initial, onCommit, onDone) {
  const input = document.createElement("input");
  input.className = "inline-rename";
  input.maxLength = 60;
  input.value = initial;
  const finish = commit => {
    if (input._done) return;
    input._done = true;
    const val = input.value.trim();
    input.remove();
    if (commit && val && val !== initial) onCommit(val);
    if (onDone) onDone(commit);
  };
  input.onclick = ev => ev.stopPropagation();
  input.onkeydown = ev => {
    ev.stopPropagation();   // 别让按键冒泡成全局快捷键
    if (ev.key === "Enter") finish(true);
    else if (ev.key === "Escape") finish(false);
  };
  input.onblur = () => finish(true);
  requestAnimationFrame(() => { input.focus(); input.select(); });
  return input;
}

function makeSessionItem(s) {
  const item = document.createElement("div");
  const unnamed = s.title === "(未命名)";
  item.className = "session-item"
    + (s.id === state.sessionId ? " active" : "")
    + (unnamed ? " unnamed" : "");
  item.dataset.id = s.id;
  item.innerHTML = '<span class="s-ico">' + sessionStateIcon(s) + '</span>'
    + '<span class="s-unread"></span>'
    + '<span class="title"></span><span class="meta"></span>'
    + '<button class="s-ren" data-tip="重命名">' + PENCIL_SMALL_SVG + '</button>'
    + '<button class="s-del" data-tip="删除会话">' + TRASH_SMALL_SVG + '</button>';
  item.querySelector(".title").textContent = displayTitle(s);
  item.querySelector(".meta").textContent = relativeTime(s.id);
  const run = state.runs[s.id];
  if (run && run.busy) item.classList.add("running");
  if (run && run.unread > 0) {
    item.classList.add("unread");
    item.querySelector(".s-unread").textContent = run.unread > 99 ? "99+" : run.unread;
  }
  item.onclick = () => selectSession(s.id);
  item.querySelector(".s-ren").onclick = ev => {
    ev.stopPropagation();
    if (item.classList.contains("renaming")) return;
    item.classList.add("renaming");   // 编辑期间隐藏标题/时间/按钮
    const input = makeInlineEditor(
      displayTitle(s),
      val => renameSession(s.id, val),
      () => item.classList.remove("renaming"),
    );
    item.appendChild(input);
  };
  item.querySelector(".s-del").onclick = ev => {
    ev.stopPropagation();
    deleteSession(s.id);
  };
  return item;
}

async function deleteSession(id) {
  const run = state.runs[id];
  if (run && run.busy) {
    toast("该会话本轮对话进行中，暂不能删除");
    return;
  }
  const cur = state.sessions.find(s => s.id === id);
  if (!confirm(`删除会话「${displayTitle(cur)}」？删除后不可恢复。`)) return;
  try {
    const r = await fetch(`/api/sessions/${id}`, { method: "DELETE" });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || r.status);
    }
    state.sessions = state.sessions.filter(s => s.id !== id);
    closeWs(id);                 // 断掉该会话的 WS
    delete state.runs[id];       // 运行态一并清理
    const col = document.getElementById("msg-col-" + id);
    if (col) col.remove();       // 消息列一并清理
    renderSessionList();
    if (id === state.sessionId) {
      // 删的是当前会话: 自动切到最近的会话，没有则回草稿态
      state.sessionId = null;
      const next = state.sessions[0];
      if (next) await selectSession(next.id);
      else startDraft();
    }
    toast("会话已删除");
  } catch (e) {
    toast("删除失败: " + e.message);
  }
}

function renderSessionList() {
  const list = $("session-list");
  list.innerHTML = "";
  const q = ($("search-input").value || "").trim().toLowerCase();
  const sessions = state.sessions.filter(
    s => !q
      || (s.title || "").toLowerCase().includes(q)
      || (s.workdir || "").toLowerCase().includes(q));   // 项目路径/目录名也可搜
  const addLabel = text => {
    const l = document.createElement("div");
    l.className = "list-label";
    l.textContent = text;
    list.appendChild(l);
  };
  if (state.sideTab === "group") {
    if (!sessions.length) {
      list.innerHTML = '<div class="list-empty">' + (q ? "没有匹配的会话" : "还没有会话") + "</div>";
      return;
    }
    // 会话按时间倒序，分组标签自然按 今天→更早 顺序出现
    let bucket = -1;
    for (const s of sessions) {
      const b = sessionBucket(s.id);
      if (b !== bucket) { bucket = b; addLabel(BUCKET_LABELS[b]); }
      list.appendChild(makeSessionItem(s));
    }
    return;
  }

  /* ---- 项目视图: 可折叠项目组 + 任务(未选择文件夹的会话) ---- */
  const headRow = document.createElement("div");
  headRow.className = "list-label-row";
  headRow.innerHTML = '<span class="list-label">项目</span>';
  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "icon-btn";
  addBtn.id = "btn-add-project";
  addBtn.dataset.tip = "添加项目";
  addBtn.innerHTML = PLUS_SMALL_SVG;
  addBtn.onclick = async ev => {
    ev.stopPropagation();
    // 桌面端: 系统原生"选择文件夹"对话框; 浏览器/预览: 页面内目录选择兜底
    if (window.xcodePickFolder) {
      try {
        const dir = await window.xcodePickFolder();
        if (dir) addProject(dir);
      } catch (e) { /* 用户取消 */ }
      return;
    }
    openDirPop();
  };
  headRow.appendChild(addBtn);
  list.appendChild(headRow);

  if (!sessions.length && (q || !state.customProjects.length)) {
    const e = document.createElement("div");
    e.className = "list-empty";
    e.textContent = q ? "没有匹配的会话" : "还没有项目，点上方 + 添加";
    list.appendChild(e);
  }

  // 项目目录 = 会话的 workdir ∪ 手动添加的项目
  const groups = new Map();
  for (const s of sessions) {
    if (!s.workdir) continue;
    if (!groups.has(s.workdir)) groups.set(s.workdir, []);
    groups.get(s.workdir).push(s);
  }
  for (const wd of state.customProjects) {
    if (!groups.has(wd)) groups.set(wd, []);
  }
  const named = [...groups.entries()];
  named.sort((a, b) => (b[1][0]?.id ?? "").localeCompare(a[1][0]?.id ?? ""));  // 组间按最新会话
  const chev = '<svg class="p-chev" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
  for (const [wd, items] of named) {
    const collapsed = state.collapsedProjects.has(wd) && !q;   // 搜索时强制展开
    const header = document.createElement("div");
    header.className = "project-item" + (collapsed ? " collapsed" : "");
    header.innerHTML = FOLDER_SVG + '<span class="p-name"></span>'
      + '<span class="p-count"></span>'
      + '<button class="p-add" data-tip="在此项目新建任务">' + PLUS_SMALL_SVG + '</button>'
      + chev;
    header.querySelector(".p-name").textContent = projectDisplayName(wd);
    header.querySelector(".p-count").textContent = items.length ? String(items.length) : "";
    header.querySelector(".p-add").onclick = ev => {
      ev.stopPropagation();   // 别触发折叠/展开
      startDraft(wd);
    };
    header.onclick = () => toggleProject(wd);
    list.appendChild(header);
    if (collapsed) continue;
    for (const s of items) {
      const it = makeSessionItem(s);
      it.classList.add("in-project");
      list.appendChild(it);
    }
  }

  // 任务 = 没有选择文件夹的会话; 标题行带 + 与项目分组一致
  const taskRow = document.createElement("div");
  taskRow.className = "list-label-row";
  taskRow.innerHTML = '<span class="list-label">任务</span>';
  const taskAdd = document.createElement("button");
  taskAdd.type = "button";
  taskAdd.className = "icon-btn";
  taskAdd.dataset.tip = "新建任务";
  taskAdd.innerHTML = PLUS_SMALL_SVG;
  taskAdd.onclick = ev => { ev.stopPropagation(); startDraft(); };
  taskRow.appendChild(taskAdd);
  list.appendChild(taskRow);
  const loose = sessions.filter(s => !s.workdir);
  if (!loose.length) {
    const e = document.createElement("div");
    e.className = "list-empty";
    e.textContent = "还没有任务";
    list.appendChild(e);
  } else {
    for (const s of loose) list.appendChild(makeSessionItem(s));
  }
}

function toggleProject(wd) {
  state.collapsedProjects.has(wd)
    ? state.collapsedProjects.delete(wd)
    : state.collapsedProjects.add(wd);
  localStorage.setItem("xc-collapsed", JSON.stringify([...state.collapsedProjects]));
  renderSessionList();
}
function addProject(wd) {
  if (!state.customProjects.includes(wd)) {
    state.customProjects.push(wd);
    localStorage.setItem("xc-projects", JSON.stringify(state.customProjects));
  }
  renderSessionList();
  toast("已添加项目：" + dirName(wd));
}

/* 项目分组的组标题: 与顶栏 ws-tag 一致取目录名(末段), 空路径兜底 */
function projectDisplayName(wd) {
  return dirName(wd) || "未设置项目";
}

function markActiveSession() {
  document.querySelectorAll(".session-item").forEach(el => {
    el.classList.toggle("active", el.dataset.id === state.sessionId);
  });
}

function refreshDocTitle() {
  const cur = state.sessions.find(s => s.id === state.sessionId);
  $("doc-title").textContent = cur ? displayTitle(cur) : UNTITLED_TITLE;
  refreshWorkdirTag();
}

/* ============================================================
 * 工作目录（项目）: 草稿态选择 + 顶栏标签展示
 * ============================================================ */
function dirName(p) { return p ? p.split(/[\\/]/).filter(Boolean).pop() : null; }

function refreshWorkdirTag() {
  // 顶栏标签: 草稿 → 预选项目; 会话 → 其运行态里的工作目录。
  // 任务类会话（无工作目录）不显示标签——服务进程的目录名与会话无关
  const wd = state.draft ? state.draftDir
    : (curRun() ? curRun().currentWorkdir : null);
  const name = dirName(wd);
  $("ws-tag").style.display = name ? "" : "none";
  if (name) $("ws-tag-text").textContent = name;
}

const dirPop = $("dir-pop");
let dirBrowsePath = null;

async function browseDir(path) {
  const err = $("dir-err");
  err.classList.remove("show");
  try {
    const q = path ? "?path=" + encodeURIComponent(path) : "";
    const r = await fetch("/api/dirs" + q);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || r.status);
    }
    const data = await r.json();
    dirBrowsePath = data.path;
    $("dir-path-input").value = data.path;
    const list = $("dir-list");
    list.innerHTML = "";
    if (!data.dirs.length) {
      list.innerHTML = '<div class="dir-empty">此目录下没有子目录</div>';
      return;
    }
    for (const name of data.dirs) {
      const it = document.createElement("div");
      it.className = "dir-item";
      it.innerHTML = FOLDER_SVG + '<span></span>';
      it.querySelector("span").textContent = name;
      it.onclick = () => browseDir(dirBrowsePath.replace(/[\\/]+$/, "") + "\\" + name);
      list.appendChild(it);
    }
  } catch (e) {
    err.textContent = "无法读取: " + e.message;
    err.classList.add("show");
  }
}

function openDirPop(anchor = $("btn-add-project")) {
  const r = anchor.getBoundingClientRect();
  dirPop.classList.add("open");
  const left = Math.max(10, Math.min(r.left, window.innerWidth - 352));
  dirPop.style.left = left + "px";
  dirPop.style.top = "auto";
  dirPop.style.bottom = (window.innerHeight - r.top + 8) + "px";
  browseDir("");
}
function closeDirPop() { dirPop.classList.remove("open"); }

$("dir-up").onclick = () => {
  const parts = (dirBrowsePath || "").split(/[\\/]/).filter(Boolean);
  if (!parts.length) return;
  parts.pop();
  if (!parts.length) return;
  // 只剩盘符（如 "D:"）时补上根斜杠，避免 Path("D:") 落到盘符当前目录
  browseDir(parts.length === 1 && /^[a-zA-Z]:$/.test(parts[0]) ? parts[0] + "\\" : parts.join("\\"));
};
function goDirInput() { browseDir($("dir-path-input").value.trim()); }
$("dir-go").onclick = goDirInput;
$("dir-path-input").addEventListener("keydown", ev => {
  ev.stopPropagation();
  if (ev.key === "Enter") goDirInput();
});
$("dir-pick").onclick = () => {
  if (!dirBrowsePath) return;
  addProject(dirBrowsePath);
  if (state.draft) {   // 从欢迎页打开: 选中的目录同时作为草稿的工作区
    state.draftDir = dirBrowsePath;
    showEmptyState();
  }
  closeDirPop();
};
document.addEventListener("click", ev => {
  if (!dirPop.classList.contains("open")) return;
  const addBtn = $("btn-add-project");
  if (!dirPop.contains(ev.target)
      && !(addBtn && addBtn.contains(ev.target))) closeDirPop();
});

/* 顶栏标题点击重命名（Notion 式） */
$("doc-title").onclick = () => {
  if (state.draft || !state.sessionId) return;   // 草稿态没有可命名的会话
  if ($("doc-title").querySelector("input")) return;
  const cur = state.sessions.find(s => s.id === state.sessionId);
  if (!cur) return;
  const input = makeInlineEditor(
    displayTitle(cur),
    val => renameSession(state.sessionId, val),
    () => refreshDocTitle(),   // 取消/完成后恢复标题文本（提交后 rename 也会刷新）
  );
  $("doc-title").textContent = "";
  $("doc-title").appendChild(input);
};

/* ============================================================
 * 会话切换 / 新建 / 历史回放
 * ============================================================ */
/* 拉取并渲染会话历史。首次切入与断线重同步共用:
 * 重同步会替换整列 DOM, 旧的流式指针一并作废——
 * 断连窗口内丢掉的事件以服务端落盘的历史为准。 */
async function loadSessionHistory(id) {
  const run = runOf(id), col = colOf(id);
  run.loading = true;
  col.innerHTML = '<div class="empty-state"><h2>加载中…</h2></div>';
  run.toolResultIndex = {};
  try {
    const r = await fetch(`/api/sessions/${id}/messages`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    run.currentWorkdir = data.workdir || null;
    flushAssistantBubble(run);
    run.curBubble = null;
    run.curThinking = null;
    run.activeToolCard = null;
    run.liveToolCards = {};
    pinnedCol = col; pinnedRun = run;
    col.innerHTML = "";
    for (const m of data.messages) renderHistoryMessage(m);
    // 悬空 tool_use 收口: 轮次早已结束的, 结果永远来不了;
    // 仍在跑的轮次保留"运行中", 等活动流的 tool_result 按 id 配对闭合
    if (!run.busy) {
      col.querySelectorAll('.tool-row[data-state="run"]').forEach(el => setToolState(el, "stopped"));
    }
    pinnedCol = null; pinnedRun = null;
    run.loaded = true;
  } catch (e) {
    col.innerHTML = "";
    pinnedCol = col; pinnedRun = run;
    addNoteBubble("err", "加载历史失败: " + e.message);
    pinnedCol = null; pinnedRun = null;
  } finally {
    run.loading = false;
  }
}

async function selectSession(id) {
  saveCurrentInput();           // 切走前保存当前会话的未发送输入
  state.draft = false;
  state.sessionId = id;
  $("pane").classList.remove("empty-view");   // 真实会话: 输入卡回到常规底部布局
  renderDraftChrome();                        // 顺带清掉草稿态的工作区条/建议 chips
  const run = runOf(id);
  markActiveSession();
  refreshDocTitle();
  renderSessionList();          // 未读标识切换
  connectWs(id);                // 已有连接则复用; 旧会话的 WS 原样保留, 后台继续跑
  colOf(id);   // 确保该会话的消息列已创建（惰性建列）
  showCol(id);
  // 历史只在首次切入时拉一次; 之后切换不再重拉——流式 DOM 一直活着,
  // 后台轮次的内容就写在本列里, 切回即所见
  if (!run.loaded && !run.loading) await loadSessionHistory(id);
  run.unread = 0;               // 回到前台看完了, 未读清零
  renderSessionList();
  refreshWorkdirTag();
  setBusyUi(run.busy);
  syncThinkingIndicator();   // 切会话必须重算: 转圈只属于"正在等待输出的那个会话"
  setConn(run.ws && run.ws.readyState === 1 ? "on" : "", run.ws ? (run.ws.readyState === 1 ? "已连接" : "连接中…") : "未连接");
  // 有挂着的审批请求: 重新弹出
  if (id === state.sessionId && run.pendingPerm) onPermissionRequest(run.pendingPerm);
  restoreCurrentInput();        // 输入框恢复成该会话未发送的内容
  scrollToBottom(true);
}

const SUGGESTIONS = [
  "帮我看看这个项目的整体结构",
  "解释一下核心模块的实现思路",
  "跑一下全部测试并总结结果",
];

function showEmptyState() {
  $("pane").classList.add("empty-view");   // 欢迎态: 输入卡随欢迎内容垂直居中
  const col = msgCol();
  const h = new Date().getHours();
  const greet = h < 6 ? "夜深了" : h < 12 ? "上午好" : h < 14 ? "中午好" : h < 18 ? "下午好" : "晚上好";
  col.innerHTML =
    "<div class='empty-state welcome'>" +
    "<img class='watermark' src='" + iconUrl() + "' alt=''>" +
    "<h2>" + greet + "呀，有什么想让我帮忙的吗</h2>" +
    "</div>";
  renderDraftChrome();   // 工作区条/建议 chips 挂在输入卡上下, 不随消息列重绘
}

/* 草稿态: 工作区选择条贴输入卡顶部, 建议 chips 挂输入卡下方; 非草稿态清空 */
function renderDraftChrome() {
  const dock = $("ws-dock"), sug = $("sug-dock");
  if (!state.draft) {
    dock.innerHTML = "";
    sug.innerHTML = "";
    return;
  }
  dock.innerHTML = "<button class='ws-chip' id='ws-chip'></button>";
  renderWsChip();
  sug.innerHTML = SUGGESTIONS.map(s => `<button class='sug-chip'>${escapeHtml(s)}</button>`).join("");
  sug.querySelectorAll(".sug-chip").forEach(chip => {
    chip.onclick = () => {
      $("input").value = chip.textContent;
      autoGrow($("input"));
      $("input").focus();
    };
  });
}

/* ---------- 工作区 chip + 下拉: 选了文件夹归项目, 不选归独立任务 ---------- */
const WS_CHEV = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';

function renderWsChip() {
  const chip = $("ws-chip");
  if (!chip) return;
  if (state.draftDir) {
    chip.classList.add("on");
    chip.innerHTML = FOLDER_SVG + "<span class='ws-name'></span>" +
      "<span class='ws-x' data-tip='清除, 作为独立任务'>×</span>" + WS_CHEV;
    chip.querySelector(".ws-name").textContent = projectDisplayName(state.draftDir);
    chip.title = state.draftDir;
    chip.querySelector(".ws-x").onclick = ev => {
      ev.stopPropagation();
      state.draftDir = null;
      renderWsChip();
    };
  } else {
    chip.classList.remove("on");
    chip.innerHTML = FOLDER_SVG + "<span>选择工作区</span>" + WS_CHEV;
    chip.title = "";
  }
  chip.onclick = ev => { ev.stopPropagation(); openWsPop(chip); };
}

function knownWorkspaces() {
  const set = new Set(state.customProjects);
  for (const s of state.sessions) if (s.workdir) set.add(s.workdir);
  return [...set];
}

function openWsPop(anchor) {
  closeWsPop();
  const known = knownWorkspaces();
  const pop = document.createElement("div");
  pop.id = "ws-pop";
  pop.innerHTML = "<input class='ws-search' placeholder='搜索工作区'><div class='ws-list'></div>" +
    "<div class='ws-item ws-act' data-act='browse'>" + FOLDER_SVG + "<span>打开文件夹…</span></div>" +
    "<div class='ws-item ws-act' data-act='none'>" + FOLDER_SVG + "<span>不在项目中工作</span></div>";
  document.body.appendChild(pop);
  const list = pop.querySelector(".ws-list");
  const renderList = q => {
    const items = known.filter(w => !q || w.toLowerCase().includes(q.toLowerCase()));
    list.innerHTML = items.length
      ? items.map(w =>
          `<div class="ws-item${w === state.draftDir ? " on" : ""}">` +
          FOLDER_SVG + "<span>" + escapeHtml(projectDisplayName(w)) + "</span>" +
          (w === state.draftDir ? "<span class='ws-check'>✓</span>" : "") + "</div>").join("")
      : '<div class="list-empty" style="margin:8px 10px">没有匹配的工作区</div>';
    const els = list.querySelectorAll(".ws-item");
    items.forEach((w, i) => {
      els[i].onclick = () => {
        state.draftDir = w;
        closeWsPop();
        renderWsChip();
      };
    });
  };
  renderList("");
  pop.querySelector(".ws-search").addEventListener("input", ev => renderList(ev.target.value));
  pop.querySelector("[data-act='browse']").onclick = async () => {
    closeWsPop();
    if (window.xcodePickFolder) {   // 桌面端: 原生文件夹对话框
      try {
        const dir = await window.xcodePickFolder();
        if (dir) { addProject(dir); state.draftDir = dir; renderWsChip(); }
      } catch (e) { /* 用户取消 */ }
      return;
    }
    openDirPop(anchor);             // 浏览器/预览: 页面内目录浏览兜底
  };
  pop.querySelector("[data-act='none']").onclick = () => {
    state.draftDir = null;
    closeWsPop();
    renderWsChip();
  };
  // 定位: chip 下方, 越界时翻到上方/收拢
  const r = anchor.getBoundingClientRect();
  pop.style.visibility = "hidden";
  requestAnimationFrame(() => {
    let left = Math.max(10, Math.min(r.left, window.innerWidth - pop.offsetWidth - 10));
    let top = r.bottom + 6;
    if (top + pop.offsetHeight > window.innerHeight - 10) {
      top = Math.max(10, r.top - pop.offsetHeight - 6);
    }
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    pop.style.visibility = "";
    pop.querySelector(".ws-search").focus();
  });
}
function closeWsPop() { const p = $("ws-pop"); if (p) p.remove(); }
document.addEventListener("click", ev => {
  const pop = $("ws-pop");
  if (!pop) return;
  const chip = $("ws-chip");
  if (!pop.contains(ev.target) && !(chip && chip.contains(ev.target))) closeWsPop();
});

function startDraft(draftDir = null) {
  saveCurrentInput();          // 离开原会话: 保存其未发送输入
  state.draft = true;
  state.sessionId = null;
  state.draftDir = draftDir;   // 侧栏项目行 + 进入: 首条消息据此把会话归入该项目
  markActiveSession();
  refreshDocTitle();
  const col = colOf("__draft__");
  col.innerHTML = "";
  showCol("__draft__");
  showEmptyState();
  setBusyUi(false);
  setConn("", "未连接");
  restoreCurrentInput();       // 恢复草稿态自己的输入
  $("input").focus();
}

/* 历史消息回放: role + blocks（text / tool_use / tool_result） */
function renderHistoryMessage(m) {
  if (m.role === "user") {
    const text = m.blocks.filter(b => b.type === "text").map(b => b.text).join("\n");
    if (text) addUserBubble(text);
    return;
  }
  if (m.role === "assistant") {
    // text 与 tool_use 交替出现: text → 气泡, tool_use → 卡片（登记待配对）
    let buf = [];
    const flush = () => {
      if (buf.length) { addAssistantBubble(buf.join("")); buf = []; }
    };
    const run = pinnedRun || curRun();
    for (const b of m.blocks) {
      if (b.type === "text") {
        buf.push(renderMd(b.text));
      } else if (b.type === "tool_use") {
        flush();
        const card = addToolCard({ id: b.id, name: b.name, input: b.input });
        run.toolResultIndex[b.id] = card;   // 等 tool 角色的结果块配对
      }
    }
    flush();
    return;
  }
  if (m.role === "tool") {
    const run = pinnedRun || curRun();
    for (const b of m.blocks) {
      if (b.type !== "tool_result") continue;
      const card = run.toolResultIndex[b.id];
      if (card) {
        completeToolCard(card, { output: b.output, is_error: b.is_error, denied: false });
      } else {
        // 配不上对（旧数据/截断）: 独立卡片兜底
        addToolCard({ id: b.id, name: b.name, input: "(—)",
                      result: { output: b.output, is_error: b.is_error, denied: false } });
      }
    }
  }
}

/* ============================================================
 * WebSocket
 * ============================================================ */
function setConn(cls, text) {
  $("conn-state").className = cls;
  $("conn-text").textContent = text;
}

function connectWs(id) {
  if (!id) return;
  const run = runOf(id);
  // 已连接或连接中就不动——切换会话不再断开后台会话的 WS
  if (run.ws && (run.ws.readyState === 0 || run.ws.readyState === 1)) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${id}`);
  run.ws = ws;

  ws.onopen = () => {
    if (id === state.sessionId) setConn("on", "已连接");
    run.reconnectAttempts = 0;
    // 首条消息在 WS 建立期间入队，连接好了统一发出
    const pending = run.pendingSends;
    run.pendingSends = [];
    for (const p of pending) ws.send(JSON.stringify(p));
    // 断线重连后的重同步: 断连窗口内的工具/正文事件已丢,
    // 重拉历史替换整列, 丢配的卡片不会以"运行中"僵住。
    // 仅在重连时做——草稿首发是"先画乐观气泡再 connectWs",
    // 而 turn 落盘只在结束时, 首连就重拉会拿空历史把用户消息抹掉
    const reconnected = run.everConnected;
    run.everConnected = true;
    if (reconnected && run.loaded && !run.loading) loadSessionHistory(id);
  };
  ws.onclose = () => {
    if (id === state.sessionId) setConn("", "已断开");
    if (run.ws === ws) run.ws = null;
    scheduleReconnect(id);
  };
  ws.onerror = () => { if (id === state.sessionId) setConn("err", "连接错误"); };
  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleServerMessage(msg, id);
  };
}

function closeWs(id) {
  const run = id ? state.runs[id] : null;
  if (run) {
    if (run.reconnectTimer) { clearTimeout(run.reconnectTimer); run.reconnectTimer = null; }
    if (run.ws) {
      const ws = run.ws;
      run.ws = null;
      ws.onclose = null;   // 防止触发断连提示
      ws.close();
    }
  }
}

/* 断线自动重连（服务重启 / 网络闪断）: 指数退避至 10s, 最多 40 次。
 * 恢复后的历史对齐在 ws.onopen 里做——丢掉的事件以落盘历史为准。 */
function scheduleReconnect(id) {
  const run = runOf(id);
  if (run.reconnectTimer) return;
  if (++run.reconnectAttempts > 40) {
    if (id === state.sessionId) setConn("err", "重连失败, 切换会话可重试");
    return;
  }
  const delay = Math.min(2500 * run.reconnectAttempts, 10000);
  run.reconnectTimer = setTimeout(() => {
    run.reconnectTimer = null;
    if (run.ws) return;   // 已被 selectSession / 手动重连抢先
    connectWs(id);
  }, delay);
}

/* ============================================================
 * 服务端消息分派
 * ============================================================ */
function handleServerMessage(msg, sid) {
  const run = runOf(sid);
  // 后台会话: 流式内容照常写进它自己的常驻列（隐藏）, 切回时完整可见;
  // 只对 权限/结果/结束/错误 累计未读。结束/接力时同步运行态。
  if (sid !== state.sessionId) {
    if (msg.type === "text_delta") onTextDelta(msg, sid);
    else if (msg.type === "thinking_start") onThinkingStart(msg, sid);
    else if (msg.type === "thinking_end") onThinkingEnd(msg, sid);
    else if (msg.type === "tool_use") onToolUse(msg, sid);
    else if (msg.type === "tool_result") onToolResult(msg, sid);
    else if (msg.type === "await_output") run.awaiting = run.busy;

    if (msg.type === "permission_request") { bumpUnread(sid); run.pendingPerm = msg; }
    else if (msg.type === "tool_result") bumpUnread(sid);
    else if (msg.type === "turn_done" || msg.type === "error") {
      bumpUnread(sid);
      run.busy = false;
      run.awaiting = false;
      run.queued = false;
      run.pendingPerm = null;
      // 轮次收口: 悬空工具行标"已中断", 清掉流式指针
      sweepPendingToolCards(run);
      run.curBubble = null;
      // 思考行/乐观胶囊兜底收口（客户端计时）, 与前台 endTurnUiReset 一致;
      // 只清指针的话, 行会永远卡在"思考中…"动画态
      if (run.curThinking) onThinkingEnd({}, sid);
      // 打断收口与前台 onTurnDone 一致: 「已停止」挂在本轮思考行上
      if (msg.type === "turn_done" && msg.interrupted
          && run.lastThinkRow && run.lastThinkRow.isConnected) {
        const tag = document.createElement("span");
        tag.className = "t-stopped";
        tag.textContent = "已停止";
        run.lastThinkRow.appendChild(tag);
      }
    } else if (msg.type === "turn_started") {
      run.busy = true;               // 排队的后续消息接力开跑
      run.lastThinkRow = null;
      const qi = run.queue.indexOf(msg.text);
      if (qi >= 0) run.queue.splice(qi, 1);
      addUserBubble(msg.text, colOf(sid));
      beginOptimisticThinking(run, sid, colOf(sid));
    }
    return;
  }
  switch (msg.type) {
    case "text_delta":         onTextDelta(msg, sid); break;
    case "tool_use":           onToolUse(msg, sid); break;
    case "tool_result":        onToolResult(msg, sid); break;
    case "thinking_start":     onThinkingStart(msg, sid); break;
    case "thinking_end":       onThinkingEnd(msg, sid); break;
    case "turn_queued":        onTurnQueued(msg); break;
    case "turn_started":       onTurnStarted(msg, sid); break;
    case "turn_queued_user":   onTurnQueuedUser(msg, sid); break;
    case "turn_queue_cleared": onQueueCleared(sid); break;
    case "await_output":       onAwaitOutput(msg, state.sessionId); break;
    case "permission_request": onPermissionRequest(msg); break;
    case "turn_done":          onTurnDone(msg); break;
    case "session_renamed":    onSessionRenamed(msg); break;
    case "error":              onError(msg); break;
    default: console.warn("未知消息类型", msg);
  }
}

function bumpUnread(sid) {
  const run = runOf(sid);
  run.unread += 1;
  const s = state.sessions.find(x => x.id === sid);
  if (s) s._unreadShown = run.unread;
  renderSessionList();
}

/* ---------- 正文流式（sid 感知: 后台会话写进自己的列） ---------- */
function onTextDelta(msg, sid) {
  const run = runOf(sid);
  const active = sid === state.sessionId;
  run.awaiting = false;                    // 首个内容事件: 等待空窗结束
  if (active) syncThinkingIndicator();
  dropOptimisticThinking(run);   // 正文先到: 撤掉还没被 thinking_start 接管的乐观胶囊
  if (!run.curBubble) {
    run.curBubble = addAssistantBubble("", "", colOf(sid));
    run.curBubble._raw = "";
  }
  run.curBubble._raw += msg.text;
  run.curBubble.innerHTML = renderMd(run.curBubble._raw);
  decorateCode(run.curBubble);
  scrollToBottom();
}

/* 流式气泡收口: 有内容就固化，下一轮正文开新气泡 */
function flushAssistantBubble(run) {
  if (!run) return;
  if (run.curBubble && !run.curBubble._raw) {
    run.curBubble.closest(".msg").remove();   // 空气泡（模型直接调工具）: 移除
  }
  run.curBubble = null;
}

/* ---------- 思考行 ---------- */
const ICON_MIND = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 00-4 10.5c.8.7 1.4 1.5 1.6 2.5h4.8c.2-1 .8-1.8 1.6-2.5A6 6 0 0012 3z"/></svg>';
const ICON_TERM = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7 9l3.5 3L7 15M12.5 15H17"/></svg>';
const ICON_FILE = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6V3z"/><path d="M14 3v4h4"/></svg>';
const ICON_EDIT = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20l4.5-1L20 7.5 16.5 4 5 15.5 4 20z"/></svg>';
const ICON_TOOL = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="4" y="7" width="16" height="13" rx="2"/><path d="M9 7V5a2 2 0 012-2h2a2 2 0 012 2v2"/></svg>';
const TOOL_META = {
  bash:       { label: "终端",     icon: ICON_TERM },
  powershell: { label: "终端",     icon: ICON_TERM },
  read_file:  { label: "读取文件", icon: ICON_FILE },
  write_file: { label: "写入文件", icon: ICON_EDIT },
};

function fmtDuration(ms) {
  const s = ms / 1000;
  if (s < 1) return "不到 1 秒";
  if (s < 60) return Math.round(s) + " 秒";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return r ? `${m} 分 ${r} 秒` : `${m} 分钟`;
}

function onThinkingStart(msg, sid) {
  const run = runOf(sid);
  const active = sid === state.sessionId;
  run.awaiting = false;                    // 思考行已是可见反馈: 空窗结束
  if (active) syncThinkingIndicator();
  flushAssistantBubble(run);
  if (run.curThinking) return;   // 已有实时思考行或乐观胶囊: 直接采用, 计时连续不归零
  const div = document.createElement("div");
  div.className = "think-row thinking";
  div.innerHTML = '<span class="t-ico">' + ICON_MIND + '</span>' +
    '<span class="shine">思考中…</span>';
  colOf(sid).appendChild(div);
  run.curThinking = { el: div, t0: Date.now() };
  run.lastThinkRow = div;   // 本轮思考行引用: 打断时「已停止」挂在这里
  if (active) scrollToBottom();
}

function onThinkingEnd(msg, sid) {
  const run = runOf(sid);
  const active = sid === state.sessionId;
  if (active) syncThinkingIndicator();
  const cur = run.curThinking;
  if (!cur) return;
  // 服务端计时优先，缺失（轮次兜底收口）时用客户端起止时间。
  // 例外: 乐观胶囊出身的思考行用客户端起止——服务端 duration_ms 只覆盖
  // 思考块本身, 会把 发送→首个思考块 之间的 prefill 等待丢掉, 违背
  // "等待+思考合并计时"的语义（等待就是用户真实等待的一部分）。
  const ms = (cur.optimistic || typeof msg.duration_ms !== "number")
    ? Date.now() - cur.t0 : msg.duration_ms;
  cur.el.classList.remove("thinking");
  cur.el.innerHTML = '<span class="t-ico">' + ICON_MIND + '</span>' +
    '<span>思考 · 持续了 ' + fmtDuration(ms) + '</span>';
  run.curThinking = null;
  run.lastThinkRow = cur.el;
  if (active) scrollToBottom();
}

/* ---------- 乐观思考胶囊 ----------
 * 发送/接力开跑的瞬间先渲染"思考中…"胶囊, 把模型首个事件之前不可见的
 * prefill 等待（长会话可达几十秒）变成可见反馈。thinking_start 到达时,
 * onThinkingStart 的 curThinking 守卫直接采用它而不新建第二条, 计时从乐观
 * 创建时刻起连续不归零; 若首个输出是正文/工具, dropOptimisticThinking 撤下
 * （正文气泡/工具卡已经是反馈, 不能让胶囊与它们同屏挂着）。 */
function beginOptimisticThinking(run, sid, col) {
  if (run.curThinking || run.curBubble) return;   // 已有思考行/正文已开流: 不重复创建
  const div = document.createElement("div");
  div.className = "think-row thinking";
  div.innerHTML = '<span class="t-ico">' + ICON_MIND + '</span>' +
    '<span class="shine">思考中…</span>';
  col.appendChild(div);
  run.curThinking = { el: div, t0: Date.now(), optimistic: true };
  run.lastThinkRow = div;   // 打断「已停止」与轮次收口兜底都走 lastThinkRow, 复用既有逻辑
  if (sid === state.sessionId) scrollToBottom();
}

function dropOptimisticThinking(run) {
  const cur = run.curThinking;
  if (!cur || !cur.optimistic) return;   // 只撤尚未被 thinking_start 接管的
  cur.el.remove();
  if (run.lastThinkRow === cur.el) run.lastThinkRow = null;
  run.curThinking = null;
}

/* ---------- 工具调用 ---------- */
function describeInput(raw) {
  // 卡片标题一行摘要: JSON 先取 command/path 等关键字段，失败展示原文
  try {
    const data = JSON.parse(raw);
    if (data && typeof data === "object") {
      for (const k of ["command", "path", "file_path", "url", "content"]) {
        if (typeof data[k] === "string" && data[k].trim()) {
          return data[k].replace(/\s+/g, " ").slice(0, 90);
        }
      }
      return Object.keys(data)
        .map(k => `${k}=${String(data[k])}`).join(" ").slice(0, 90);
    }
  } catch (e) { /* not json */ }
  return String(raw || "").replace(/\s+/g, " ").slice(0, 90);
}

function addToolCard({ id, name, input, result }, col) {
  const meta = TOOL_META[name] || { label: name, icon: ICON_TOOL };
  const row = document.createElement("div");
  row.className = "tool-row";
  row.innerHTML =
    '<span class="t-ico">' + meta.icon + '</span>' +
    '<span class="tname2"></span><span class="tdesc"></span><span class="tstate"></span>';
  row.querySelector(".tname2").textContent = meta.label;
  row.querySelector(".tdesc").textContent = describeInput(input);
  if (result) {
    completeToolCard(row, result);
  } else {
    setToolState(row, "run");
  }
  (col || msgCol()).appendChild(row);
  scrollToBottom();
  return row;
}

function setToolState(row, kind) {
  row.dataset.state = kind;
  const st = row.querySelector(".tstate");
  st.className = "tstate " + kind;
  st.textContent = { run: "运行中", ok: "已完成", err: "出错",
                     denied: "已拒绝", stopped: "已中断" }[kind] || kind;
}

function completeToolCard(row, { is_error, denied }) {
  if (denied) {
    setToolState(row, "denied");
  } else if (is_error) {
    setToolState(row, "err");
  } else {
    setToolState(row, "ok");
  }
}

function onToolUse(msg, sid) {
  const run = runOf(sid);
  const active = sid === state.sessionId;
  run.awaiting = false;                    // 工具卡已是可见反馈: 空窗结束
  if (active) syncThinkingIndicator();
  flushAssistantBubble(run);   // 工具前先收掉流式中的正文气泡
  dropOptimisticThinking(run);   // 工具先于思考到达: 撤掉乐观胶囊（工具卡已是反馈）
  const card = addToolCard({ id: msg.id, name: msg.name, input: msg.input }, colOf(sid));
  if (msg.id) run.liveToolCards[msg.id] = card;   // 按 id 登记, 结果精确配对
  run.activeToolCard = card;
}

function onToolResult(msg, sid) {
  const run = runOf(sid);
  flushAssistantBubble(run);
  // 配对优先级: 流式卡片(按 id) → 历史回放登记的卡片(断线重同步接缝) →
  // 旧单槽位 → 都配不上(旧数据)才新开兜底卡片
  let card = null;
  if (msg.id && run.liveToolCards[msg.id]) {
    card = run.liveToolCards[msg.id];
    delete run.liveToolCards[msg.id];
  }
  if (!card && msg.id && run.toolResultIndex[msg.id]) {
    card = run.toolResultIndex[msg.id];
    delete run.toolResultIndex[msg.id];
  }
  if (!card && run.activeToolCard) card = run.activeToolCard;
  if (card) {
    completeToolCard(card, msg);
  } else {
    completeToolCard(addToolCard({ id: msg.id, name: msg.name, input: msg.input }, colOf(sid)), msg);
  }
  if (run.activeToolCard === card) run.activeToolCard = null;
}

/* 轮次收口: 把该会话所有还挂在"运行中"的工具卡统一闭合为"已中断"
 * （被打断/异常/断连丢事件, 结果永远来不了）。 */
function sweepPendingToolCards(run) {
  const sweep = c => { if (c && c.dataset.state === "run") setToolState(c, "stopped"); };
  Object.values(run.liveToolCards || {}).forEach(sweep);
  Object.values(run.toolResultIndex || {}).forEach(sweep);
  run.liveToolCards = {};
  run.toolResultIndex = {};
  run.activeToolCard = null;
}

/* ---------- 权限审批 ---------- */
function onPermissionRequest(msg) {
  const run = curRun();
  run.pendingPerm = msg;
  const s = state.sessions.find(x => x.id === state.sessionId);
  $("perm-title").textContent =
    "需要授权" + (s ? ` — ${displayTitle(s)}` : "");
  $("perm-tool").textContent = msg.tool_name;
  $("perm-mode").textContent = `${msg.required_mode}（当前 ${msg.current_mode}）`;
  $("perm-input").textContent = msg.input;
  $("perm-overlay").style.display = "flex";
  $("btn-allow").focus();
}

function respondPermission(approved) {
  const run = curRun();
  if (!run || !run.pendingPerm) return;
  sendWs({
    type: "permission_response",
    request_id: run.pendingPerm.request_id,
    approved,
  });
  run.pendingPerm = null;
  $("perm-overlay").style.display = "none";
}

$("btn-allow").onclick = () => respondPermission(true);
$("btn-deny").onclick = () => respondPermission(false);
/* 弹窗快捷键: Enter 允许 / Esc 拒绝 */
document.addEventListener("keydown", ev => {
  if ($("perm-overlay").style.display !== "flex") return;
  if (ev.key === "Enter") { ev.preventDefault(); respondPermission(true); }
  else if (ev.key === "Escape") { ev.preventDefault(); respondPermission(false); }
});

/* ---------- 轮次结束 / 错误 ---------- */
function endTurnUiReset() {
  const run = curRun();
  if (run) {
    run.busy = false;
    run.awaiting = false;
    run.queued = false;
    run.unread = 0;             // 前台亲眼看完了, 未读清零
    flushAssistantBubble(run);
    // 轮次结束还有工具行停在"运行中"（被打断/异常, 结果永远来不了）: 收口
    sweepPendingToolCards(run);
    if (run.curThinking) onThinkingEnd({}, state.sessionId);   // 思考行兜底收口（客户端计时）
  }
  syncThinkingIndicator();
  setBusyUi(false);
  renderSessionList();          // 运行标识/未读刷新
}

function onTurnQueued(msg) {
  addNoteBubble("warn", `并发已满（上限 ${msg.max_concurrent} 轮），等前面的轮次结束后自动开始`);
}

function onAwaitOutput(msg, sid) {
  // 模型调用已发出、首个 token 未到的空窗（每轮 prefill / 工具跑完后的下一轮）:
  // 空窗状态记在会话上, 显示统一走 syncThinkingIndicator 重算——
  // 切到该会话时也能正确显示/隐藏, 不会残留到别的会话
  const run = runOf(sid);
  if (run && run.busy) {
    run.awaiting = true;
    if (sid === state.sessionId) syncThinkingIndicator();
  }
}

/* ---------- 接力: 轮到待发送的后续消息了 ---------- */
function onTurnStarted(msg, sid) {
  const run = runOf(sid);
  run.busy = true;
  run.awaiting = false;      // 新一轮: 上一轮的空窗状态作废, 等 await_output 重新点亮
  run.lastThinkRow = null;   // 新一轮开始: 打断标记只属于当前轮的思考行
  // 待发送卡片此刻转正: 从队列撤下, 消息正式出现在消息流
  const qi = run.queue.indexOf(msg.text);
  if (qi >= 0) run.queue.splice(qi, 1);
  addUserBubble(msg.text, colOf(sid));
  beginOptimisticThinking(run, sid, colOf(sid));   // 排队消息接力开跑: 立刻给反馈
  if (sid === state.sessionId) {
    renderQueueCards();
    syncThinkingIndicator();
    setBusyUi(true);
    scrollToBottom();
  }
  renderSessionList();
}

function onTurnQueuedUser(msg, sid) {
  // 服务端入队回执: 前端只显示中性的"待发送"徽标, 不展示排队位置
}

function onQueueCleared(sid) {
  // 打断/断连清空待发送区: 撤掉全部卡片, 文本放回输入框不丢
  const run = runOf(sid);
  if (!run.queue.length) return;
  const texts = run.queue.slice();
  run.queue.length = 0;
  if (sid === state.sessionId) {
    renderQueueCards();
    const cur = $("input").value.trim();
    $("input").value = cur ? cur + "\n" + texts.join("\n") : texts.join("\n");
    autoGrow($("input"));
  }
}

function onTurnDone(msg) {
  endTurnUiReset();
  if (msg.interrupted) {
    // 「已停止」挂在本轮思考行的胶囊里; 本轮没思考过（工具/正文阶段打断）才落成独立提示行
    const row = curRun()?.lastThinkRow;
    if (row && row.isConnected) {
      const tag = document.createElement("span");
      tag.className = "t-stopped";
      tag.textContent = "已停止";
      row.appendChild(tag);
    } else {
      addNoteBubble("stopped", "已停止");
    }
  } else if (msg.budget_exhausted) {
    addNoteBubble("warn", "本轮输出 token 预算已用尽，已提前收束本轮");
  } else if (msg.iterations_exhausted) {
    addNoteBubble("warn", `已达单轮最大迭代次数（${msg.iterations} 次调用），已提前收束本轮`);
  }
  // 刷新侧栏标题/消息数，标题可能被自动命名更新
  (async () => { await loadSessions(); refreshDocTitle(); })();
}

function onSessionRenamed(msg) {
  // AI 命名完成（在 turn_done 之后异步到达）: 刷新列表 + 顶栏标题
  (async () => { await loadSessions(); refreshDocTitle(); })();
}

function onError(msg) {
  // 错误也是本轮结束: 不复位 busy 的话输入框会永久锁死，看起来像卡死
  endTurnUiReset();
  addNoteBubble("err", msg.message || "未知错误");
}

function addNoteBubble(kind, text) {
  const div = document.createElement("div");
  div.className = "note " + kind;
  div.textContent = (kind === "err" ? "✗ " : kind === "warn" ? "⚠ " : "") + text;
  msgCol().appendChild(div);
  scrollToBottom();
}

function addErrorBubble(text) {
  addNoteBubble("err", text);
}

/* ============================================================
 * 气泡工厂
 * ============================================================ */
function msgCol() {
  if (pinnedCol) return pinnedCol;   // 历史回放: 固定写入目标列
  const id = (state.draft || !state.sessionId) ? "__draft__" : state.sessionId;
  return colOf(id);
}

function addUserBubble(text, col) {
  const div = document.createElement("div");
  div.className = "msg user";
  div._text = text;
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = text;   // 用户输入永远纯文本
  div.appendChild(b);
  (col || msgCol()).appendChild(div);
  scrollToBottom();
  return b;
}

/* ---------- 待发送卡片: ↑立即(插队) / 编辑(放回输入框) / 删除 ---------- */
const Q_PROMOTE_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/></svg>';

function renderQueueCards() {
  const box = $("queue-cards");
  if (!box) return;
  box.innerHTML = "";
  const run = curRun();
  const items = run ? run.queue : [];
  items.forEach((text, idx) => {
    const card = document.createElement("div");
    card.className = "q-card";
    const t = document.createElement("span");
    t.className = "q-text";
    t.textContent = text;
    t.dataset.tip = text;   // 悬停看全文
    card.appendChild(t);
    const promote = document.createElement("button");
    promote.type = "button";
    promote.className = "q-promote";
    promote.innerHTML = Q_PROMOTE_SVG + "<span>立即</span>";
    promote.dataset.tip = "打断当前回复, 这条立即发送";
    promote.onclick = () => {
      card.classList.add("promoting");   // 已登记插队, 等当前回复收尾
      sendWs({ type: "queue_promote", text });
    };
    card.appendChild(promote);
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "q-ico";
    edit.innerHTML = PENCIL_SMALL_SVG;
    edit.dataset.tip = "编辑";
    edit.onclick = () => editQueued(idx);
    card.appendChild(edit);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "q-ico q-del";
    del.innerHTML = TRASH_SMALL_SVG;
    del.dataset.tip = "删除";
    del.onclick = () => removeQueued(idx);
    card.appendChild(del);
    box.appendChild(card);
  });
}

function editQueued(idx) {
  const run = curRun();
  if (!run || run.queue[idx] === undefined) return;
  const [text] = run.queue.splice(idx, 1);
  sendWs({ type: "queue_remove", text });
  const input = $("input");
  input.value = input.value ? input.value + "\n" + text : text;
  autoGrow(input);
  input.focus();
  saveCurrentInput();
  updateSendBtn();
  renderQueueCards();
}

function removeQueued(idx) {
  const run = curRun();
  if (!run) return;
  const [text] = run.queue.splice(idx, 1);
  if (text !== undefined) sendWs({ type: "queue_remove", text });
  renderQueueCards();
}

function addAssistantBubble(html, raw, col) {
  const div = document.createElement("div");
  div.className = "msg assistant";
  const avatar = document.createElement("img");
  avatar.className = "avatar";
  avatar.src = iconUrl();
  avatar.alt = "";
  const b = document.createElement("div");
  b.className = "bubble";
  b.innerHTML = html || "";
  decorateCode(b);
  div.appendChild(avatar);
  div.appendChild(b);
  (col || msgCol()).appendChild(div);
  scrollToBottom();
  return b;
}

/* ============================================================
 * 发送 / 中断 / 输入框
 * 同一个圆钮双形态: 输入框有内容 = 发送; 为空且本轮进行中 = 中断
 * ============================================================ */
function updateSendBtn() {
  const hasText = !!$("input").value.trim();
  const busy = !!(curRun() && curRun().busy);
  const btn = $("btn-send");
  const stop = !hasText && busy;
  btn.dataset.mode = stop ? "stop" : "send";
  btn.dataset.tip = stop ? "中断对话" : "发送";
}
function setBusyUi(busy) {
  // 忙碌态占位符对齐 Claude.ai: 提示可以直接继续排队
  $("input").placeholder = busy ? "继续输入以排队后续修改" : "提出后续修改要求";
  updateSendBtn();
}

/* 底部"思考中"转圈 = 当前会话忙且正处于等待模型输出的空窗（await_output
 * 起至首个内容事件）。此前各事件分支里手工开关、切换会话不重算:
 * 切到空闲会话转圈残留、后台轮次跑完转圈不灭——统一在这里按当前会话重算。 */
function syncThinkingIndicator() {
  const run = curRun();
  $("thinking").style.display = run && run.busy && run.awaiting ? "flex" : "none";
}

async function sendCurrent() {
  const input = $("input");
  const text = input.value.trim();
  const run = curRun();
  const busy = !!(run && run.busy);
  if (!text) return;
  if (!state.draft && (!run || !run.ws || run.ws.readyState !== 1)) return;
  // 草稿态: 此刻才向服务端要 id 建会话条目；失败则留在草稿态
  let firstWorkdir = "";
  if (state.draft) {
    firstWorkdir = state.draftDir || "";
    try {
      const r = await fetch("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workdir: firstWorkdir }),   // 创建即绑定项目, 列表立刻归组
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      state.sessionId = (await r.json()).id;
    } catch (e) {
      toast("创建会话失败: " + e.message);
      return;
    }
  }
  msgCol().querySelector(".empty-state")?.remove();
  $("pane").classList.remove("empty-view");   // 有内容了: 输入卡落回底部
  $("ws-dock").innerHTML = "";                // 草稿态的工作区条/建议 chips 一并撤下
  $("sug-dock").innerHTML = "";
  nearBottom = true;
  scrollToBottom(true);   // 发送是用户主动行为: 无论滚到哪里, 立刻回到底部看最新消息
  // 本轮在跑: 消息进入输入框上方的待发送卡片, 轮到它时才出现在消息列
  if (busy) {
    runOf(state.sessionId).queue.push(text);
    input.value = "";
    autoGrow(input);
    saveCurrentInput();          // 已发送: 清空本会话的输入草稿
    updateSendBtn();             // 输入已清空: 圆钮切回"停止"形态, 随时可中断
    renderQueueCards();
    sendWs({ type: "user", text });
    return;
  }
  addUserBubble(text);
  input.value = "";
  autoGrow(input);
  saveCurrentInput();          // 已发送: 清空本会话的输入草稿
  updateSendBtn();             // 输入已清空: 忙碌态下圆钮切回"停止"形态
  const myRun = runOf(state.sessionId);
  if (!busy) {
    myRun.busy = true;
    myRun.awaiting = false;
    myRun.lastThinkRow = null;   // 新一轮开始: 打断标记只属于当前轮的思考行
    beginOptimisticThinking(myRun, state.sessionId, msgCol());   // 乐观胶囊: 发送瞬间即有反馈
    syncThinkingIndicator();
    setBusyUi(true);
    renderSessionList();   // 立即显示运行状态（转圈图标）
  }
  if (state.draft) {
    state.draft = false;
    state.draftDir = null;   // 已转正: 预选项目用完即清
    myRun.loaded = true;   // 草稿列里的气泡就是全部内容, 无需再拉历史
    // 草稿列转正为该会话的消息列（气泡不挪窝）
    colOf("__draft__").id = "msg-col-" + state.sessionId;
    // 列表此刻才出现新条目并选中；WS 建立期间消息会排队，onopen 后冲刷
    await loadSessions();
    renderSessionList();
    markActiveSession();
    refreshDocTitle();
    connectWs(state.sessionId);
  }
  sendWs({ type: "user", text, workdir: firstWorkdir });
}

function sendWs(obj) {
  const run = curRun();
  if (!run) return;
  if (run.ws && run.ws.readyState === 1) {
    run.ws.send(JSON.stringify(obj));
  } else if (run.ws && run.ws.readyState === 0) {
    run.pendingSends.push(obj);   // 连接建立中: onopen 后冲刷
  }
}

$("btn-send").onclick = () => {
  if ($("btn-send").dataset.mode === "stop") sendWs({ type: "stop" });
  else sendCurrent();
};
$("btn-new").onclick = () => startDraft();   // 包一层: 别把点击事件对象当成 draftDir 传进去

$("input").addEventListener("keydown", ev => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    sendCurrent();
  }
});
$("input").addEventListener("input", () => { saveCurrentInput(); updateSendBtn(); });
function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}
$("input").addEventListener("input", () => autoGrow($("input")));

/* ============================================================
 * 侧栏交互: 分组/项目切换 + 搜索 + 快捷键
 * ============================================================ */
function setSideTab(tab) {
  state.sideTab = tab;
  $("tab-project").classList.toggle("on", tab === "project");
  $("tab-group").classList.toggle("on", tab === "group");
  renderSessionList();
}
$("tab-project").onclick = () => setSideTab("project");
$("tab-group").onclick = () => setSideTab("group");

function openSearch() {
  $("search-box").classList.add("open");
  $("search-input").focus();
}
function closeSearch() {
  $("search-box").classList.remove("open");
  $("search-input").value = "";
  renderSessionList();
}
$("btn-search").onclick = openSearch;

$("search-input").addEventListener("input", renderSessionList);
$("search-input").addEventListener("keydown", ev => {
  if (ev.key === "Escape") closeSearch();
});

document.addEventListener("keydown", ev => {
  const mod = ev.ctrlKey || ev.metaKey;
  if (mod && ev.key.toLowerCase() === "n") {
    ev.preventDefault();
    startDraft();
  } else if (mod && ev.key.toLowerCase() === "k") {
    ev.preventDefault();
    openSearch();
  }
});


/* ============================================================
 * 划词工具条: 选中聊天文本后浮出
 *   添加到当前任务 → 选中文本预填进当前会话输入框
 *   复制仍走浏览器原生: 选中后右键"复制"或 Ctrl+C
 * ============================================================ */
const selBar = document.createElement("div");
selBar.className = "sel-bar";
selBar.innerHTML = '<button type="button" data-act="task">添加到当前任务</button>';
selBar.style.display = "none";
document.body.appendChild(selBar);

function hideSelBar() { selBar.style.display = "none"; }
function selectionInMessages() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return "";
  if (!$("messages").contains(sel.getRangeAt(0).commonAncestorContainer)) return "";
  return sel.toString().trim();
}
function moveSelBar() {
  const text = selectionInMessages();
  if (!text) { hideSelBar(); return; }
  const rect = window.getSelection().getRangeAt(0).getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) { hideSelBar(); return; }
  selBar.style.display = "flex";
  const bw = selBar.offsetWidth, bh = selBar.offsetHeight;
  let left = rect.left + rect.width / 2 - bw / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - bw - 8));
  let top = rect.top - bh - 8;              // 默认浮在选区上方
  if (top < 8) top = Math.min(rect.bottom + 8, window.innerHeight - bh - 8);
  selBar.style.left = left + "px";
  selBar.style.top = top + "px";
}
document.addEventListener("selectionchange", () => {
  clearTimeout(moveSelBar._t);
  moveSelBar._t = setTimeout(moveSelBar, 120);   // 拖选过程轻微防抖
});
window.addEventListener("scroll", hideSelBar, true);
window.addEventListener("resize", hideSelBar);
selBar.addEventListener("mousedown", ev => ev.preventDefault());   // 点击按钮不丢选区
selBar.addEventListener("click", ev => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  const text = selectionInMessages();
  hideSelBar();
  const sel = window.getSelection();
  if (sel) sel.removeAllRanges();
  if (!text) return;
  $("input").value = text;
  autoGrow($("input"));
  updateSendBtn();
  $("input").focus();
});

/* ---------- 右键菜单兜底: 选中文字后右键 → "复制" ---------- */
/* Electron 里由主进程弹原生菜单（userAgent 含 Electron 时跳过）,
   普通浏览器/内嵌预览没有原生菜单, 用这个页面内菜单兜底 */
const ctxMenu = document.createElement("div");
ctxMenu.className = "ctx-menu";
ctxMenu.innerHTML = '<button type="button" data-act="copy">复制</button>';
ctxMenu.style.display = "none";
document.body.appendChild(ctxMenu);
function hideCtxMenu() { ctxMenu.style.display = "none"; }
document.addEventListener("contextmenu", ev => {
  hideCtxMenu();
  if (window.xcodeDesktop) return;   // Electron 桌面端: 主进程弹原生菜单（preload 桥标记）
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0
      || !$("messages").contains(sel.getRangeAt(0).commonAncestorContainer)) {
    return;   // 没有聊天区选区: 走浏览器默认菜单（输入框的粘贴等）
  }
  ev.preventDefault();
  hideSelBar();
  ctxMenu.style.display = "block";
  ctxMenu.style.left = Math.min(ev.clientX, window.innerWidth - 140) + "px";
  ctxMenu.style.top = Math.min(ev.clientY, window.innerHeight - 50) + "px";
});
ctxMenu.addEventListener("mousedown", e => e.preventDefault());   // 不丢选区
ctxMenu.addEventListener("click", ev => {
  const btn = ev.target.closest("button[data-act='copy']");
  if (!btn) return;
  const sel = window.getSelection();
  const text = sel ? sel.toString() : "";
  hideCtxMenu();
  if (sel) sel.removeAllRanges();
  copyText(text).then(ok => toast(ok ? "已复制" : "复制失败"));
});
window.addEventListener("scroll", hideCtxMenu, true);
document.addEventListener("click", ev => {
  if (!ctxMenu.contains(ev.target)) hideCtxMenu();
});

/* ============================================================
 * 设置（输入卡片内的下拉）
 * ============================================================ */
function prettyModel(name) {
  // glm-5.3-flash → GLM 5.3 Flash（首段全大写，其余词首字母大写）
  if (!name) return "模型";
  return name.split("-").map((w, i) => {
    if (/^\d/.test(w)) return w;
    return i === 0 ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1);
  }).join(" ");
}

/* 自定义下拉组件: 触发按钮 + 定位弹层（输入卡片贴底, 默认向上弹出）。
   程序侧 setValue 只改显示不触发 onChange; 用户点选才回调。 */
function makeDropdown(trigger, opts) {
  let items = opts.items;
  let value = opts.value;
  let pop = null, onDocClick = null, onKey = null;

  function syncLabel() {
    const it = items.find(i => i.value === value);
    trigger.querySelector(".dd-label").textContent = it ? it.label : String(value ?? "");
  }
  function close() {
    if (!pop) return;
    pop.remove(); pop = null;
    trigger.classList.remove("open");
    document.removeEventListener("click", onDocClick, true);
    document.removeEventListener("keydown", onKey, true);
  }
  function choose(v) {
    const changed = v !== value;
    value = v; syncLabel(); close();
    if (changed && opts.onChange) opts.onChange(v);
  }
  function open() {
    if (pop) { close(); return; }
    pop = document.createElement("div");
    pop.className = "dd-pop" + (items.some(i => i.desc) ? " rich" : "");
    for (const it of items) {
      const o = document.createElement("button");
      o.type = "button";
      o.className = "dd-opt" + (it.desc ? " rich" : "") + (it.value === value ? " on" : "");
      o.innerHTML = '<span class="dd-check">' + CHECK_SVG + '</span>'
        + (it.icon ? '<span class="dd-ico">' + it.icon + '</span>' : '')
        + '<span class="dd-col"><span class="dd-txt"></span>'
        + (it.desc ? '<span class="dd-desc"></span>' : '')
        + '</span>';
      o.querySelector(".dd-txt").textContent = it.label;
      if (it.desc) o.querySelector(".dd-desc").textContent = it.desc;
      o.onclick = () => choose(it.value);
      pop.appendChild(o);
    }
    document.body.appendChild(pop);
    const r = trigger.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - pw - 8));
    let top = r.top - ph - 8;   // 输入卡片贴底: 默认向上弹
    if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - ph - 8);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    trigger.classList.add("open");
    onDocClick = ev => {
      if (!pop.contains(ev.target) && !trigger.contains(ev.target)) close();
    };
    onKey = ev => { if (ev.key === "Escape") close(); };
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onKey, true);
  }
  trigger.addEventListener("click", ev => { ev.stopPropagation(); open(); });
  syncLabel();
  return {
    setValue(v) { value = v; syncLabel(); },
    getValue: () => value,
    setItems(newItems, newValue) {
      items = newItems;
      if (newValue !== undefined) value = newValue;
      syncLabel();
    },
    close,
  };
}

/* 权限模式: 富菜单（图标 + 名称 + 描述, 当前项打勾） */
const ICON_MODE_EYE = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_MODE_HAND = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 12.5V5.5a1.5 1.5 0 013 0V11m0-5.5v-1a1.5 1.5 0 013 0V11m0-4.5a1.5 1.5 0 013 0V12m-9 .5l-2.4-2.2c-.9-.8-2.2-.4-2.5.8-.1.5 0 1 .3 1.4L10 19c1 1.3 2.3 2 4.2 2 3.2 0 4.8-2 4.8-5v-3.5"/></svg>';
const ICON_MODE_PENCIL = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20l4.5-1L20 7.5 16.5 4 5 15.5 4 20z"/></svg>';
const ICON_MODE_SHIELD = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 2.8v5.4c0 4.4-2.9 7.8-7 9.8-4.1-2-7-5.4-7-9.8V5.8L12 3z"/></svg>';
const ICON_MODE_ALLOW = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';
const MODE_ITEMS = [
  { value: "read-only", label: "只读", icon: ICON_MODE_EYE,
    desc: "只查看和分析，不执行任何修改。" },
  { value: "prompt", label: "每次询问", icon: ICON_MODE_HAND,
    desc: "改动前先征求我的意见。" },
  { value: "workspace-write", label: "自动编辑", icon: ICON_MODE_PENCIL,
    desc: "自动编辑工作区内的文件。" },
  { value: "danger-full-access", label: "完全访问", icon: ICON_MODE_SHIELD,
    desc: "减少确认次数，放开全部权限。" },
  { value: "allow", label: "全部允许", icon: ICON_MODE_ALLOW,
    desc: "所有工具直接放行，不再询问。" },
];
const THINK_ITEMS = [
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
  { value: "max", label: "最高" },
];
const modeDd = makeDropdown($("sel-mode"), {
  items: MODE_ITEMS, value: "prompt",
  onChange: v => saveSettings({ permission_mode: v }),
});
const thinkDd = makeDropdown($("sel-thinking"), {
  items: THINK_ITEMS, value: "medium",
  onChange: v => saveSettings({ thinking_level: v }),
});
/* 模型下拉: 选项来自所有启用供应商的模型（loadProviders 后填充） */
const modelDd = makeDropdown($("sel-model"), {
  items: [], value: "",
  onChange: v => {
    const [provider_id, model_id] = v.split("|");
    saveSettings({ provider_id, model_id });
  },
});

async function loadSettings() {
  try {
    const [r, pr] = await Promise.all([fetch("/api/settings"), fetch("/api/providers")]);
    const s = await r.json();
    state.providerCfg = await pr.json();
    syncModelDropdown(s.provider_id, s.model_id);
    modeDd.setValue(s.permission_mode);
    thinkDd.setValue(s.thinking_level);
    if (s.workspace) $("ws-tag-text").textContent = s.workspace;
    state.serverWorkspace = s.workspace || null;
    state.configured = s.configured !== false;   // 旧服务端无此字段时视为已配置
    state.defaultModel = s.model || null;
    refreshWorkdirTag();
    if (s.icon_ver) { iconVer = s.icon_ver; applyIconEverywhere(iconUrl()); }
  } catch (e) { console.error("加载设置失败", e); }
}

/* ---------- 应用图标: 设置 → 外观 可上传替换, 服务端落盘 static/icon.png ---------- */
let iconVer = 0;   // 图标文件版本（mtime）: 用 ?v= 穿透浏览器缓存
const iconUrl = () => "/static/icon.png" + (iconVer ? `?v=${iconVer}` : "");
function applyIconEverywhere(src) {
  document.querySelectorAll("img.mark, img.avatar").forEach(el => { el.src = src; });
  const fav = document.querySelector('link[rel="icon"]');
  if (fav) fav.href = src;
  const prev = $("icon-preview");
  if (prev) prev.src = src;
}

/* ---------- 模型供应商配置: 渲染 / 编辑 / 保存 ---------- */
function modelItems() {
  const items = [];
  for (const p of (state.providerCfg?.providers || [])) {
    if (p.enabled === false) continue;
    for (const m of (p.models || [])) {
      items.push({
        value: p.id + "|" + m.id,
        label: m.name || prettyModel(m.id),   // 只显示模型名, 不带供应商前缀
      });
    }
  }
  return items;
}
function syncModelDropdown(pid, mid) {
  modelDd.setItems(modelItems(), pid && mid ? pid + "|" + mid : undefined);
}
async function loadProviders() {
  try {
    state.providerCfg = await fetch("/api/providers").then(r => r.json());
  } catch (e) { console.error("加载供应商配置失败", e); }
}
function renderProviderSettings() {
  const wrap = $("provider-cards");
  if (!wrap) return;
  wrap.innerHTML = "";
  for (const p of (state.providerCfg?.providers || [])) {
    wrap.appendChild(buildProviderCard(p));
  }
}
function buildProviderCard(p) {
  const card = document.createElement("div");
  card.className = "prov-card";

  /* 头部: 行内名称 / 启用开关 / 删除 */
  const head = document.createElement("div");
  head.className = "prov-head";
  const name = document.createElement("input");
  name.className = "prov-name";
  name.value = p.name || "";
  name.placeholder = "供应商名称";
  name.addEventListener("input", () => { p.name = name.value; });
  const en = document.createElement("label");
  en.className = "prov-switch";
  const enBox = document.createElement("input");
  enBox.type = "checkbox";
  enBox.checked = p.enabled !== false;
  enBox.addEventListener("change", () => { p.enabled = enBox.checked; });
  const track = document.createElement("i");
  track.className = "track";
  en.append(enBox, track, document.createTextNode("已启用"));
  const del = document.createElement("button");
  del.type = "button";
  del.className = "prov-del";
  del.innerHTML = TRASH_SMALL_SVG;
  del.dataset.tip = "删除供应商";
  del.onclick = () => {
    if (!confirm(`删除供应商「${p.name}」？其模型将从下拉中移除。`)) return;
    state.providerCfg.providers = state.providerCfg.providers.filter(x => x !== p);
    if (state.providerCfg.active && state.providerCfg.active.provider === p.id) {
      state.providerCfg.active = {};
    }
    renderProviderSettings();
  };
  head.append(name, en, del);
  card.appendChild(head);

  /* 连接配置: Base URL / API Key 并排两列 */
  const grid = document.createElement("div");
  grid.className = "prov-grid";
  const urlField = document.createElement("div");
  urlField.className = "prov-field";
  urlField.innerHTML = "<label>BASE URL</label>";
  const url = document.createElement("input");
  url.className = "set-input mono";
  url.value = p.base_url || "";
  url.placeholder = "https://...";
  url.addEventListener("input", () => { p.base_url = url.value; });
  urlField.appendChild(url);
  const keyField = document.createElement("div");
  keyField.className = "prov-field";
  keyField.innerHTML = "<label>API KEY</label>";
  const keyRow = document.createElement("div");
  keyRow.className = "key-row";
  const key = document.createElement("input");
  key.type = "password";
  key.className = "set-input mono";
  key.value = p.api_key || "";
  key.addEventListener("input", () => { p.api_key = key.value; });
  const eye = document.createElement("button");
  eye.type = "button";
  eye.className = "key-eye";
  eye.textContent = "👁";
  eye.onclick = () => { key.type = key.type === "password" ? "text" : "password"; };
  keyRow.append(key, eye);
  keyField.appendChild(keyRow);
  grid.append(urlField, keyField);
  card.appendChild(grid);

  /* 模型列表: 列头 + 行 */
  const mField = document.createElement("div");
  mField.className = "prov-models";
  mField.innerHTML =
    '<div class="prov-models-label">模型列表</div>' +
    '<div class="model-cols"><span>显示名</span><span>模型 ID</span><span>标签</span><span></span></div>';
  const rows = document.createElement("div");
  rows.className = "model-rows";
  const buildModelRow = m => {
    const row = document.createElement("div");
    row.className = "model-row";
    const nm = document.createElement("input");
    nm.className = "set-input m-name";
    nm.value = m.name || "";
    nm.placeholder = "显示名";
    nm.addEventListener("input", () => { m.name = nm.value; });
    const id = document.createElement("input");
    id.className = "set-input m-id mono";
    id.value = m.id || "";
    id.placeholder = "模型 ID（API 名）";
    id.addEventListener("input", () => { m.id = id.value; });
    const tg = document.createElement("input");
    tg.className = "set-input m-tags";
    tg.value = (m.tags || []).join(",");
    tg.placeholder = "标签（逗号分隔，如 视觉,1M）";
    tg.addEventListener("input", () => {
      m.tags = tg.value.split(/[,，]/).map(x => x.trim()).filter(Boolean);
    });
    const delB = document.createElement("button");
    delB.type = "button";
    delB.className = "m-del";
    delB.textContent = "✕";
    delB.dataset.tip = "删除模型";
    delB.onclick = () => {
      p.models = p.models.filter(x => x !== m);
      row.remove();
    };
    row.append(nm, id, tg, delB);
    return row;
  };
  for (const m of (p.models || [])) rows.appendChild(buildModelRow(m));
  mField.appendChild(rows);
  const addM = document.createElement("button");
  addM.type = "button";
  addM.className = "m-add";
  addM.textContent = "＋ 添加模型";
  addM.onclick = () => {
    const m = { id: "", name: "", tags: [] };
    p.models.push(m);
    rows.appendChild(buildModelRow(m));
  };
  mField.appendChild(addM);
  card.appendChild(mField);
  return card;
}

$("btn-add-provider").onclick = () => {
  const id = "prov-" + Date.now().toString(36);
  state.providerCfg.providers.push({
    id, name: "新供应商", base_url: "", api_key: "", enabled: true,
    models: [{ id: "", name: "", tags: [] }],
  });
  renderProviderSettings();
};

/* ---------- 设置 → 外观: 应用图标上传 / 恢复默认 ---------- */
$("btn-icon-upload").onclick = () => $("icon-file").click();
$("icon-file").addEventListener("change", () => {
  const file = $("icon-file").files[0];
  $("icon-file").value = "";   // 清空: 允许重复选择同一文件
  if (!file) return;
  if (file.size > 384 * 1024) { toast("图片过大（限 384KB）"); return; }
  const rd = new FileReader();
  rd.onload = async () => {
    const data = String(rd.result || "");
    if (!/^data:image\/(png|jpeg|webp);base64,/.test(data)) {
      toast("仅支持 PNG / JPEG / WebP");
      return;
    }
    try {
      const r = await fetch("/api/icon", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || "HTTP " + r.status);
      iconVer = (await r.json()).ver;
      applyIconEverywhere(data);   // dataURL 即时生效, 无缓存问题
      toast("图标已更新");
    } catch (e) { toast("图标更新失败: " + e.message); }
  };
  rd.readAsDataURL(file);
});
$("btn-icon-reset").onclick = async () => {
  try {
    const r = await fetch("/api/icon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: null }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "HTTP " + r.status);
    iconVer = (await r.json()).ver;
    applyIconEverywhere(iconUrl());
    toast("已恢复默认图标");
  } catch (e) { toast("恢复失败: " + e.message); }
};

/* ---------- 设置 → 配置文件: 用系统默认编辑器打开 settings.json ---------- */
$("btn-open-config").onclick = async () => {
  try {
    const r = await fetch("/api/open-config", { method: "POST" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    toast("已打开配置文件");
  } catch (e) { toast("打开失败: " + e.message); }
};
$("btn-save-providers").onclick = saveProviders;
async function saveProviders() {
  // 接口地址必填: 留空会回退到错误的服务端点, 是 403 类问题的根源
  for (const p of (state.providerCfg?.providers || [])) {
    if (p.enabled !== false && !(p.base_url || "").trim()) {
      toast(`供应商「${p.name}」缺少接口地址 Base URL`);
      return;
    }
  }
  try {
    const r = await fetch("/api/providers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.providerCfg),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast("保存失败: " + (err.detail || r.status));
      return;
    }
    state.providerCfg = await r.json();
    renderProviderSettings();
    await loadSettings();      // 刷新 composer 模型下拉与当前选中
    toast("模型配置已保存");
  } catch (e) { toast("保存失败: " + e.message); }
}

async function saveSettings(patch) {
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast("设置失败: " + (err.detail || r.status));
      await loadSettings();   // 回显真实值
      return;
    }
    const s = await r.json();
    modeDd.setValue(s.permission_mode);
    thinkDd.setValue(s.thinking_level);
  } catch (e) { toast("设置失败: " + e.message); }
}

/* ============================================================
 * 设置弹窗: 主题外观（深色 / 浅色 / 跟随系统）
 * ============================================================ */
const THEME_ITEMS = [
  { value: "dark", label: "深色" },
  { value: "light", label: "浅色" },
  { value: "system", label: "跟随系统" },
];
const themeDd = makeDropdown($("sel-theme"), {
  items: THEME_ITEMS, value: themePref(),
  onChange: v => {
    localStorage.setItem(THEME_KEY, v);
    applyTheme();
    applyAccentVars();   // 自定义色的派生令牌跟随深浅主题
    syncAccentInput();
  },
});

/* ---------- 强调颜色: 预设色板 + 自定义调色盘, 覆盖 --accent 令牌 ---------- */
const ACCENT_KEY = "xc-accent";
const ACCENT_CUSTOM_KEY = "xc-accent-custom";
const ACCENTS = [
  { id: "gray",   name: "浅灰", dark: "#b9bec7", light: "#757b85" },
  { id: "blue",   name: "蓝色", dark: "#6aa6ff", light: "#2f6fd6" },
  { id: "violet", name: "靛紫", dark: "#8b80f9", light: "#6a5be0" },
  { id: "green",  name: "翠绿", dark: "#4ade80", light: "#17803b" },
  { id: "orange", name: "暖橙", dark: "#f97316", light: "#e5622a" },
  { id: "rose",   name: "玫红", dark: "#fb7185", light: "#cf3a5c" },
];
function accentPref() { return localStorage.getItem(ACCENT_KEY) || "gray"; }
function customAccent() { return localStorage.getItem(ACCENT_CUSTOM_KEY) || "#8b80f9"; }

function hexToRgb(hex) {
  const m = /^#?([0-9a-fA-F]{6})$/.exec(hex.trim());
  if (!m) return { r: 139, g: 128, b: 249 };
  const n = parseInt(m[1], 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}
function mixHex(a, b, w) {   // w = b 的权重
  const A = hexToRgb(a), B = hexToRgb(b);
  const ch = (x, y) => Math.round(x + (y - x) * w);
  return "#" + [ch(A.r, B.r), ch(A.g, B.g), ch(A.b, B.b)]
    .map(v => v.toString(16).padStart(2, "0")).join("");
}
/* 自定义颜色: 由主色派生 deep/soft/border（深浅主题各自规则），内联覆盖令牌 */
function applyAccentVars() {
  const st = document.documentElement.style;
  if (accentPref() !== "custom") {
    ["--accent", "--accent-deep", "--accent-soft", "--accent-border"]
      .forEach(p => st.removeProperty(p));
    return;
  }
  const c = customAccent();
  const { r, g, b } = hexToRgb(c);
  const dark = document.documentElement.dataset.theme === "dark";
  st.setProperty("--accent", c);
  st.setProperty("--accent-deep", mixHex(c, "#000000", dark ? 0.16 : 0.2));
  st.setProperty("--accent-soft",
    dark ? `rgba(${r}, ${g}, ${b}, .13)` : mixHex(c, "#ffffff", 0.9));
  st.setProperty("--accent-border",
    dark ? `rgba(${r}, ${g}, ${b}, .36)` : mixHex(c, "#ffffff", 0.74));
}
function applyAccent() {
  const v = accentPref();
  if (v === "gray") delete document.documentElement.dataset.accent;
  else document.documentElement.dataset.accent = v;
  applyAccentVars();
}
function renderAccentDots() { }   // 已由 16 进制输入框取代（保留空实现防旧调用）
function syncAccentInput() {
  const inp = $("accent-hex-input");
  if (!inp) return;
  const v = accentPref();
  if (v === "custom") {
    inp.value = customAccent();
  } else {
    const a = ACCENTS.find(x => x.id === v);
    inp.value = a ? (document.documentElement.dataset.theme === "dark" ? a.dark : a.light) : "";
  }
  inp.classList.remove("invalid");
  syncAccentSwatch();
}
/* 色块预览: 取当前生效的 --accent 计算值（预设主题与自定义色都适用） */
function syncAccentSwatch() {
  const sw = $("accent-swatch");
  if (!sw) return;
  const c = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  const hex = /^#[0-9a-fA-F]{6}$/.test(c) ? c.toLowerCase() : "#8b80f9";
  sw.style.background = hex;
  sw.dataset.hex = hex;
}
$("accent-swatch").addEventListener("click", () => {
  const picker = $("accent-color-picker");
  const sw = $("accent-swatch");
  // Chrome 的取色弹层锚定在 input 自身位置: 先把它挪到色块旁, 否则会飞到页面左上角
  const r = sw.getBoundingClientRect();
  picker.style.left = r.left + "px";
  picker.style.top = (r.bottom + 6) + "px";
  const typed = $("accent-hex-input").value.trim();
  picker.value = /^#[0-9a-fA-F]{6}$/.test(typed) ? typed
    : (sw.dataset.hex || "#8b80f9");
  picker.click();
});
$("accent-color-picker").addEventListener("input", ev => {
  const v = ev.target.value.toLowerCase();
  localStorage.setItem(ACCENT_CUSTOM_KEY, v);
  localStorage.setItem(ACCENT_KEY, "custom");
  applyAccent();
  syncAccentInput();
});
$("accent-hex-input").addEventListener("input", () => {
  const inp = $("accent-hex-input");
  let v = inp.value.trim();
  if (v && !v.startsWith("#")) v = "#" + v;
  if (/^#[0-9a-fA-F]{6}$/.test(v)) {
    inp.classList.remove("invalid");
    localStorage.setItem(ACCENT_CUSTOM_KEY, v.toLowerCase());
    localStorage.setItem(ACCENT_KEY, "custom");
    applyAccent();
    syncAccentSwatch();
  } else {
    inp.classList.add("invalid");
  }
});
$("accent-hex-input").addEventListener("keydown", ev => {
  ev.stopPropagation();   // 别让 Enter/Esc 冒泡成全局快捷键
  if (ev.key === "Enter") ev.target.blur();
});
applyAccent();
mqDark.addEventListener("change", () => {
  applyAccentVars();   // 系统深浅切换: 自定义色的派生令牌跟随
  syncAccentInput();
});

/* ---------- 设置页视图切换: 侧栏换设置导航, 主区换设置内容 ---------- */
function openSettings() {
  themeDd.setValue(themePref());   // 每次打开回显当前值
  syncAccentInput();
  loadProviders().then(renderProviderSettings);   // 拉取供应商配置并渲染
  $("sidebar").classList.add("settings-view");
  $("pane").dataset.view = "settings";
}
function closeSettings() {
  $("sidebar").classList.remove("settings-view");
  $("pane").dataset.view = "chat";
  $("input").focus();
}
document.querySelectorAll(".js-open-settings").forEach(b => { b.onclick = openSettings; });
$("btn-settings-back").onclick = closeSettings;

/* ============================================================
 * 初始化引导页: 无可用供应商配置时弹出, 填 API Key 后立即可用
 * ============================================================ */
function openOnboarding() {
  $("pane").dataset.view = "onboarding";
  setTimeout(() => $("ob-key").focus(), 50);
}
async function saveOnboarding() {
  const key = $("ob-key").value.trim();
  const base = $("ob-base").value.trim();
  const model = $("ob-model").value.trim();
  const err = $("ob-err");
  err.textContent = "";
  if (!key) { err.textContent = "请填写 API Key"; return; }
  if (!base) { err.textContent = "请填写接口地址 Base URL"; return; }
  if (!model) { err.textContent = "请填写默认模型"; return; }   // 不发明默认值
  const btn = $("ob-save");
  btn.disabled = true;
  try {
    // 合并进现有配置: 只更新/追加 id=default 的供应商, 不动用户已配的其他条目
    const cfg = state.providerCfg || { providers: [], active: {} };
    const providers = [...(cfg.providers || [])];
    let prov = providers.find(p => p.id === "default");
    if (prov) {
      prov.api_key = key;
      prov.enabled = true;
      if (base) prov.base_url = base;
      prov.models = prov.models || [];
      if (!prov.models.some(m => m.id === model)) prov.models.push({ id: model, name: model, tags: [] });
    } else {
      providers.push({
        id: "default", name: "默认供应商",
        base_url: base, api_key: key, enabled: true,
        models: [{ id: model, name: model, tags: [] }],
      });
    }
    const r = await fetch("/api/providers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providers, active: { provider: "default", model } }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    state.providerCfg = await r.json();
    state.configured = true;
    syncModelDropdown("default", model);
    $("pane").dataset.view = "chat";
    startDraft();   // 配置完成: 跳转新任务欢迎页
    $("input").focus();
  } catch (e) {
    err.textContent = "保存失败: " + e.message;
  } finally {
    btn.disabled = false;
  }
}
$("ob-save").onclick = saveOnboarding;
$("ob-key").addEventListener("keydown", ev => {
  ev.stopPropagation();
  if (ev.key === "Enter") saveOnboarding();
});
/* 设置导航项切换（当前只有"外观"一节, 结构预留多节扩展） */
document.querySelectorAll("#side-settings .nav-item").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll("#side-settings .nav-item")
      .forEach(x => x.classList.toggle("on", x === b));
    document.querySelectorAll("#settings-page .sp-section-body").forEach(s => {
      s.style.display = s.dataset.section === b.dataset.section ? "" : "none";
    });
  };
});
document.addEventListener("keydown", ev => {
  if ($("pane").dataset.view !== "settings") return;
  if (ev.key === "Escape") { ev.preventDefault(); closeSettings(); }
});

/* ============================================================
 * 统一悬浮提示: 接管原生 title（系统白框提示又慢又丑, 也无法换肤）
 * 悬停带 title 的元素时显示玻璃小气泡; 悬浮期间临时摘掉原生 title
 * ============================================================ */
const tip = document.createElement("div");
tip.id = "tip";
document.body.appendChild(tip);
let tipTimer = null, tipAnchor = null, tipSaved = null;

function tipShow(el) {
  const nativeTitle = el.getAttribute("title");
  const text = nativeTitle || el.getAttribute("data-tip") || "";
  if (!text) return;
  tipSaved = { el, title: nativeTitle || null };   // 仅原生 title 需要压住/还原;
  if (nativeTitle) el.removeAttribute("title");    // data-tip 不能回写 title, 否则动态
  tip.textContent = text;                          // 改文案后会被旧 title 永远盖住
  tip.classList.add("show");
  const r = el.getBoundingClientRect();
  const x = Math.max(8, Math.min(r.left + r.width / 2 - tip.offsetWidth / 2, window.innerWidth - tip.offsetWidth - 8));
  let y = r.top - tip.offsetHeight - 7;   // 默认在元素上方
  if (y < 8) y = r.bottom + 7;            // 顶部放不下: 移到下方
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function tipHide() {
  if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
  tip.classList.remove("show");
  if (tipSaved) {
    if (tipSaved.title) tipSaved.el.setAttribute("title", tipSaved.title);
    tipSaved = null;
  }
}
document.addEventListener("mouseover", ev => {
  const el = ev.target.closest("[title], [data-tip]");
  if (el === tipAnchor) return;   // 在同一元素内移动: 不重置计时
  // 移到当前锚点内部没有提示的子元素: 保持现状, 避免"摘title/还原"抖动给原生提示钻空子
  if (!el && tipAnchor && tipAnchor.contains(ev.target)) return;
  tipAnchor = el;
  tipHide();
  if (!el) return;
  tipTimer = setTimeout(() => { tipTimer = null; tipShow(el); }, 350);
});
document.addEventListener("mousedown", () => { tipAnchor = null; tipHide(); }, true);
window.addEventListener("blur", tipHide);
document.addEventListener("scroll", tipHide, true);

/* ============================================================
 * 启动
 * ============================================================ */
(async function init() {
  await loadSettings();
  await loadSessions();
  // 默认选最近的会话（列表已倒序，第一个即最新）; 没有会话则进入草稿态
  const first = state.sessions[0];
  if (first) await selectSession(first.id);
  else startDraft();
  if (state.configured === false) openOnboarding();   // 首次使用: 先引导配置供应商
  $("input").focus();
})();
