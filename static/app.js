"use strict";
/* 桌面态判定: 壳注入的标记优先, URL 参数 desktop=1 兜底——
 * initialization_script 偶发不注入时菜单/标题栏照常工作 */
const DESKTOP = window.xcodeDesktop
  || new URLSearchParams(location.search).has("desktop");
/* ============================================================
 * 入口守卫: 网页入口已关闭, 仅允许 x-code 桌面壳打开
 * （桌面壳注入 window.xcodeDesktop 标记或 URL 带 desktop=1;
 *   浏览器直接访问 127.0.0.1:8000 只会看到提示, 应用不初始化）
 * ============================================================ */
if (!DESKTOP) {
  document.documentElement.innerHTML =
    '<head><meta charset="UTF-8"><title>x-code</title></head>' +
    '<body style="margin:0;background:#101014">' +
    '<div style="height:100vh;display:flex;flex-direction:column;gap:10px;' +
    'align-items:center;justify-content:center;font-family:system-ui,' +
    '"Microsoft YaHei",sans-serif;color:#a0a1ab;font-size:15px">' +
    '<img src="/api/icon" alt="" style="width:56px;height:56px;' +
    'border-radius:14px;object-fit:cover">' +
    "<div>请通过 x-code 桌面应用打开</div></div></body>";
  throw new Error("x-code: 网页入口已关闭, 请使用桌面应用");
}
if (DESKTOP) document.documentElement.classList.add("xcode-desktop");
/* ============================================================
 * 状态
 * ============================================================ */
const $ = id => document.getElementById(id);
const state = {
  sessionId: null,          // 当前"可见"的会话 id（null = 草稿/无）
  sessions: [],             // 全量会话列表（loadSessions 填充）
  sideTab: "project",       // 侧栏列表模式: project | group
  draft: false,             // 草稿态: 已点"新建"但还没发首条消息（不建条目）
  draftAttach: [],          // 草稿态未发送的附件（跟随会话切换）
  draftInput: "",           // 草稿态未发送的输入（跟随会话切换）
  draftDir: null,           // 草稿态预选的项目目录（侧栏项目行 + 进入时带上）
  draftMode: null,          // 草稿态预选的权限模式: 建会话后先于首条消息下发
  serverWorkspace: null,    // 服务进程工作区名（无会话目录时的兜底展示）
  // 三设置的全局默认值（loadSettings 从 /api/settings 填充）: 无自己覆盖值的
  // 会话/草稿态, 下拉框回落到这里——否则会残留上一个会话的显示值,
  // 与该会话实际用的值不一致（用户看到的"串值"大多是这条路径）
  globalDefaults: { permissionMode: "prompt", thinkingLevel: null, modelKey: null },
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
      pendingPerms: {},       // 待审批的 permission_request: request_id → msg
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
      permissionMode: null,   // 该会话生效的权限模式（mode_changed / 切会话回显）
      thinkingLevel: null,    // 该会话生效的思考等级（thinking_changed / 切会话回显）
      modelKey: null,         // 该会话生效的模型 "provider_id|model_id"（model_changed / 切会话回显）
      loaded: false,          // 历史是否已加载过（首次切入必拉）
      loading: false,         // 历史加载进行中（防并发重复拉取）
      everConnected: false,   // 该会话 WS 是否成功连过（区分首次连接与断线重连）
      reconnected: false,     // 当前连接是否重连（busy_sync 的 busy=false 校正只信重连）
      rlNote: null,           // 限流退避提示行（原地更新, 轮次有进展/收口即撤）
      awaiting: false,        // 忙碌中且正处于等待模型输出的空窗（await_output 起止）
      awaitT0: null,          // 空窗起点（客户端）: 底部转圈的已耗时计时
    };
  }
  return state.runs[id];
}
const curRun = () => (state.sessionId ? runOf(state.sessionId) : null);

/* 手动添加的项目 / 项目折叠状态: localStorage 持久化 */
state.customProjects = JSON.parse(localStorage.getItem("xc-projects") || "[]");
state.collapsedProjects = new Set(JSON.parse(localStorage.getItem("xc-collapsed") || "[]"));
state.draftInput = "";   // 草稿态未发送的输入

/* ============================================================
 * 侧栏拖拽调宽: 左会话栏(--side-w) / 右计划面板(--plan-w)
 * 拖边实时改 CSS 变量, 松手存 localStorage, 双击手柄复位默认值
 * ============================================================ */
const SIDE_W = { min: 200, max: 480, reserve: 420, key: "xc-side-w", prop: "--side-w" };
const PLAN_W = { min: 300, max: 720, reserve: 420, key: "xc-plan-w", prop: "--plan-w" };

function applyColWidth(pref, px) {
  const clamped = Math.max(pref.min,
    Math.min(px, pref.max, window.innerWidth - pref.reserve));
  document.documentElement.style.setProperty(pref.prop, Math.round(clamped) + "px");
  return Math.round(clamped);
}
function restoreColWidth(pref) {
  const saved = Number(localStorage.getItem(pref.key));
  if (saved >= pref.min) {
    document.documentElement.style.setProperty(pref.prop, saved + "px");
  }
}
function attachColResize(handle, panel, pref, dir) {
  if (!handle || !panel) return;   // loading 页等无此结构
  handle.addEventListener("dblclick", () => {
    localStorage.removeItem(pref.key);
    document.documentElement.style.removeProperty(pref.prop);
  });
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    const startX = e.clientX;
    const startW = panel.getBoundingClientRect().width;
    handle.classList.add("dragging");
    document.body.classList.add("col-resizing");
    const move = (ev) =>
      applyColWidth(pref, startW + (ev.clientX - startX) * dir);
    const up = () => {
      handle.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      localStorage.setItem(pref.key, String(
        panel.getBoundingClientRect().width));
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
}
restoreColWidth(SIDE_W);
restoreColWidth(PLAN_W);
attachColResize($("side-resize"), $("sidebar"), SIDE_W, +1);
attachColResize($("plan-resize"), $("plan-panel"), PLAN_W, -1);

/* 输入框内容跟随会话: 切走前保存, 切回后恢复 */
function saveCurrentInput() {
  const v = $("input").value;
  const atts = attachDraftOf();
  if (state.draft) {
    state.draftInput = v;
    state.draftAttach = atts;
  } else if (state.sessionId) {
    const run = runOf(state.sessionId);
    run.inputDraft = v;
    run.attachDraft = atts;
  }
}
function restoreCurrentInput() {
  const v = state.draft ? state.draftInput
    : (state.sessionId ? runOf(state.sessionId).inputDraft : "");
  const atts = state.draft ? state.draftAttach
    : (state.sessionId ? runOf(state.sessionId).attachDraft : null);
  $("input").value = v || "";
  setAttachDraft(atts || []);
  autoGrow($("input"));
  updateSendBtn();
}

/* ============================================================
 * 附件（图片 / 文本文件）: 暂存 → 预览 → 随 user 消息内联发送。
 * 本地不做任何图像识别: 图片经 Canvas 压缩后 base64 内联在 WS 消息里,
 * 后端包成 Anthropic image 内容块, 由视觉模型在服务端看图。
 * ============================================================ */
const ATTACH_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
/* 文本附件扩展白名单: 命中才读入内容, 其余类型 toast 拒绝 */
const ATTACH_TEXT_EXTS = [
  "txt", "md", "markdown", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx",
  "json", "csv", "tsv", "log", "xml", "yml", "yaml", "html", "htm", "css",
  "scss", "less", "sh", "bash", "bat", "cmd", "ps1", "sql", "ini", "toml",
  "cfg", "conf", "env", "java", "c", "h", "cpp", "hpp", "go", "rs", "rb",
  "php", "swift", "kt", "vue", "svg", "diff", "patch",
];
const ATTACH_MAX_IMAGES = 8;
const ATTACH_MAX_FILES = 8;
const ATTACH_MAX_FILE_CHARS = 512 * 1024;   // 与后端 _parse_attachments 上限对齐

/* 附件草稿读写: 与输入文字同一节奏（切会话保存/恢复, 发送后清空） */
function attachDraftOf() {
  return state.draft ? state.draftAttach
    : (state.sessionId ? (runOf(state.sessionId).attachDraft || []) : []);
}
function setAttachDraft(list) {
  if (state.draft) state.draftAttach = list;
  else if (state.sessionId) runOf(state.sessionId).attachDraft = list;
  renderAttachPreview();
  updateSendBtn();
}

function extOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}

/* 本地排队 qid: 与后端排队区对齐, 接力/立即/删除都按它配对 */
function genQid() {
  return "q-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error || new Error("read failed"));
    r.readAsDataURL(file);
  });
}
function readAsText(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error || new Error("read failed"));
    r.readAsText(file);
  });
}
function loadImageEl(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("decode failed"));
    img.src = src;
  });
}

/* Canvas 压缩: 长边 >2000px 或体积 >1MB 时缩放并重编码 webp(quality 0.85);
 * gif 重编码会丢动画帧, 恒走原图; 解码/编码任何一步失败都回退原图。
 * 输出 {kind:"image", name, media_type, data(base64 无头)} */
async function compressImage(file) {
  const dataUrl = await readAsDataURL(file);
  const strip = s => s.slice(s.indexOf(",") + 1);
  const keep = () => ({
    kind: "image", name: file.name,
    media_type: ATTACH_IMAGE_TYPES.includes(file.type) ? file.type : "image/png",
    data: strip(dataUrl),
  });
  if (file.type === "image/gif") return keep();   // 动图不重编码
  let img;
  try { img = await loadImageEl(dataUrl); } catch (e) { return keep(); }
  const longSide = Math.max(img.naturalWidth, img.naturalHeight);
  if (longSide <= 2000 && file.size <= 1024 * 1024) return keep();
  try {
    const scale = longSide > 2000 ? 2000 / longSide : 1;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    let out = canvas.toDataURL("image/webp", 0.85);
    let media = "image/webp";
    if (!out.startsWith("data:image/webp")) {
      // 浏览器不支持 webp 编码时 toDataURL 静默回退 png
      out = canvas.toDataURL("image/png");
      media = "image/png";
    }
    return { kind: "image", name: file.name, media_type: media, data: strip(out) };
  } catch (e) {
    return keep();   // 编码失败: 回退原图
  }
}

/* 附件批量入口: 📎 / 拖拽 / 粘贴 三个入口都汇到这里。
 * 白名单外类型 toast 拒绝; 超 个数/大小 限制时提示并跳过。 */
async function addFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const draft = attachDraftOf();
  let added = 0;
  for (const f of files) {
    const ext = extOf(f.name);
    if (ATTACH_IMAGE_TYPES.includes(f.type)) {
      if (draft.filter(a => a.kind === "image").length >= ATTACH_MAX_IMAGES) {
        toast("图片最多 " + ATTACH_MAX_IMAGES + " 张");
        break;
      }
      try { draft.push(await compressImage(f)); added++; }
      catch (e) { toast("图片读取失败: " + f.name); }
    } else if (ATTACH_TEXT_EXTS.includes(ext)) {
      if (draft.filter(a => a.kind === "file").length >= ATTACH_MAX_FILES) {
        toast("文本附件最多 " + ATTACH_MAX_FILES + " 个");
        break;
      }
      try {
        const text = await readAsText(f);
        if (text.length > ATTACH_MAX_FILE_CHARS) {
          toast("文件过大（内容超 512KB）: " + f.name);
          continue;
        }
        draft.push({ kind: "file", name: f.name, text });
        added++;
      } catch (e) { toast("文件读取失败: " + f.name); }
    } else {
      toast("不支持的文件类型: " + f.name);
    }
  }
  if (added) {
    setAttachDraft(draft);
    saveCurrentInput();
  }
}

function removeAttachment(idx) {
  const draft = attachDraftOf().slice();
  draft.splice(idx, 1);
  setAttachDraft(draft);
  saveCurrentInput();
}

/* 文件 chip（名 + 可选大小）: 预览行 / 用户气泡共用 */
function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}
function fileChipEl(name, sizeBytes) {
  const chip = document.createElement("span");
  chip.className = "file-chip";
  chip.innerHTML = ICON_FILE + '<span class="fc-name"></span>'
    + (sizeBytes ? '<span class="fc-size"></span>' : "");
  chip.querySelector(".fc-name").textContent = name;
  const sizeEl = chip.querySelector(".fc-size");
  if (sizeEl) sizeEl.textContent = fmtBytes(sizeBytes);
  return chip;
}

/* 预览行: 图片缩略图 + 文件 chip, 每项带 × 删除钮 */
function renderAttachPreview() {
  const box = $("attach-preview");
  if (!box) return;
  box.innerHTML = "";
  const draft = attachDraftOf();
  const imgAtts = draft.filter(a => a.kind === "image");   // 传整组给灯箱, 支持左右切换
  draft.forEach((att, idx) => {
    const item = document.createElement("div");
    item.className = "att-item";
    if (att.kind === "image") {
      const img = document.createElement("img");
      img.className = "att-thumb";
      img.alt = att.name || "";
      img.title = "点击查看大图";
      img.src = "data:" + (att.media_type || "image/png") + ";base64," + att.data;
      img.onclick = () => openLightbox(imgAtts, imgAtts.indexOf(att));
      item.appendChild(img);
    } else {
      item.classList.add("att-file");
      item.appendChild(fileChipEl(att.name, att.text ? att.text.length : 0));
    }
    const del = document.createElement("button");
    del.type = "button";
    del.className = "att-del";
    del.innerHTML = "&#215;";
    del.dataset.tip = "移除";
    del.onclick = () => removeAttachment(idx);
    item.appendChild(del);
    box.appendChild(item);
  });
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
  // 列显隐不影响 #messages 自身盒尺寸, ResizeObserver 不会触发;
  // 必须显式重建, 否则 minimap 留着上一个会话的刻度
  mmScheduleRebuild();
}

/* ============================================================
 * 完成通知: 提示音(WebAudio 合成) + 系统桌面通知, localStorage 持久化
 * 值: "1" 开 / "0" 关; 未设置时默认开
 * ============================================================ */
const NOTIFY_SOUND_KEY = "xc-notify-sound";
const NOTIFY_DESKTOP_KEY = "xc-notify-desktop";
function notifySoundPref() { return localStorage.getItem(NOTIFY_SOUND_KEY) !== "0"; }
function notifyDesktopPref() { return localStorage.getItem(NOTIFY_DESKTOP_KEY) !== "0"; }

/* 双音阶提示音: 正弦波 + 指数衰减, 约 0.6s。AudioContext 必须在用户手势
 * 之后才能出声——首次交互时 resume 预热, 之后轮次结束可直接播。 */
let _chimeCtx = null;
function chimeCtx() {
  if (!_chimeCtx) _chimeCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (_chimeCtx.state === "suspended") _chimeCtx.resume();
  return _chimeCtx;
}
document.addEventListener("pointerdown", () => { try { chimeCtx(); } catch {} }, { once: true });

function playChime() {
  try {
    const ctx = chimeCtx();
    const t0 = ctx.currentTime;
    // 两声上行（E5→A5）: 干完活上扬收尾, 比"叮"单音更醒目又不刺耳
    [[659.25, 0], [880, 0.18]].forEach(([freq, offset]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t0 + offset);
      gain.gain.exponentialRampToValueAtTime(0.22, t0 + offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + offset + 0.5);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0 + offset);
      osc.stop(t0 + offset + 0.55);
    });
  } catch (e) { console.warn("提示音播放失败", e); }
}

/* 桌面通知权限: 只在用户手势上下文（开关/测试按钮）里申请, 静默页面
 * 自动弹权限框会被浏览器拒掉。返回当前权限态。
 * 桌面壳形态恒为 "granted"——原生 toast 走壳内命令, 无 Web 权限一说。 */
function ensureNotifyPermission() {
  if (DESKTOP) return "granted";
  if (!("Notification" in window)) return "denied";
  if (Notification.permission === "default") Notification.requestPermission();
  return Notification.permission;
}

/* 桌面壳里的原生通知: WebView2 未实现 Web Notification API,
 * new Notification() 在壳里静默失败——转发给壳内 notify_desktop 命令
 * 发系统 toast。老壳没有该命令(或桥不在)时 invoke 被拒, 静默降级。 */
function nativeNotify(title, body) {
  try {
    const inv = window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke;
    if (inv) inv("notify_desktop", { title, body }).catch(() => {});
  } catch (e) { /* 桥不在(浏览器/老壳): 静默 */ }
}

/* 轮次收尾统一通知入口（turn_done / error, 前台后台会话都经过这里）:
 * 手动打断不提醒; 声音开关开了就播; 桌面弹窗只在窗口不可见或该会话
 * 在后台时弹——人正盯着这个会话时不打扰。
 * 桌面壳走原生 toast(无点击回调, 点通知不聚焦——只做告知); 浏览器形态
 * 维持 Web Notification(带点击聚焦+切会话)。 */
function notifyTurnEnd(msg, sid) {
  if (msg.type === "turn_done" && msg.interrupted) return;
  if (notifySoundPref()) playChime();
  if (!notifyDesktopPref()) return;
  const visibleHere = sid === state.sessionId
    && document.visibilityState === "visible"
    && !document.hidden;
  if (visibleHere) return;
  const s = state.sessions.find(x => x.id === sid);
  const title = (s?.name || s?.title || "会话") + " · 任务完成";
  const body = msg.type === "error" ? ("出错了: " + (msg.message || "未知错误")) : "本轮已结束, 回来看看结果";
  if (DESKTOP) {
    nativeNotify(title, body);
    return;
  }
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, { body, tag: "xcode-turn-" + sid, silent: true });
    n.onclick = () => {
      window.focus();
      if (sid !== state.sessionId) selectSession(sid);
      n.close();
    };
  } catch (e) { console.warn("桌面通知失败", e); }
}

/* ============================================================
 * 主题: dark | light | system（跟随系统），localStorage 持久化
 * ============================================================ */
const THEME_KEY = "xc-theme";
const mqDark = window.matchMedia("(prefers-color-scheme: dark)");
/* 值: dark | light | system | acrylic（亚克力深色） | acrylic-light（亚克力浅色） */
function themePref() { return localStorage.getItem(THEME_KEY) || "system"; }
function themeIsAcrylic() { return themePref().startsWith("acrylic"); }
function resolvedTheme() {
  const pref = themePref();
  if (pref === "acrylic" || pref === "acrylic-light") return pref === "acrylic" ? "dark" : "light";
  return pref === "system" ? (mqDark.matches ? "dark" : "light") : pref;
}
/* 亚克力主题: 半透明表面 + 窗口级 DWM 材质(桌面透出) + 壁纸透出。
 * 是否亚克力由主题值直接派生(acrylic / acrylic-light), 无独立开关。 */
/* 背景图开关状态: applyFx 启动早期就会经 syncBgLayers 读到, 必须先于此初始化 */
const BG_KEY = "xc-bg";
let bgVer = 0;
function applyFx() {
  // 注意: 不能引入独立开关再读它——曾有过只读不写的 xc-fx key, 导致
  // applyFx 恒走 else 分支把 head 预绘制脚本设置的 data-fx 删掉,
  // 亚克力主题永远不生效。data-fx 的唯一事实来源 = 主题值。
  if (themeIsAcrylic()) document.documentElement.dataset.fx = "acrylic";
  else delete document.documentElement.dataset.fx;
  // 背景图逻辑声明在文件后部; 函数声明会提升, 此时其状态变量已就绪, 可直接调
  syncBgLayers();
}
function applyTheme() {
  document.documentElement.dataset.theme = resolvedTheme();
}
// 跟随系统时, 系统深浅切换实时生效
mqDark.addEventListener("change", () => {
  if (themePref() === "system") { applyTheme(); syncBgLayers(); }
});
applyTheme();
applyFx();

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
function toast(text, ms = 1600) {
  const t = $("toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}
window.xcodeToast = toast;   // 摸鱼电台(music.js)共用同一枚轻提示

/* ============================================================
 * 确认弹窗 — 替代原生 confirm(): WebView2 的 confirm 顶着
 * "127.0.0.1:8000 显示" 的源地址头, 丑且不可定制。
 * 用法: if (await confirmDialog("删除会话「x」？", { title: "删除会话", okText: "删除", danger: true })) ...
 * ============================================================ */
function confirmDialog(msg, { title = "确认操作", okText = "确定", danger = false } = {}) {
  return new Promise(resolve => {
    const ov = document.createElement("div");
    ov.id = "confirm-overlay";
    ov.style.display = "flex";
    ov.innerHTML =
      '<div id="confirm-modal" role="alertdialog" aria-modal="true">' +
        '<div id="confirm-title">' +
          (danger ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>' : "") +
          '<span>' + escapeHtml(title) + '</span>' +
        '</div>' +
        '<div id="confirm-msg">' + escapeHtml(msg) + '</div>' +
        '<div id="confirm-actions">' +
          '<button type="button" data-act="cancel">取消</button>' +
          '<button type="button" data-act="ok"' + (danger ? ' class="danger"' : '') + '>' + escapeHtml(okText) + '</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(ov);
    let done = false;
    const finish = v => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onEsc);
      ov.remove();
      resolve(v);
    };
    const onEsc = e => { if (e.key === "Escape") finish(false); };
    document.addEventListener("keydown", onEsc);
    ov.querySelector("[data-act='ok']").onclick = () => finish(true);
    ov.querySelector("[data-act='cancel']").onclick = () => finish(false);
    ov.addEventListener("mousedown", e => { if (e.target === ov) finish(false); });
    ov.querySelector("[data-act='ok']").focus();
  });
}
window.confirmDialog = confirmDialog;   // 摸鱼电台(music.js)复用

/* ============================================================
 * 输入弹窗 — 替代原生 prompt(): Tauri/WebView2 不支持 prompt
 * （返回 null, 调用方当成"取消"静默退出, 功能看着就是"点了没反应"）。
 * 用法: const name = await promptDialog("歌单名称", { title: "存为歌单", value: "默认值" });
 * ============================================================ */
function promptDialog(msg, { title = "输入", value = "", placeholder = "", okText = "确定" } = {}) {
  return new Promise(resolve => {
    const ov = document.createElement("div");
    ov.id = "confirm-overlay";
    ov.style.display = "flex";
    ov.innerHTML =
      '<div id="confirm-modal" role="dialog" aria-modal="true">' +
        '<div id="confirm-title"><span>' + escapeHtml(title) + '</span></div>' +
        '<div id="confirm-msg">' + escapeHtml(msg) + '</div>' +
        '<input id="confirm-input" type="text" spellcheck="false" autocomplete="off">' +
        '<div id="confirm-actions">' +
          '<button type="button" data-act="cancel">取消</button>' +
          '<button type="button" data-act="ok">' + escapeHtml(okText) + '</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(ov);
    const input = ov.querySelector("#confirm-input");
    input.value = value;
    input.placeholder = placeholder;
    let done = false;
    const finish = v => {
      if (done) return;
      done = true;
      input.removeEventListener("keydown", onEnter);
      document.removeEventListener("keydown", onEsc);
      ov.remove();
      resolve(v);
    };
    const onEnter = e => {
      e.stopPropagation();            // 别漏进全局快捷键
      if (e.key === "Enter") finish(input.value.trim());
    };
    const onEsc = e => { if (e.key === "Escape") finish(null); };
    input.addEventListener("keydown", onEnter);
    document.addEventListener("keydown", onEsc);
    ov.querySelector("[data-act='ok']").onclick = () => finish(input.value.trim());
    ov.querySelector("[data-act='ok']").onclick = () => finish(input.value.trim());
    ov.querySelector("[data-act='cancel']").onclick = () => finish(null);
    setTimeout(() => input.focus(), 0);
  });
}

/* ============================================================
 * 自动更新（仅桌面壳, 经 window.xcodeDesktopUpdater 桥调 Rust 命令）:
 *  - 启动后静默检查一次（check_update）; 发现新版本 → 版本徽标加红点 + toast 提示
 *  - 点标题栏版本徽标 = 有更新则打开更新弹窗, 无更新则手动检查一次
 *  - 立即更新 → install_update, 前端 300ms 轮询 update_status 画进度条;
 *    下载完自动拉起 NSIS 安装器（passive）, 壳重启到新版本
 * 失败可见性: 手动检查失败必提示; 启动静默检查失败不弹（可能只是没网）。
 * ============================================================ */
const UPD = {
  checking: false,
  info: null,        // check_update 返回 { hasUpdate, currentVersion, version, notes }
  pollTimer: null,
};

function fmtMB(n) {
  if (!n) return "?";
  return (n / 1048576).toFixed(1) + " MB";
}

function updateBadgeMark(on, verText) {
  const badge = $("tb-version");
  if (!badge) return;
  badge.classList.toggle("up", !!on);
  badge.title = on ? `发现新版本 v${verText}, 点击更新` : "检查更新";
}

async function checkForUpdates(manual = false) {
  if (!window.xcodeDesktopUpdater || UPD.checking) return null;
  UPD.checking = true;
  try {
    const info = await window.xcodeDesktopUpdater.check();
    UPD.info = info;
    if (info && info.hasUpdate) {
      updateBadgeMark(true, info.version);
      if (manual) openUpdateDialog();
      else toast(`发现新版本 v${info.version}, 点击标题栏版本号更新`, 3600);
    } else {
      updateBadgeMark(false);
      if (manual) toast(`已是最新版本（v${info.currentVersion}）`);
    }
    return info;
  } catch (e) {
    if (manual) toast("检查更新失败: " + (e?.message || e));
    return null;
  } finally {
    UPD.checking = false;
  }
}

function initUpdateCheck() {
  if (!window.xcodeDesktopUpdater) return;   // 浏览器/源码运行: 无壳
  const badge = $("tb-version");
  if (badge) badge.addEventListener("click", () => {
    if (UPD.info && UPD.info.hasUpdate) openUpdateDialog();
    else checkForUpdates(true);
  });
  checkForUpdates(false);   // 启动静默检查
}

function openUpdateDialog() {
  const info = UPD.info;
  if (!info || !info.hasUpdate || $("update-overlay")) return;
  const ov = document.createElement("div");
  ov.id = "update-overlay";
  ov.style.display = "flex";
  ov.innerHTML =
    '<div id="update-modal" role="alertdialog" aria-modal="true">' +
      '<div id="update-title">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-9-9"/><path d="M21 3v6h-6"/></svg>' +
        '<span>发现新版本</span>' +
      '</div>' +
      '<div id="update-versions">v' + escapeHtml(info.currentVersion || "?") +
        ' <span class="upd-arrow">→</span> v' + escapeHtml(info.version || "?") + '</div>' +
      '<div id="update-notes">' + (info.notes ? escapeHtml(info.notes) : "") + '</div>' +
      '<div id="update-progress" hidden><div id="update-progress-bar"></div></div>' +
      '<div id="update-status-text"></div>' +
      '<div id="update-actions">' +
        '<button type="button" data-act="cancel">以后再说</button>' +
        '<button type="button" data-act="ok">立即更新</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(ov);
  const onEsc = e => {
    // 下载中不允许关（Rust 侧无取消接口, 关了下载还在跑只会更困惑）
    if (e.key === "Escape" && !ov.dataset.busy) closeUpdateDialog(ov);
  };
  ov._onEsc = onEsc;
  document.addEventListener("keydown", onEsc);
  ov.addEventListener("mousedown", e => { if (e.target === ov && !ov.dataset.busy) closeUpdateDialog(ov); });
  ov.querySelector("[data-act='cancel']").onclick = () => { if (!ov.dataset.busy) closeUpdateDialog(ov); };
  ov.querySelector("[data-act='ok']").onclick = () => startUpdateInstall(ov);
  ov.querySelector("[data-act='ok']").focus();
}

function closeUpdateDialog(ov) {
  if (UPD.pollTimer) { clearInterval(UPD.pollTimer); UPD.pollTimer = null; }
  document.removeEventListener("keydown", ov._onEsc || (() => {}));
  ov.remove();
}

function updateInstallFail(ov, msg) {
  if (UPD.pollTimer) { clearInterval(UPD.pollTimer); UPD.pollTimer = null; }
  delete ov.dataset.busy;
  const st = ov.querySelector("#update-status-text");
  st.textContent = "更新失败: " + msg;
  st.classList.add("upd-err");
  // 允许关掉重试（下次点徽标重新检查）
  const actions = ov.querySelector("#update-actions");
  actions.hidden = false;
  actions.querySelector("[data-act='ok']").hidden = true;
  actions.querySelector("[data-act='cancel']").textContent = "关闭";
}

async function startUpdateInstall(ov) {
  const bar = ov.querySelector("#update-progress");
  const fill = ov.querySelector("#update-progress-bar");
  const status = ov.querySelector("#update-status-text");
  const actions = ov.querySelector("#update-actions");
  actions.hidden = true;          // 下载不可取消, 也不许 ESC/点遮罩关
  bar.hidden = false;
  ov.dataset.busy = "1";
  try {
    await window.xcodeDesktopUpdater.install();
  } catch (e) {
    return updateInstallFail(ov, (e?.message || e) || "无法启动下载");
  }
  // install_update 立即返回（Rust 侧异步任务在跑）, 进度靠轮询
  UPD.pollTimer = setInterval(async () => {
    let st;
    try { st = await window.xcodeDesktopUpdater.status(); } catch (_) { return; }
    if (st.phase === 3) return updateInstallFail(ov, "下载或安装出错（详见 ~/.x-code/boot.log）");
    if (st.phase === 2) {
      fill.style.width = "100%";
      status.textContent = "下载完成, 正在启动安装程序…";
      return;
    }
    if (st.total > 0) {
      const pct = Math.min(100, Math.round((st.received / st.total) * 100));
      fill.style.width = pct + "%";
      status.textContent = `下载中 ${pct}%（${fmtMB(st.received)} / ${fmtMB(st.total)}）`;
    } else {
      status.textContent = "正在连接更新服务器…";
    }
  }, 300);
}

/* ============================================================
 * 桌面端右键菜单 — 复制/粘贴, 只显示当前可用的项:
 * 有选中文字 → 复制; 右键输入框/可编辑区 → 粘贴; 都没有 → 不弹。
 * 自注册 contextmenu 监听（松开右键时触发, 与原生菜单同时机）:
 * Electron 壳无原生右键菜单, preventDefault 只是拦掉默认行为作双保险。
 * 复制走 execCommand（复制用户当前选区, 无需剪贴板写权限）;
 * 粘贴经 preload 桥在主进程读系统剪贴板, insertText 走编辑
 * 命令栈——可撤销, 且正常触发 input 事件。
 * ============================================================ */
function closeCtxMenu() {
  const pop = $("ctx-pop");
  if (pop) pop.remove();
  document.removeEventListener("mousedown", onCtxAway, true);
  document.removeEventListener("keydown", onCtxEsc, true);
}
function onCtxAway(e) {
  if (!e.target.closest || !e.target.closest("#ctx-pop")) closeCtxMenu();
}
function onCtxEsc(e) {
  if (e.key === "Escape") closeCtxMenu();
}

function showDesktopCtxMenu(ev) {
  closeCtxMenu();
  const selText = String(window.getSelection() || "");
  const t = ev.target;
  const editable = t.closest
    ? (t.closest("textarea, input") || (t.isContentEditable ? t : null))
    : null;

  const pop = document.createElement("div");
  pop.id = "ctx-pop";
  const item = (label, key, enabled, fn) => {
    const it = document.createElement("div");
    it.className = "ctx-item" + (enabled ? "" : " disabled");
    const name = document.createElement("span");
    name.textContent = label;
    const hint = document.createElement("span");
    hint.className = "ctx-key";
    hint.textContent = key;
    it.append(name, hint);
    if (enabled) {
      // mousedown 不给默认行为: 不抢焦点、不冲掉选区, 等 click 再执行
      it.addEventListener("mousedown", e => e.preventDefault());
      it.onclick = () => { closeCtxMenu(); fn(); };
    }
    pop.appendChild(it);
  };
  const sep = () => {
    const s = document.createElement("div");
    s.className = "ctx-sep";
    pop.appendChild(s);
  };

  if (editable) {
    // 输入框: 完整编辑菜单（撤销/重做 | 剪切/复制/粘贴/删除 | 全选）
    const hasSel = editable.setSelectionRange
      ? editable.selectionStart !== editable.selectionEnd
      : (() => {
          const s = window.getSelection();
          return !!s && !s.isCollapsed && editable.contains(s.anchorNode);
        })();
    const edit = cmd => () => {
      editable.focus();
      document.execCommand(cmd);
    };
    item("撤销", "Ctrl+Z", true, edit("undo"));
    item("重做", "Ctrl+Y", true, edit("redo"));
    sep();
    item("剪切", "Ctrl+X", hasSel, edit("cut"));
    item("复制", "Ctrl+C", hasSel, edit("copy"));
    item("粘贴", "Ctrl+V", true, async () => {
      try {
        // Electron: 经 preload 桥在主进程读系统剪贴板（渲染层 execCommand('paste')
        // 受浏览器安全模型限制不可用）; 旧壳无桥时退回 async Clipboard API。
        // insertText 走编辑命令栈——可撤销, 且正常触发 input 事件
        const text = (window.xcodeReadClipboard
          ? await window.xcodeReadClipboard()
          : await navigator.clipboard.readText()) || "";
        editable.focus();
        if (!text) { toast("剪贴板是空的"); return; }
        if (!document.execCommand("insertText", false, text)) toast("粘贴失败");
      } catch (e) { toast("粘贴失败"); }
    });
    item("删除", "Del", hasSel, edit("delete"));
    sep();
    item("全选", "Ctrl+A", true, () => {
      editable.focus();
      if (editable.select) editable.select();
      else document.execCommand("selectAll");
    });
  } else if (selText) {
    // 消息文本: 只有复制一件事可做
    item("复制", "Ctrl+C", true, () => document.execCommand("copy"));
  } else {
    return;   // 无可做的事情就不弹（原生菜单也已被抑制）
  }

  document.body.appendChild(pop);
  const W = pop.offsetWidth, H = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(ev.clientX, window.innerWidth - W - 8)) + "px";
  pop.style.top = Math.max(8, Math.min(ev.clientY, window.innerHeight - H - 8)) + "px";
  setTimeout(() => {   // 当次右键的 mouseup 不许把刚弹出的菜单关掉
    document.addEventListener("mousedown", onCtxAway, true);
    document.addEventListener("keydown", onCtxEsc, true);
  }, 0);
}

// 桌面态由 DESKTOP（注入标记或 URL 参数）直接判定, 注入时序不影响菜单
if (DESKTOP) {
  document.addEventListener("contextmenu", ev => {
    ev.preventDefault();
    showDesktopCtxMenu(ev);
  }, true);
}

/* ============================================================
 * 自绘标题栏（仅桌面壳）— 拖拽/双击最大化/窗口控制按钮
 * ============================================================ */
if (DESKTOP) {
  const tbInvoke = cmd => window.__TAURI_INTERNALS__.invoke(cmd).catch(() => {});
  $("tb-min").onclick = () => tbInvoke("minimize_main");
  $("tb-max").onclick = () => tbInvoke("toggle_maximize_main");
  $("tb-close").onclick = () => tbInvoke("close_main");
  const tbar = $("titlebar");
  // 徽标(.tb-ver)可点击打开更新弹窗, 必须与 .tb-btn 一样排除在拖拽外——
  // 否则 mousedown 触发 start_drag_main 进入系统拖拽循环, click 永远不触发
  const TB_INTERACTIVE = ".tb-btn, .tb-ver";
  tbar.addEventListener("mousedown", e => {
    if (e.button !== 0 || e.target.closest(TB_INTERACTIVE)) return;
    tbInvoke("start_drag_main");
  });
  tbar.addEventListener("dblclick", e => {
    if (e.target.closest(TB_INTERACTIVE)) return;
    tbInvoke("toggle_maximize_main");
  });
}

/* ============================================================
 * 滚动: 贴底自动跟随; 用户上翻时不拽人, 悬浮钮一键回底
 * ============================================================ */
let nearBottom = true;
$("messages").addEventListener("scroll", () => {
  const el = $("messages");
  nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  $("scroll-btn").classList.toggle("show", !nearBottom);
  updateMsgThumb();
  mmUpdateActive();
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

/* ---------- 自绘固定长度滚动条(消息区) ----------
 * 原生滑块长度随内容比例缩放, 无法恒定; 这里隐藏原生条,
 * 滑块固定 64px, 位置按滚动比例映射。拖拽/点轨道反算 scrollTop。 */
const MSG_THUMB_H = 64;
const msgThumb = $("msg-scrollbar");
const msgThumbBar = msgThumb.querySelector(".thumb");

function updateMsgThumb() {
  const el = $("messages");
  const sh = el.scrollHeight, ch = el.clientHeight;
  if (sh - ch <= 2) { msgThumb.hidden = true; return; }   // 不可滚动
  msgThumb.hidden = false;
  /* 轨道与消息区盒子对齐(上下有文档头/输入卡, 不能直接铺满 pane) */
  msgThumb.style.top = el.offsetTop + "px";
  msgThumb.style.height = el.clientHeight + "px";
  const track = msgThumb.clientHeight - MSG_THUMB_H;      // 可移动范围(轨道高-滑块长)
  const max = sh - ch;
  const top = el.scrollTop >= max - 2 ? track             // 贴底判定: 完整显示
    : Math.min(track, Math.round(el.scrollTop / max * track));
  msgThumbBar.style.top = top + "px";
}

/* 内容尺寸变化(流式输出/切会话/窗口变化)时同步滑块 */
new ResizeObserver(() => { updateMsgThumb(); mmScheduleRebuild(); }).observe($("messages"));
window.addEventListener("resize", updateMsgThumb);

/* 拖拽滑块: 按位移比例反算 scrollTop */
msgThumbBar.addEventListener("pointerdown", ev => {
  ev.preventDefault();
  msgThumb.classList.add("dragging");
  msgThumbBar.setPointerCapture(ev.pointerId);
  const el = $("messages");
  const startY = ev.clientY;
  const startScroll = el.scrollTop;
  const track = msgThumb.clientHeight - MSG_THUMB_H;
  const max = el.scrollHeight - el.clientHeight;
  const onMove = e2 => {
    if (track <= 0 || max <= 0) return;
    el.scrollTop = startScroll + (e2.clientY - startY) / track * max;
  };
  const onUp = () => {
    msgThumb.classList.remove("dragging");
    msgThumbBar.removeEventListener("pointermove", onMove);
    msgThumbBar.removeEventListener("pointerup", onUp);
  };
  msgThumbBar.addEventListener("pointermove", onMove);
  msgThumbBar.addEventListener("pointerup", onUp);
});

/* 点击轨道空白: 跳到对应位置(与原生行为一致) */
msgThumb.addEventListener("pointerdown", ev => {
  if (ev.target === msgThumbBar) return;   // 滑块自身走拖拽
  const el = $("messages");
  const track = msgThumb.clientHeight - MSG_THUMB_H;
  const max = el.scrollHeight - el.clientHeight;
  if (track <= 0 || max <= 0) return;
  const y = ev.clientY - msgThumb.getBoundingClientRect().top - MSG_THUMB_H / 2;
  el.scrollTop = Math.max(0, Math.min(track, y)) / track * max;
});

/* ============================================================
 * minimap: 用户消息快速定位
 * 右缘一列刻度, 每条用户消息一个; 固定间距排成一簇、整簇在轨道
 * 垂直居中(消息多到放不下时才摊满整条轨道)。
 * 悬停刻度弹预览卡(摘要 + 相对时间), 点击平滑滚动定位;
 * 滚动时高亮视口中线以上最近的一条。数据源是 addUserBubble
 * 挂在行元素上的 _text/_ts, 无需额外后端结构。
 * ============================================================ */
const mm = $("msg-minimap");
const mmPop = $("mm-pop");
let mmTimer = null;

function mmScheduleRebuild() {
  clearTimeout(mmTimer);
  mmTimer = setTimeout(mmRebuild, 150);
}

function mmRebuild() {
  const el = $("messages");
  mmPop.hidden = true;
  mm.textContent = "";
  // 只收集当前可见会话列的用户气泡: #messages 下常驻着每个会话一个的
  // .msg-col（切会话仅显隐、不销毁）, 若全局 querySelectorAll 会把隐藏
  // 会话的气泡一并收进来——display:none 行的 offsetTop 恒为 0, 刻度全部
  // 堆到轨道顶部并连锁挤乱本会话刻度, 预览/跳转也会指到别的会话。
  const col = el.querySelector(".msg-col.on");
  const rows = col ? [...col.querySelectorAll(".msg.user")] : [];
  const scrollable = el.scrollHeight - el.clientHeight > 2;
  if (!rows.length || !scrollable) { mm.hidden = true; return; }
  mm.hidden = false;
  // 轨道按消息区可视范围垂直居中: 高度收窄到 70%, 上下各留 15%
  const trackH = Math.round(el.clientHeight * 0.7);
  mm.style.top = el.offsetTop + Math.round((el.clientHeight - trackH) / 2) + "px";
  mm.style.height = trackH + "px";
  const H = mm.clientHeight;
  const frag = document.createDocumentFragment();
  // 固定间距排成一簇、整簇垂直居中; 超过轨道容纳量时退化为等距摊满。
  const step = 10;
  const span = (rows.length - 1) * step;
  const gap = rows.length > 1
    ? (span < H ? step : H / (rows.length - 1))
    : 0;
  const top0 = rows.length > 1 ? (H - (rows.length - 1) * gap) / 2 : H / 2;
  rows.forEach((row, i) => {
    const tick = document.createElement("button");
    tick.className = "mm-tick";
    tick.type = "button";
    tick.style.top = (top0 + i * gap) + "px";
    tick._row = row;
    tick._text = mmSummary(row);
    tick._time = mmRelTime(row._ts);
    frag.appendChild(tick);
  });
  mm.appendChild(frag);
  mmUpdateActive();
}

function mmSummary(row) {
  const text = (row._text || "").replace(/\s+/g, " ").trim();
  if (text) return text.length > 140 ? text.slice(0, 140) + "…" : text;
  if (row.querySelector(".u-imgs")) return "[图片]";
  if (row.querySelector(".file-chip")) return "[文件]";
  return "[消息]";
}

function mmRelTime(ts) {
  const t = ts ? new Date(ts) : null;
  if (!t || isNaN(t)) return "";
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 90) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + "分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
  if (diff < 172800) return "1天前";
  if (diff < 604800) return Math.floor(diff / 86400) + "天前";
  return `${t.getFullYear()}/${t.getMonth() + 1}/${t.getDate()}`;
}

/* 激活态: 视口中线以上最近的一条用户消息 */
function mmUpdateActive() {
  if (mm.hidden) return;
  const el = $("messages");
  const mid = el.scrollTop + el.clientHeight / 2;
  let best = null;
  for (const tick of mm.children) {
    if (tick._row && tick._row.offsetTop <= mid) best = tick;
  }
  for (const tick of mm.children) tick.classList.toggle("active", tick === best);
}

/* 刻度交互: 悬停出预览卡(刻度右侧, 视口内钳位), 点击平滑定位;
 * 喷泉波纹: 以指针所指刻度为中心, 向上下两侧递减拉长——
 * 展开量随 |i - h| 每远 1 个刻度衰减 1/3, 3 档外归零;
 * 复位用 removeProperty(不能写内联 0, 会盖掉 .active 的类规则 --f:1) */
const mmText = mmPop.querySelector(".mm-text");
const mmTime = mmPop.querySelector(".mm-time");
function mmFountain(hoverIdx) {
  [...mm.children].forEach((t, i) => {
    if (hoverIdx == null) {
      t.style.removeProperty("--f");
      return;
    }
    const f = Math.max(0, 1 - Math.abs(i - hoverIdx) / 3);
    t.style.setProperty("--f", f.toFixed(3));
  });
}
function mmTickFromEvent(e) {
  const r = mm.getBoundingClientRect();
  const y = e.clientY - r.top;
  let best = -1, bestD = Infinity;
  for (let i = 0; i < mm.children.length; i++) {
    const d = Math.abs(parseFloat(mm.children[i].style.top) - y);
    if (d < bestD) { bestD = d; best = i; }
  }
  return bestD <= 12 ? best : null;   // 稍微离轨也认, 出范围即收回
}
/* 预览卡延迟弹出: 停留 250ms 才显示, 划过不闪卡; 波纹始终实时跟随。
 * 时序关键: "已显示这条"的早退必须在 clearTimeout 之前——卡可见只说明
 * 显示着上一条的内容, 此时往往还有一条指向新刻度的定时器挂起; 若先
 * clear 再早退会把它误杀, 卡片内容就永远停在上一条, 直到鼠标离开重进 */
let mmPopTimer = null, mmPopIdx = null;
function mmPopArm(idx) {
  if (idx == null) {
    clearTimeout(mmPopTimer);
    mmPopIdx = null;
    mmPop.hidden = true;
    return;
  }
  if (idx === mmPopIdx && !mmPop.hidden) return;   // 已显示/已挂起这条: 不动定时器
  clearTimeout(mmPopTimer);
  mmPopIdx = idx;
  mmPopTimer = setTimeout(() => {
    const tick = mm.children[mmPopIdx];
    if (!tick) return;
    mmText.textContent = tick._text || "";
    mmTime.textContent = tick._time || "";
    mmPop.hidden = false;
    const r = tick.getBoundingClientRect();
    const pr = mmPop.getBoundingClientRect();
    let top = r.top + r.height / 2 - pr.height / 2;
    top = Math.max(8, Math.min(window.innerHeight - pr.height - 8, top));
    mmPop.style.top = top + "px";
    mmPop.style.left = (r.right + 12) + "px";
  }, 250);
}
mm.addEventListener("pointermove", e => {
  if (mm.hidden) return;
  const idx = mmTickFromEvent(e);
  mmFountain(idx);
  mmPopArm(idx);
});
mm.addEventListener("pointerleave", () => {
  mmFountain(null);
  clearTimeout(mmPopTimer);
  mmPopIdx = null;
  mmPop.hidden = true;
});
mm.addEventListener("pointerdown", e => {
  // 点击走就近吸附判定(与 hover 同源): 刻度只有 2px 高, e.target 精确
  // 命中率太低; 12px 吸附半径内都算点到, 也与当前波纹所指保持一致
  const idx = mmTickFromEvent(e);
  const tick = idx != null ? mm.children[idx] : null;
  if (!tick || !tick._row) return;
  const row = tick._row;
  $("messages").scrollTo({
    top: Math.max(0, row.offsetTop - $("messages").clientHeight / 2 + row.offsetHeight / 2),
    behavior: "smooth",
  });
});

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
  if (run && isAwaitingPlan(run)) return ICON_MODE_PLAN; // 卡在计划审批
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
    + '<span class="s-plan">待批计划</span>'
    + '<span class="title"></span><span class="meta"></span>'
    + '<button class="s-ren" data-tip="重命名">' + PENCIL_SMALL_SVG + '</button>'
    + '<button class="s-del" data-tip="删除会话">' + TRASH_SMALL_SVG + '</button>';
  item.querySelector(".title").textContent = displayTitle(s);
  item.querySelector(".meta").textContent = relativeTime(s.id);
  const run = state.runs[s.id];
  if (run && run.busy) item.classList.add("running");
  // 卡在计划审批: 元信息让位给"待批计划"徽标, 错过弹窗也能从侧栏看出
  // 这个会话在等用户批准; 图标同步换成计划徽标（sessionStateIcon）
  const awaitingPlan = !!(run && isAwaitingPlan(run));
  if (awaitingPlan) item.classList.add("awaiting-plan");
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
  if (!await confirmDialog(`删除会话「${displayTitle(cur)}」？删除后不可恢复。`,
      { title: "删除会话", okText: "删除", danger: true })) return;
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
      const dir = await pickNativeFolder();
      if (dir) addProject(dir);
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
      + '<button class="p-del" data-tip="移除项目">' + TRASH_SMALL_SVG + '</button>'
      + chev;
    header.querySelector(".p-name").textContent = projectDisplayName(wd);
    header.querySelector(".p-count").textContent = items.length ? String(items.length) : "";
    header.querySelector(".p-add").onclick = ev => {
      ev.stopPropagation();   // 别触发折叠/展开
      startDraft(wd);
    };
    header.querySelector(".p-del").onclick = ev => {
      ev.stopPropagation();   // 别触发折叠/展开
      removeProject(wd, items.length);
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

/* 移除项目: 空项目仅从侧栏消失; 有会话的项目先解绑其下会话
 * （会话保留为独立"任务", 磁盘文件不动）, 再清手动添加记录。 */
async function removeProject(wd, count) {
  const name = projectDisplayName(wd);
  if (count > 0) {
    const ok = await confirmDialog(
      `移除项目「${name}」？其下 ${count} 个会话将保留为独立任务（不删除）, 磁盘文件不受影响。`,
      { title: "移除项目", okText: "移除", danger: true });
    if (!ok) return;
  } else {
    const ok = await confirmDialog(`移除项目「${name}」？仅从侧栏移除, 不影响磁盘文件。`,
      { title: "移除项目", okText: "移除", danger: true });
    if (!ok) return;
  }
  // 有会话的项目: 逐个解绑; 失败的（如恰在对话中）跳过并提示, 项目保留
  const sessions = state.sessions.filter(s => s.workdir === wd);
  let failed = 0;
  for (const s of sessions) {
    try {
      const r = await fetch(`/api/sessions/${s.id}/workdir`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workdir: null }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || r.status);
      }
      s.workdir = null;
      const run = state.runs[s.id];
      if (run) run.currentWorkdir = null;      // 顶栏标签/恢复对话不再绑回旧目录
      if (s.id === state.sessionId) refreshWorkdirTag();
    } catch (e) {
      failed++;
      console.error("[xcode] 解绑会话失败:", s.id, e);
    }
  }
  if (failed) {
    toast(`${failed} 个会话解绑失败（可能对话进行中）, 项目保留`);
    return;
  }
  state.customProjects = state.customProjects.filter(w => w !== wd);
  localStorage.setItem("xc-projects", JSON.stringify(state.customProjects));
  state.collapsedProjects.delete(wd);
  localStorage.setItem("xc-collapsed", JSON.stringify([...state.collapsedProjects]));
  renderSessionList();
  toast("已移除项目：" + name);
}

/* 桌面端选文件夹统一入口（侧栏「添加项目」与工作区「打开文件夹…」共用）。
 * 失败不再静默: reject（ACL/IPC/桥缺失/对话框崩溃）→ console.error + toast 上屏;
 * 用户取消（桥返回 null）静默返回 null——只有真实失败才打扰用户。 */
async function pickNativeFolder() {
  try {
    return await window.xcodePickFolder();
  } catch (e) {
    console.error("[xcode] 打开文件夹失败:", e);
    toast("打开文件夹失败：" + (e && e.message ? e.message : e), 4000);
    return null;
  }
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
    collapseFinishedToolGroups(col);   // 历史回放: 已结束的大分组默认收起
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
  state.draftMode = null;       // 草稿态预选的模式作废: 不带到别的会话
  state.sessionId = id;
  localStorage.setItem("xc-cur-session", id);   // 桌宠悬浮窗直连 WS 回退时用
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
  renderQueueCards();   // 待发送卡片跟随会话: 重绘为本会话的排队（无则清掉上个会话的残留）
  syncPlanPanelForActiveSession();   // 切回的会话若有未决计划: 恢复面板弹窗
  // 三设置回显: 会话运行值 → 会话列表缓存（/api/sessions 每项都带持久值/
  // 全局默认）→ 全局默认值。必须无条件 setValue: 否则下拉框残留上一个
  // 会话的显示值, 与本会话实际用的值不一致（"跟随全局"的会话尤其如此）
  {
    const s = state.sessions.find(x => x.id === id);
    const gd = state.globalDefaults;
    modeDd.setValue(run.permissionMode || (s && s.permission_mode) || gd.permissionMode);
    thinkDd.setValue(run.thinkingLevel || (s && s.thinking_level) || gd.thinkingLevel || "medium");
    const mKey = run.modelKey
      || (s && s.model_provider && s.model_id ? s.model_provider + "|" + s.model_id : null)
      || gd.modelKey;
    if (mKey) modelDd.setValue(mKey);
  }
  syncThinkingIndicator();   // 切会话必须重算: 转圈只属于"正在等待输出的那个会话"
  setConn(run.ws && run.ws.readyState === 1 ? "on" : "", run.ws ? (run.ws.readyState === 1 ? "已连接" : "连接中…") : "未连接");
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
      const dir = await pickNativeFolder();
      if (dir) { addProject(dir); state.draftDir = dir; renderWsChip(); }
      return;
    }
    openDirPop(anchor);             // 浏览器/预览: 页面内目录浏览兜底
  };
  pop.querySelector("[data-act='none']").onclick = () => {
    state.draftDir = null;
    closeWsPop();
    renderWsChip();
  };
  // 定位: chip 下方优先, 放不下翻到上方; 两向都放不下时限高让列表内滚
  const r = anchor.getBoundingClientRect();
  pop.style.visibility = "hidden";
  requestAnimationFrame(() => {
    const W = pop.offsetWidth, H = pop.offsetHeight;
    const left = Math.max(10, Math.min(r.left, window.innerWidth - W - 10));
    const M = 10;                                   // 视口安全边距
    const below = r.bottom + 6;
    const fitsBelow = below + H <= window.innerHeight - M;
    const fitsAbove = r.top - 6 - H >= M;
    let top = below;
    if (!fitsBelow && fitsAbove) {
      top = r.top - 6 - H;                          // 上方空间更充裕: 翻转
    } else if (below + H > window.innerHeight - M) {
      // 两向都放不下: 就地限高, 搜索框和操作项固定, 列表内部滚动
      const avail = Math.max(160, window.innerHeight - below - M);
      pop.style.maxHeight = avail + "px";
      pop.style.display = "flex";
      pop.style.flexDirection = "column";
      const lst = pop.querySelector(".ws-list");
      lst.style.flex = "1";
      lst.style.minHeight = "0";
      lst.style.maxHeight = "none";
    }
    pop.style.left = left + "px";
    pop.style.top = Math.max(M, top) + "px";
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
  renderQueueCards();   // 草稿态没有会话队列: 清掉上个会话残留的待发送卡片
  syncPlanPanelForActiveSession();   // 草稿态没有未决计划: 收起面板
  syncThinkingIndicator();     // 草稿态没有 run: 收掉从原会话带来的"思考中"转圈
                                 // （切走瞬间原会话正在 prefill 空窗, 否则没人再碰这个 DOM,
                                 //   后台轮次的空窗事件都带 sid 守卫, 不会点亮这里）
  setConn("", "未连接");
  // 草稿态下拉 = 新会话将用的全局默认值。必须显式重置: 否则残留上一个
  // 会话的显示值, 而首条消息实际按全局默认起跑 → 显示与实际不一致
  {
    const gd = state.globalDefaults;
    modeDd.setValue(state.draftMode || gd.permissionMode);
    thinkDd.setValue(gd.thinkingLevel || "medium");
    if (gd.modelKey) modelDd.setValue(gd.modelKey);
  }
  restoreCurrentInput();       // 恢复草稿态自己的输入
  $("input").focus();
}

/* 历史消息回放: role + blocks（text / tool_use / tool_result） */
function renderHistoryMessage(m) {
  if (m.role === "user") {
    const text = m.blocks.filter(b => b.type === "text").map(b => b.text).join("\n");
    // 旧版压缩持久化的续接指令: 模型专用文本, 渲染为一行提示卡而非气泡
    if (text && isCompactNotice(text)) { addCompactNotice(); return; }
    // 附件块转成与 WS 同形状: image 拼缩略图网格, file 渲染文件 chip
    const atts = m.blocks
      .filter(b => b.type === "image" || b.type === "file")
      .map(b => b.type === "image"
        ? { kind: "image", media_type: b.media_type, data: b.data }
        : { kind: "file", name: b.name, text: b.text });
    if (text || atts.length) addUserBubble(text, atts, null, m.ts);
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
        completeToolCard(card, { output: b.output, is_error: b.is_error,
                                 denied: false, result_meta: b.result_meta });
      } else {
        // 配不上对（旧数据/截断）: 独立卡片兜底
        addToolCard({ id: b.id, name: b.name, input: "(—)",
                      result: { output: b.output, is_error: b.is_error,
                                denied: false, result_meta: b.result_meta } });
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
    // 首连/重连标记: busy_sync 校正只信重连——首连的快照早于在途的乐观
    // 发送（草稿首发先置忙碌再建连）, 快照 false 不能否掉本地忙碌
    const reconnected = run.everConnected;
    run.reconnected = reconnected;
    run.everConnected = true;
    // 首条消息在 WS 建立期间入队，连接好了统一发出
    const pending = run.pendingSends;
    run.pendingSends = [];
    for (const p of pending) ws.send(JSON.stringify(p));
    // 断线重连后的重同步: 断连窗口内的工具/正文事件已丢,
    // 重拉历史替换整列, 丢配的卡片不会以"运行中"僵住。
    // 仅在重连时做——草稿首发是"先画乐观气泡再 connectWs",
    // 而 turn 落盘只在结束时, 首连就重拉会拿空历史把用户消息抹掉
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
/* 桌宠悬浮窗标注权限气泡的会话名: 只读查询, 不暴露 state 本体 */
window.xcodeSessionTitle = (sid) => {
  const s = state.sessions.find(x => x.id === sid);
  return s ? (s.title || s.name || "") : "";
};

function handleServerMessage(msg, sid) {
  const run = runOf(sid);
  // 桌宠悬浮窗(pet.js 转发): 前后台会话的运行事件都镜像一份给它做状态机。
  // 装饰性钩子必须隔离——它内部抛错不能拖垮消息主处理链(曾因 pet.js
  // 引用未定义变量, turn_done 全部炸在中断, 界面永远转圈且无法中断)。
  try { window.xcodePet?.onEvent?.(msg, sid); } catch (e) { console.warn("[pet]", e); }
  // 完成通知（提示音 + 桌面弹窗）: turn_done/error 是轮次终点, 前台/后台
  // 两条路径都从这里过, 单点挂钩全覆盖。内部自己判断"该不该响/该不该弹"。
  if (msg.type === "turn_done" || msg.type === "error") {
    try { notifyTurnEnd(msg, sid); } catch (e) { console.warn("[notify]", e); }
  }
  // 继续聊天 = 隐性否决未决计划: 服务端此时会把旧计划自动拒绝并叫停当前轮
  // （见 server 的 user 分支）, turn_interrupting/turn_started 到达即收口 UI——
  // 计划卡与右侧面板按钮定格"已过期", 正文淡化。前后台会话都要收口。
  if (run && (msg.type === "turn_interrupting" || msg.type === "turn_started")) {
    expirePlanCard(run, sid, "已过期 · 继续对话后重新规划");
  }
  // WS 建连时的服务端 busy 快照校正（前台/后台都要）: 本地 busy 的唯一清除
  // 途径是 turn_done/error, 服务进程死亡丢掉收尾事件后本地永远忙碌——
  // 死轮次的悬空工具卡在历史回放里被 !busy 跳过收口, 永远停在"运行中"。
  // busy=false 的校正只在重连时生效: 首连的快照不携带在途乐观发送的信息
  // （草稿首发: 本地先置忙碌再建连, 服务端此刻还空闲, 且首轮没有
  // turn_started 来恢复）, 误信会把整轮的忙碌 UI 杀掉。
  // 快照 true 而本地空闲 → 跟上忙碌（别的窗口的轮次活着, 保留"运行中"卡）。
  if (msg.type === "busy_sync") {
    if (!msg.busy && run && run.busy && run.reconnected) {
      if (sid === state.sessionId) endTurnUiReset();
      else settleBackgroundTurnEnd(run, sid);
    } else if (msg.busy && run && !run.busy) {
      run.busy = true;
      renderSessionList();
    }
    return;
  }
  // 后台会话: 流式内容照常写进它自己的常驻列（隐藏）, 切回时完整可见;
  // 只对 权限/结果/结束/错误 累计未读。结束/接力时同步运行态。
  if (sid !== state.sessionId) {
    if (msg.type === "text_delta") onTextDelta(msg, sid);
    else if (msg.type === "thinking_start") onThinkingStart(msg, sid);
    else if (msg.type === "thinking_end") onThinkingEnd(msg, sid);
    else if (msg.type === "tool_use_started") onToolUseStarted(msg, sid);
    else if (msg.type === "tool_use") onToolUse(msg, sid);
    // 后台会话的计划/权限请求也要走统一入口: 登记 pendingPerms + 在它的
    // 隐藏列里建审批卡。此前只刷会话列表, 切回后既无卡也无可点按钮,
    // 两个会话都在等计划审批时, 后弹的那个会让先弹的"卡住"。
    else if (msg.type === "permission_request") onPermissionRequest(msg, sid);
    else if (msg.type === "tool_result") onToolResult(msg, sid);
    else if (msg.type === "await_output") {
      run.awaiting = run.busy;
      if (run.awaiting) run.awaitT0 = Date.now();
    }
    else if (msg.type === "context_compacted") addCompactNotice(colOf(sid), true);
    else if (msg.type === "mode_changed") onModeChanged(msg, sid);
    else if (msg.type === "thinking_changed") onThinkingChanged(msg, sid);
    else if (msg.type === "model_changed") onModelChanged(msg, sid);

    else if (msg.type === "tool_result") bumpUnread(sid);
    else if (msg.type === "permission_resolved") onPermissionResolved(msg, sid);
    else if (msg.type === "turn_done" || msg.type === "error") {
      bumpUnread(sid);
      settleBackgroundTurnEnd(run, sid);
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
      settleRelayedMessages(msg, sid);
      beginOptimisticThinking(run, sid, colOf(sid));
    }
    return;
  }
  switch (msg.type) {
    case "text_delta":         onTextDelta(msg, sid); break;
    case "tool_use_started":   onToolUseStarted(msg, sid); break;
    case "tool_use":           onToolUse(msg, sid); break;
    case "tool_result":        onToolResult(msg, sid); break;
    case "thinking_start":     onThinkingStart(msg, sid); break;
    case "thinking_end":       onThinkingEnd(msg, sid); break;
    case "turn_queued":        onTurnQueued(msg); break;
    case "turn_started":       onTurnStarted(msg, sid); break;
    case "turn_queued_user":   onTurnQueuedUser(msg, sid); break;
    case "turn_queue_cleared": onQueueCleared(sid); break;
    case "await_output":       onAwaitOutput(msg, state.sessionId); break;
    case "context_compacted":  addCompactNotice(msgCol(), true); break;
    case "rate_limited_retry": onRateLimitedRetry(msg, sid); break;
    case "turn_interrupting":  onTurnInterrupting(msg, sid); break;
    case "permission_request": onPermissionRequest(msg, sid); break;
    case "permission_resolved": onPermissionResolved(msg, sid); break;
    case "mode_changed":       onModeChanged(msg, sid); break;
    case "thinking_changed":   onThinkingChanged(msg, sid); break;
    case "model_changed":      onModelChanged(msg, sid); break;
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
  clearRateLimitNote(run);   // 正文已到: 限流重试成功, 撤提示行
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
// 计划模式图标: TOOL_META 在模块加载即求值, 声明必须位于其前（否则 TDZ 炸掉整个引导）
const ICON_MODE_PLAN = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6l1 3h3v15H5V6h3l1-3z"/><path d="M9 12h6M9 16h4"/></svg>';
const ICON_SEARCH = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21"/><path d="M8 11h6M11 8v6"/></svg>';
const ICON_GLOB = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h5M4 11h7M4 16h5"/><path d="M14 5l6 7-6 7"/><path d="M20 12H10"/></svg>';
const ICON_TODO = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6.5l1.5 1.5L8 5.5"/><path d="M4 13.5l1.5 1.5L8 12.5"/><path d="M11 7h9M11 14h9M11 20h6"/></svg>';
const TOOL_META = {
  bash:       { label: "终端",     icon: ICON_TERM },
  powershell: { label: "终端",     icon: ICON_TERM },
  read_file:  { label: "读取文件", icon: ICON_FILE },
  write_file: { label: "写入文件", icon: ICON_EDIT },
  present_plan: { label: "实施计划", icon: ICON_MODE_PLAN },
  todo: { label: "任务清单", icon: ICON_TODO },
  grep: { label: "搜索内容", icon: ICON_SEARCH },
  glob: { label: "查找文件", icon: ICON_GLOB },
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
  clearRateLimitNote(run);   // 思考已开始: 限流重试成功, 撤提示行
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
  const rlMs = run.rlDelayMs || 0;
  run.rlDelayMs = 0;
  const suffix = rlMs > 0 ? '（含限流重试 ' + fmtDuration(rlMs) + '）' : '';
  cur.el.classList.remove("thinking");
  cur.el.innerHTML = '<span class="t-ico">' + ICON_MIND + '</span>' +
    '<span>思考 · 持续了 ' + fmtDuration(ms) + suffix + '</span>';
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
/* 工具分组: 连续的工具调用收进同一个可折叠容器（头部显示次数/状态摘要），
 * 避免长任务十几行工具记录把正文顶出屏幕。轮次结束自动收起，运行中保持展开。
 * 纯视觉层: 不参与卡片配对（liveToolCards/toolResultIndex 仍指向行元素本身）。 */
const TOOL_GROUP_AUTO_COLLAPSE_AT = 5;   // 分组内行数达到该值, 结束后自动收起

function fmtToolDur(ms) {
  if (ms < 1000) return (ms / 1000).toFixed(1) + "s";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
}

function updateToolGroupHeader(group) {
  if (!group) return;
  const rows = group.querySelectorAll(".tool-row");
  const sum = group.querySelector(".tg-sum");
  if (!rows.length) { group.remove(); return; }   // 最后一行被撤（plan_rejected）: 分组一并撤
  if (!sum) return;
  let run = 0, ok = 0, bad = 0;
  rows.forEach(r => {
    const st = r.dataset.state;
    if (st === "run") run++;
    else if (st === "ok") ok++;
    else bad++;   // err / denied / stopped 归为需注意
  });
  group.classList.toggle("running", run > 0);
  if (run > 0) {
    const done = rows.length - run;
    sum.textContent = rows.length === 1 ? "运行中…" : "运行中 " + done + "/" + rows.length + "…";
    sum.className = "tg-sum run";
    // 运行中自动收起: 行数达标后只留头部摘要 + 当前运行行, 历史行点头部回看
    if (rows.length >= TOOL_GROUP_AUTO_COLLAPSE_AT && !group._userOpen) {
      group.classList.add("collapsed");
    }
  } else {
    let txt = "✓" + ok;
    if (bad) txt += " · !" + bad;
    sum.textContent = txt;
    sum.className = "tg-sum" + (bad ? " bad" : "");
  }
}

/* 新工具行的落点: 上一个元素是分组就续用, 否则开新分组。
 * 顺手收起同列里上一个"已结束且行数达标"的旧分组（新活动开始了, 旧的让位）。 */
function groupForNewToolRow(col) {
  let g = col.lastElementChild;
  if (!g || !g.classList.contains("tool-group")) {
    g = document.createElement("div");
    g.className = "tool-group";
    const head = document.createElement("button");
    head.type = "button";
    head.className = "tg-head";
    head.innerHTML = '<span class="tg-caret">▾</span><span class="tg-title">工具调用</span>' +
                     '<span class="tg-sum"></span>';
    head.onclick = () => {
      g.classList.toggle("collapsed");
      if (!g.classList.contains("collapsed")) g._userOpen = true;   // 手动展开: 运行中不自动收回
    };
    g.appendChild(head);
    const body = document.createElement("div");
    body.className = "tg-body";
    g.appendChild(body);
    col.appendChild(g);
  }
  const prior = col.querySelectorAll(".tool-group");
  if (prior.length > 1) {
    const prev = prior[prior.length - 2];
    const running = prev.querySelector('.tool-row[data-state="run"]');
    if (!running && prev.querySelectorAll(".tool-row").length >= TOOL_GROUP_AUTO_COLLAPSE_AT) {
      prev.classList.add("collapsed");
    }
  }
  return g;
}

/* 轮次收口/历史回放后调用: 结束且行数达标的分组收起（运行中的分组不动） */
function collapseFinishedToolGroups(col) {
  if (!col) return;
  col.querySelectorAll(".tool-group").forEach(g => {
    g._userOpen = false;   // 轮次收口: 重置手动展开标记, 新一轮恢复自动收起
    if (g.querySelector('.tool-row[data-state="run"]')) return;
    if (g.querySelectorAll(".tool-row").length >= TOOL_GROUP_AUTO_COLLAPSE_AT) {
      g.classList.add("collapsed");
    }
  });
}

function describeInput(raw, name) {  // 卡片标题一行摘要: JSON 先取 command/path 等关键字段，失败展示原文
  try {
    const data = JSON.parse(raw);
    if (data && typeof data === "object") {
      if (data.action === "write" && Array.isArray(data.items)) {
        const done = data.items.filter(it => it.status === "completed").length;
        return data.items.length + " 项 (已完成 " + done + ")";
      }
      if (data.action === "read") return "读取当前清单";
      if (name === "grep") {
        let s = "/" + String(data.pattern || "") + "/";
        if (data.glob) s += "  ·  " + data.glob;
        if (data.output_mode && data.output_mode !== "files_with_matches") s += "  ·  " + data.output_mode;
        return s;
      }
      if (name === "glob") return String(data.pattern || "");
      for (const k of ["command", "path", "file_path", "url", "content", "plan"]) {
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
    '<span class="tname2"></span><span class="tdesc"></span><span class="t-dur"></span><span class="tstate"></span>';
  row.querySelector(".tname2").textContent = meta.label;
  row.querySelector(".tdesc").textContent = describeInput(input, name);
  row.dataset.tool = name;
  row.dataset.input = input || "";
  row._t0 = Date.now();   // 结果到达时在 setToolState 里折算耗时小字
  // 不设 title: 悬停不再弹入参 JSON, 行内摘要足够
  // 落进工具分组（连续调用折叠为一组）, 不再逐条平铺在消息列
  groupForNewToolRow(col || msgCol()).querySelector(".tg-body").appendChild(row);
  if (result) {
    completeToolCard(row, result);
  } else {
    setToolState(row, "run");
  }
  scrollToBottom();
  return row;
}

/* ---------- diff 展示: write_file 结果卡的统一 diff 面板 ----------
 * 服务端在 tool_result 事件/历史回放里带 result_meta.diff（unified diff）。
 * 面板插在工具行下方, 默认折叠, 点头部展开/收起; ± 行绿/红着色。 */
function renderDiffPanel(row, diffText, meta) {
  if (!diffText || row.querySelector(".td-wrap")) return;
  const lines = diffText.split(String.fromCharCode(10));
  // 统计增删行数（跳过 ---/+++/@@ 头）
  let add = 0, del = 0;
  for (const l of lines) {
    if (l.startsWith("+") && !l.startsWith("+++")) add++;
    else if (l.startsWith("-") && !l.startsWith("---")) del++;
  }
  const wrap = document.createElement("div");
  wrap.className = "td-wrap";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "td-head";
  head.innerHTML =
    '<span class="td-caret">▸</span>' +
    '<span class="td-sum">' + (meta && meta.created ? "新建文件" : "变更") + '</span>' +
    '<span class="td-add">+' + add + '</span><span class="td-del">-' + del + '</span>';
  const body = document.createElement("div");
  body.className = "td-body";
  body.hidden = true;
  const table = document.createElement("div");
  table.className = "td-table";
  for (const l of lines) {
    const ln = document.createElement("div");
    let cls = "ctx";
    if (l.startsWith("+") && !l.startsWith("+++")) cls = "add";
    else if (l.startsWith("-") && !l.startsWith("---")) cls = "del";
    else if (l.startsWith("@@")) cls = "hunk";
    ln.className = "td-line " + cls;
    ln.textContent = l.length ? l : " ";
    table.appendChild(ln);
  }
  body.appendChild(table);
  head.onclick = () => {
    body.hidden = !body.hidden;
    head.querySelector(".td-caret").textContent = body.hidden ? "▸" : "▾";
  };
  wrap.appendChild(head);
  wrap.appendChild(body);
  row.insertAdjacentElement("afterend", wrap);   // 面板独立于工具行, 不挤一行式布局
}

function setToolState(row, kind) {
  row.dataset.state = kind;
  const st = row.querySelector(".tstate");
  st.className = "tstate " + kind;
  st.textContent = { run: "运行中", ok: "已完成", err: "出错",
                     denied: "已拒绝", stopped: "已中断" }[kind] || kind;
  // 耗时小字: 结果闭合那一刻起算。过短（<100ms，历史回放/同 tick 闭合）不显示,
  // 避免整列 "0.0s" 噪音; 悬空收口的行没有 _t0 也不显示
  const dur = row.querySelector(".t-dur");
  if (dur && row._t0 && kind !== "run") {
    const ms = Date.now() - row._t0;
    if (ms >= 100) dur.textContent = fmtToolDur(ms);
  }
  updateToolGroupHeader(row.closest(".tool-group"));
}

function completeToolCard(row, { is_error, denied, result_meta, output }) {
  if (denied) {
    setToolState(row, "denied");
  } else if (is_error) {
    setToolState(row, "err");
  } else {
    setToolState(row, "ok");
  }
  if (result_meta && result_meta.diff) {
    renderDiffPanel(row, result_meta.diff, result_meta);
  }
  if (row.dataset.tool === "todo") {
    renderTodoCard(row);
  } else {
    // 命令/工具输出面板: 输出非空即渲染（此前 output 只进历史, 界面上
    // 无处可看——点卡片没有任何反应）。出错自动展开, 成功默认收起。
    renderOutputPanel(row, output, !!is_error && !denied);
  }
}

/* ---------- 输出展示: 工具卡的统一输出面板 ----------
 * tool_result 的 output（命令 stdout/stderr、工具摘要）此前只进模型历史,
 * 界面无处可看。面板插在工具行下方（与 diff 面板同构）, 默认折叠;
 * is_error 自动展开——出错时用户最关心的就是输出。纯 textContent,
 * 不走 markdown 渲染, 超长截断（完整输出在会话历史里）。 */
const OUTPUT_PANEL_MAX = 20000;
function renderOutputPanel(row, outputText, autoOpen) {
  if (!outputText || !String(outputText).trim()) return;
  if (row.querySelector(".to-wrap")) return;
  let text = String(outputText);
  if (text.length > OUTPUT_PANEL_MAX) {
    text = text.slice(0, OUTPUT_PANEL_MAX) +
      "\n\n[... 已截断, 共 " + text.length + " 字符; 完整输出见上下文]";
  }
  const wrap = document.createElement("div");
  wrap.className = "to-wrap" + (autoOpen ? " has-err" : "");
  const head = document.createElement("button");
  head.type = "button";
  head.className = "to-head";
  head.innerHTML = '<span class="to-caret">▸</span><span class="to-sum">输出</span>';
  const body = document.createElement("div");
  body.className = "to-body";
  body.hidden = !autoOpen;
  head.querySelector(".to-caret").textContent = body.hidden ? "▸" : "▾";
  const pre = document.createElement("div");
  pre.className = "to-pre";
  pre.textContent = text;
  body.appendChild(pre);
  head.onclick = () => {
    body.hidden = !body.hidden;
    head.querySelector(".to-caret").textContent = body.hidden ? "▸" : "▾";
  };
  wrap.appendChild(head);
  wrap.appendChild(body);
  row.insertAdjacentElement("afterend", wrap);
}

/* todo 卡: 工具行下方渲染任务清单本体（取代裸文本结果）。数据取自
 * 工具入参（write 时最新鲜、且历史回放可用——tool_use 块的 input 就有） */
function renderTodoCard(row) {
  if (row.querySelector(".todo-list")) return;
  let items = null;
  try {
    const data = JSON.parse(row.dataset.input || "{}");
    if (data.action === "write" && Array.isArray(data.items)) items = data.items;
  } catch (e) { /* 历史数据无 input */ }
  if (!items || !items.length) return;
  const wrap = document.createElement("div");
  wrap.className = "todo-list";
  const done = items.filter(it => it.status === "completed").length;
  const prog = document.createElement("div");
  prog.className = "todo-prog";
  const track = document.createElement("div");
  track.className = "todo-track";
  const bar = document.createElement("div");
  bar.className = "todo-bar";
  bar.style.width = Math.round(done / items.length * 100) + "%";
  track.appendChild(bar);
  prog.appendChild(track);
  const label = document.createElement("span");
  label.textContent = done + "/" + items.length;
  prog.appendChild(label);
  wrap.appendChild(prog);
  for (const it of items) {
    const line = document.createElement("div");
    line.className = "todo-item " + (it.status || "pending");
    line.textContent = it.content || "";
    wrap.appendChild(line);
  }
  row.insertAdjacentElement("afterend", wrap);
}

function onToolUseStarted(msg, sid) {
  // content_block_start(tool_use) 即建卡: 大参数（write_file 整文件等）的
  // JSON 流式期可达几十秒, 此前这段时间画面全静（转圈已收、卡片未建）,
  // 像卡死。参数传完后 onToolUse 按 id 复用这张卡补全描述。
  const run = runOf(sid);
  if (msg.id && run.liveToolCards[msg.id]) return;   // 幂等: 卡已在
  const active = sid === state.sessionId;
  run.awaiting = false;                    // 工具卡已是可见反馈: 空窗结束
  clearRateLimitNote(run);
  if (active) syncThinkingIndicator();
  flushAssistantBubble(run);
  dropOptimisticThinking(run);
  const card = addToolCard({ id: msg.id, name: msg.name, input: "" }, colOf(sid));
  card.querySelector(".tdesc").textContent = "接收参数中…";
  if (msg.id) run.liveToolCards[msg.id] = card;
  run.activeToolCard = card;
}

function onToolUse(msg, sid) {
  const run = runOf(sid);
  const active = sid === state.sessionId;
  run.awaiting = false;                    // 工具卡已是可见反馈: 空窗结束
  clearRateLimitNote(run);   // 工具调用已到: 限流重试成功, 撤提示行
  if (active) syncThinkingIndicator();
  flushAssistantBubble(run);   // 工具前先收掉流式中的正文气泡
  dropOptimisticThinking(run);   // 工具先于思考到达: 撤掉乐观胶囊（工具卡已是反馈）
  // tool_use_started 已提前建卡: 就地补全真实参数, 不重复建卡
  const existing = msg.id ? run.liveToolCards[msg.id] : null;
  if (existing) {
    existing.querySelector(".tdesc").textContent = describeInput(msg.input, msg.name || existing.dataset.tool);
    existing.dataset.input = msg.input || "";          // todo 卡等从入参渲染的地方依赖它
    existing._t0 = Date.now();   // 占位卡早于参数到达: 耗时从参数齐全起算
    run.activeToolCard = existing;
    return;
  }
  const card = addToolCard({ id: msg.id, name: msg.name, input: msg.input }, colOf(sid));
  if (msg.id) run.liveToolCards[msg.id] = card;   // 按 id 登记, 结果精确配对
  run.activeToolCard = card;
}

function onToolResult(msg, sid) {
  const run = runOf(sid);
  // 聊天点播: music_play 的结果镜像带 result_meta.music, 转交电台开播。
  // 只在实时事件里播——历史回放（completeToolCard 的 result_meta 只渲染 diff）
  // 不重播旧歌, 刷新页面不会凭空响起来。
  if (msg.result_meta && msg.result_meta.music && !msg.is_error) {
    const ok = window.xcodeMusicPlay && window.xcodeMusicPlay(msg.result_meta.music);
    if (!ok) toast("电台没接住点播指令, 点侧栏 ♫ 手动播吧");
  }
  if (msg.plan_rejected) {
    // 计划被拒: 计划卡已渲染拒绝态, 不补失败工具卡。但 tool_use_started
    // 可能已提前建了占位卡（present_plan 也会先镜像）, 就地移除, 不留悬卡
    if (msg.id && run.liveToolCards[msg.id]) {
      const card = run.liveToolCards[msg.id];
      delete run.liveToolCards[msg.id];
      const group = card.closest(".tool-group");   // 先取: remove 后节点脱离 DOM, closest 拿不到分组
      card.remove();
      if (run.activeToolCard === card) run.activeToolCard = null;
      updateToolGroupHeader(group);   // 空了会整个撤掉分组
    }
    return;
  }
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
  // （msg.result_meta 由 completeToolCard 消费: write_file 的 diff 面板）
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

/* 后台会话的轮次终点收口: busy 复位 + 悬空工具卡 sweep + 各类滞留 UI 清理。
 * turn_done/error 分支与 busy_sync 校正共用（前台会话走 endTurnUiReset）。 */
function settleBackgroundTurnEnd(run, sid) {
  run.busy = false;
  run.awaiting = false;
  run.queued = false;
  settlePendingPermsOnTurnEnd(run, sid);
  // 轮次收口: 悬空工具行标"已中断", 清掉流式指针
  sweepPendingToolCards(run);
  collapseFinishedToolGroups(colOf(sid));   // 后台会话同样收起已结束的大分组
  clearRateLimitNote(run);   // 限流退避提示一并撤下
  run.curBubble = null;
  // 思考行/乐观胶囊兜底收口（客户端计时）, 与前台 endTurnUiReset 一致;
  // 只清指针的话, 行会永远卡在"思考中…"动画态
  if (run.curThinking) onThinkingEnd({}, sid);
}

/* ---------- 权限审批: 聊天流内联卡片（替代旧模态弹窗） ---------- */
const ICON_PRM_OK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 12.5l5 5 10-11"/></svg>';
const ICON_PRM_NO = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';

/* 允许/拒绝按钮组: 工具审批卡与计划卡共用, 点击即决定 */
function buildPermChoices(requestId, sid, allowLabel, denyLabel) {
  const choices = document.createElement("div");
  choices.className = "pr-choices";
  for (const trip of [[true, "allow", allowLabel, ICON_PRM_OK],
                      [false, "deny", denyLabel, ICON_PRM_NO]]) {
    const val = trip[0], cls = trip[1], label = trip[2], icon = trip[3];
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pr-btn " + cls;
    btn.innerHTML = icon;
    const txt = document.createElement("span");
    txt.textContent = label;
    btn.appendChild(txt);
    btn.onclick = function () { respondPermission(requestId, val, sid); };
    choices.appendChild(btn);
  }
  return choices;
}

/* 决定后的结果标记: 勾/叉图标 + 文案 */
function makePrMark(text, approved) {
  const mark = document.createElement("span");
  mark.className = "pr-mark";
  mark.innerHTML = approved ? ICON_PRM_OK : ICON_PRM_NO;
  const txt = document.createElement("span");
  txt.textContent = text;
  mark.appendChild(txt);
  return mark;
}

/* 命令前缀规则提取: 从入参 command 里取前 ≤2 个词（剥 VAR=val 前缀）,
 * 作为"以 xx 开头"的白名单规则。包管理器 run/exec/test 类取 3 词——
 * "uv run pytest -q" 提 "uv run pytest" 而不是 "uv run"(那会连带放行
 * "uv run python 任意脚本")。UI 层示意即可, 服务端保存时会再清洗、
 * 授权层按 shlex 词对齐匹配（比这里更严格）。取不出词返回 null。 */
const RULE_RUNNER_FIRST = new Set(["uv", "npm", "pnpm", "yarn", "bun", "deno"]);
const RULE_RUNNER_SECOND = new Set(["run", "exec", "test", "x"]);
function allowlistRuleOf(input) {
  let cmd = "";
  try {
    const data = JSON.parse(input || "{}");
    if (typeof data.command === "string") cmd = data.command;
  } catch (e) { /* not json */ }
  let words = cmd.trim().split(/\s+/).filter(w => w && !/^[A-Za-z_][A-Za-z0-9_]*=$/.test(w));
  const n = (words.length >= 3 && RULE_RUNNER_FIRST.has((words[0] || "").toLowerCase())
    && RULE_RUNNER_SECOND.has((words[1] || "").toLowerCase())) ? 3 : 2;
  words = words.slice(0, n).map(w => w.replace(/["']/g, ""));
  const rule = words.join(" ").trim().slice(0, 80);
  return rule || null;
}

/* 请求是否仍挂起: 记忆类按钮的副作用(写白名单/附加目录)只在请求真正
 * 待批时才允许发生。批复可能在按钮 POST 的竞态窗口里经别的路径
 * (主按钮/桌宠/REST)完成——已处理就不再落任何副作用。 */
function permRequestPending(requestId, sid) {
  const run = runOf(sid);
  return !!(run && run.pendingPerms[requestId]);
}

/* "总是允许"按钮: 把该命令的前缀规则加入白名单并批准本次请求。
 * 只给 bash/powershell 审批卡渲染——白名单是 shell 语义。 */
function buildAlwaysAllowBtn(requestId, sid, input) {
  const rule = allowlistRuleOf(input);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pr-btn always";
  btn.innerHTML = ICON_PRM_OK;
  const txt = document.createElement("span");
  txt.textContent = rule ? `总是允许"${rule} …"` : "总是允许";
  btn.appendChild(txt);
  btn.onclick = async function () {
    if (!permRequestPending(requestId, sid)) return;
    btn.disabled = true;
    try {
      const r = await fetch("/api/settings/allowlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule: rule || (allowlistRuleOf(input) || "bash") }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || r.status);
      }
      const data = await r.json();
      respondPermission(requestId, true, sid);
      toast(`已加白名单并允许: ${(data.rules || []).slice(-1)[0] || rule}`);
      if (typeof renderAllowlistSettings === "function") renderAllowlistSettings();
    } catch (e) {
      btn.disabled = false;
      toast("加白名单失败: " + e.message);
    }
  };
  return btn;
}

/* "本会话允许"按钮: 前缀规则只写入该会话 runtime 的会话级白名单（不落盘）,
 * 会话内同类命令不再弹问, 会话结束即失效——比"总是允许"轻一档的记忆。 */
function buildSessionAllowBtn(requestId, sid, input) {
  const rule = allowlistRuleOf(input);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pr-btn session";
  btn.innerHTML = ICON_PRM_OK;
  const txt = document.createElement("span");
  txt.textContent = rule ? `本会话允许"${rule} …"` : "本会话允许";
  btn.appendChild(txt);
  btn.onclick = async function () {
    if (!permRequestPending(requestId, sid)) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/allow-rules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule: rule || "bash" }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || r.status);
      }
      respondPermission(requestId, true, sid);
      toast(`本会话内不再询问: ${rule || "该命令"}`);
    } catch (e) {
      btn.disabled = false;
      toast("会话规则写入失败: " + e.message);
    }
  };
  return btn;
}

/* 写工具目标路径所在目录（取绝对路径才有可记的目录; 相对路径交给
 * 服务端 resolve 会按服务进程 cwd 解析, 记错目录不如不记）。 */
function writeTargetDirOf(input) {
  let p = "";
  try {
    const data = JSON.parse(input || "{}");
    if (typeof data.path === "string") p = data.path;
  } catch (e) { return null; }
  if (!/^[a-zA-Z]:[\\/]/.test(p) && !p.startsWith("/")) return null;
  p = p.replace(/[\\/]+$/, "");
  const i = Math.max(p.lastIndexOf("/"), p.lastIndexOf("\\"));
  const dir = i > 0 ? p.slice(0, i) : null;
  return dir && dir.length > 2 ? dir : null;
}

/* "允许并记住该目录"按钮: 写出 workspace 根的审批卡专用——目标目录加入
 * 全局附加目录（additionalDirectories）并批准本次, 之后写该目录不再弹问。
 * 敏感路径（escalation=sensitive）不给记忆出路, 每次都问。 */
function buildRememberDirBtn(requestId, sid, input) {
  const dir = writeTargetDirOf(input);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pr-btn always";
  btn.innerHTML = ICON_PRM_OK;
  const txt = document.createElement("span");
  txt.textContent = dir ? `允许并记住 ${dir}` : "允许并记住该目录";
  btn.appendChild(txt);
  btn.onclick = async function () {
    if (!permRequestPending(requestId, sid)) return;
    if (!dir) { respondPermission(requestId, true, sid); return; }
    btn.disabled = true;
    try {
      const r = await fetch("/api/settings/additional-dirs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || r.status);
      }
      respondPermission(requestId, true, sid);
      toast(`已记住目录, 之后写该目录不再询问: ${dir}`);
    } catch (e) {
      btn.disabled = false;
      toast("记住目录失败: " + e.message);
    }
  };
  return btn;
}

function onPermissionRequest(msg, sid) {
  // sid 缺省 = 当前会话（旧事件流路径）: WS 分发处总是带 sid
  const sid2 = sid || state.sessionId;
  const run = runOf(sid2);
  if (run.pendingPerms[msg.request_id]) return;   // 重放/重连重复事件: 忽略
  run.pendingPerms[msg.request_id] = msg;
  bumpUnread(sid2);
  // 等待授权也是"等模型"的一种: 点亮空窗态, 否则画面全静止,
  // 用户会以为这轮已经跑完
  run.awaiting = run.busy;
  if (run.awaiting) run.awaitT0 = Date.now();
  if (sid2 === state.sessionId) syncThinkingIndicator();

  const meta = TOOL_META[msg.tool_name] || { label: msg.tool_name, icon: ICON_TOOL };
  if (msg.tool_name === "present_plan") {
    renderPlanCard(msg, sid2, run);
    return;
  }
  const row = document.createElement("div");
  row.className = "perm-row";
  row.dataset.reqId = msg.request_id;

  // 卡片式: 头行(图标+工具+等待提示) / 命令行 / 按钮行, 点按钮即决定
  const head = document.createElement("div");
  head.className = "pr-head";
  const ico = document.createElement("span");
  ico.className = "pr-ico";
  ico.innerHTML = meta.icon;
  const title = document.createElement("span");
  title.className = "pr-title";
  title.textContent = meta.label;
  const hint = document.createElement("span");
  hint.className = "pr-hint";
  hint.innerHTML = '<i class="pr-dot"></i>等待确认';
  head.appendChild(ico); head.appendChild(title); head.appendChild(hint);
  row.appendChild(head);

  const body = describeInput(msg.input, msg.name) || "(无参数)";
  const cmd = document.createElement("div");
  cmd.className = "pr-cmd";
  cmd.textContent = body;
  row.appendChild(cmd);

  // 弹问原因（敏感路径/写出 workspace 根）: 用户得知道这次为什么弹
  if (msg.detail) {
    const why = document.createElement("div");
    why.className = "pr-detail";
    why.textContent = "⚠ " + msg.detail;
    row.appendChild(why);
  }

  row.appendChild(buildPermChoices(msg.request_id, sid2, "允许", "拒绝"));
  // shell 命令审批卡追加记忆按钮: "总是允许"入全局白名单（落盘）,
  // "本会话允许"只写会话级规则（会话结束失效）——同类命令不再逐条问
  if (msg.tool_name === "bash" || msg.tool_name === "powershell") {
    row.appendChild(buildAlwaysAllowBtn(msg.request_id, sid2, msg.input));
    row.appendChild(buildSessionAllowBtn(msg.request_id, sid2, msg.input));
  }
  // 写出 workspace 根的审批卡: 记住目标目录（加入附加目录）后免问;
  // 敏感路径不给记忆出路（escalation=sensitive 不渲染此按钮）
  if ((msg.tool_name === "write_file" || msg.tool_name === "edit_file")
      && msg.escalation === "outside-write") {
    row.appendChild(buildRememberDirBtn(msg.request_id, sid2, msg.input));
  }

  colOf(sid2).appendChild(row);
  if (sid2 === state.sessionId) scrollToBottom();
}

/* ---------- 计划预览卡: present_plan 专用 ----------
 * markdown 渲染计划全文, 单选批准/拒绝; 批准后端自动升级模式并继续,
 * 拒绝则收起选择区、标记"已拒绝"，模型会修订后再次提交。 */
/* present_plan 的 input → markdown 计划全文（解析失败回退原文） */
function planTextOf(input) {
  try {
    const data = JSON.parse(input || "{}");
    return typeof data.plan === "string" ? data.plan : String(input || "");
  } catch (e) { return String(input || ""); }
}

/* 右侧计划面板呈现一份计划: 全文渲染 + 审批按钮 + 归属会话标记。
 * 当前会话的新计划（renderPlanCard）与切回会话的恢复
 * （syncPlanPanelForActiveSession）共用。面板全局只有一份, 归属
 * 记在 #plan-actions 的 reqId/reqSession 上, 收口/批复据此认领。 */
function showPlanInPanel(reqId, sid, planText) {
  const pane = $("pane");
  const body = $("plan-body");
  _planSource = planText;   // 复制按钮用: 始终复制 markdown 源文
  body.innerHTML = renderMd(planText);
  decorateCode(body);
  body.classList.remove("stale");   // 新计划到达: 清掉上一份的过期淡化
  const actions = $("plan-actions");
  actions.innerHTML = "";
  actions.dataset.reqId = reqId;
  actions.dataset.reqSession = sid;   // 面板归属: 批复/收口只认这个会话
  actions.appendChild(buildPermChoices(reqId, sid, "批准并实施", "拒绝"));
  const s = state.sessions.find(x => x.id === sid);
  $("plan-title").textContent = "实施计划" + (s ? " · " + displayTitle(s) : "");
  pane.classList.add("plan-open");
}

/* 切会话/回草稿态时同步右侧面板: 当前会话有未决 present_plan 就恢复显示
 * （后台会话收到计划时不抢面板, 靠这里在切回时补弹——两个会话都在等
 * 计划审批时, 先弹的那个不再被后弹的顶掉）; 没有则只收起面板, 不清正文
 * （用户可能还在看上一份已定的计划全文）。 */
function syncPlanPanelForActiveSession() {
  const run = curRun();
  const pend = run ? Object.values(run.pendingPerms)
    .filter(p => p && p.tool_name === "present_plan") : [];
  if (pend.length) {
    const msg = pend[pend.length - 1];   // 最近提交的一份未决计划
    showPlanInPanel(msg.request_id, state.sessionId, planTextOf(msg.input));
  } else {
    $("pane").classList.remove("plan-open");
  }
}

function renderPlanCard(msg, sid2, run) {
  const active = sid2 === state.sessionId;
  // 右侧计划面板只属于当前会话: 新计划在此弹出; 后台会话不抢面板,
  // 只建聊天流卡 + 登记 pendingPerms, 切回时 syncPlanPanelForActiveSession 补弹
  if (active) showPlanInPanel(msg.request_id, sid2, planTextOf(msg.input));

  // 聊天流轻量卡: 面板被收起时也能就地批准/拒绝
  const card = document.createElement("div");
  card.className = "plan-card";
  card.dataset.reqId = msg.request_id;
  const head = document.createElement("div");
  head.className = "plan-head";
  head.innerHTML = '<span class="pr-ico">' + ICON_MODE_PLAN + '</span>' +
    '<span class="plan-title">实施计划</span>' +
    '<span class="pr-hint"><i class="pr-dot"></i>等待确认' +
    (active ? "（已在右侧面板打开）" : "") + '</span>';
  card.appendChild(head);
  card.appendChild(buildPermChoices(msg.request_id, sid2, "批准并实施", "拒绝"));
  colOf(sid2).appendChild(card);
  if (active) scrollToBottom();
}

$("plan-close").onclick = () => $("pane").classList.remove("plan-open");

/* 计划全文复制: 复制 markdown 源文（renderPlanCard 存于 _planSource）,
 * 粘贴到文档/issue 语法结构完好; 按钮短暂显示"已复制"后回弹 */
let _planSource = "";
$("plan-copy").onclick = async () => {
  if (!_planSource) return;
  await copyText(_planSource);
  const btn = $("plan-copy");
  btn.innerHTML = CHECK_SVG + "<span>已复制</span>";
  setTimeout(() => { btn.innerHTML = COPY_SVG + "<span>复制</span>"; }, 1400);
};

/* 计划过期收口: 用户不批计划而是继续发消息追加需求, 或轮次已收口时,
 * 未决的 present_plan 在协议层已是死请求（服务端 prompter 已换新）。
 * 前端就地定格: 聊天流计划卡与右侧面板按钮标"已过期/已中断", 正文淡化
 * 但保留可读——模型随后会重新规划, 新计划以新 reqId 覆盖面板。
 * 只清前端 pendingPerms, 不发 permission_response（旧 reqId 发了只会错配）。 */
function expirePlanCard(run, sid, label) {
  const rids = Object.keys(run.pendingPerms).filter(rid =>
    run.pendingPerms[rid] && run.pendingPerms[rid].tool_name === "present_plan");
  if (!rids.length) return;
  for (const rid of rids) delete run.pendingPerms[rid];
  for (const rid of rids) {
    colOf(sid).querySelectorAll(`.plan-card[data-req-id="${rid}"]`).forEach(card => {
      if (card.classList.contains("allowed") || card.classList.contains("denied")) return;
      card.classList.add("denied", "expired");
      const choices = card.querySelector(".pr-choices");
      if (choices) choices.remove();
      card.appendChild(makePrMark(label, false));
    });
  }
  // 右侧面板: 显示的正是被过期的这份计划（且属于本会话）→ 按钮区定格, 正文淡化
  const pa = $("plan-actions");
  if (rids.includes(pa.dataset.reqId) && pa.dataset.reqSession === sid) {
    pa.innerHTML = "";
    pa.appendChild(makePrMark(label, false));
    $("plan-body").classList.add("stale");
  }
}

/* 审批卡定格: 撤按钮, 标记结果（本地批复与 permission_resolved 广播共用,
 * "已定格"的卡跳过——两条路径可能先后到达同一 rid, 幂等） */
function markPermResolved(sid, requestId, approved) {
  const markCard = card => {
    if (!card) return;
    if (card.classList.contains("allowed") || card.classList.contains("denied")) return;
    card.classList.add(approved ? "allowed" : "denied");
    const choices = card.querySelector(".pr-choices");
    if (choices) choices.remove();
    // 记忆类按钮(总是允许/本会话允许/记住目录)直接挂在卡上, 不在
    // .pr-choices 里——不摘掉的话定格后仍可点击, 且副作用(写白名单/
    // 附加目录)照常发生。在途请求不受影响(用户已表达的意图保留)。
    card.querySelectorAll(".pr-btn").forEach(b => b.remove());
    card.appendChild(makePrMark(
      approved
        ? (card.classList.contains("plan-card") ? "已批准 · 开始实施" : "已允许")
        : "已拒绝",
      approved));
  };
  colOf(sid).querySelectorAll(
    `.perm-row[data-req-id="${requestId}"], .plan-card[data-req-id="${requestId}"]`
  ).forEach(markCard);
  // 右侧面板脚注同步定格（面板正显示这份计划且属于本会话才动——
  // 面板可能已被另一会话的计划占用）
  const pa = $("plan-actions");
  if (pa.dataset.reqId === requestId && pa.dataset.reqSession === sid) {
    pa.innerHTML = "";
    pa.appendChild(makePrMark(approved ? "已批准 · 开始实施" : "已拒绝", approved));
  }
}

/* 桌宠/REST 批复的同步: 服务端广播 permission_resolved, 主窗据此清
 * pendingPerms 登记并定格审批卡。不广播的话主窗永远不知道请求已被
 * 悬浮窗处理——"等待授权…"指示与可点按钮全部滞留到轮次结束。 */
function onPermissionResolved(msg, sid) {
  const run = runOf(sid);
  if (!run || !msg.request_id) return;
  if (run.pendingPerms[msg.request_id]) delete run.pendingPerms[msg.request_id];
  markPermResolved(sid, msg.request_id, !!msg.approved);
  if (sid === state.sessionId) syncThinkingIndicator();   // "等待授权"标签即时纠偏
}

/* 轮次收口: 未决审批卡定格为已拒绝（服务端此刻已朝安全侧 DENY）,
 * 并清空登记。前台后台两条收口路径共用——漏掉哪条, stale 登记都会
 * 让"等待授权…"标签在该会话后续等待窗口里永久滞留。 */
function settlePendingPermsOnTurnEnd(run, sid) {
  for (const rid of Object.keys(run.pendingPerms)) {
    const card = colOf(sid).querySelector(
      `.perm-row[data-req-id="${rid}"], .plan-card[data-req-id="${rid}"]`);
    if (card && !card.classList.contains("allowed") && !card.classList.contains("denied")) {
      card.classList.add("denied");
      const choices = card.querySelector(".pr-choices");
      if (choices) choices.remove();
      // 同 markPermResolved: 记忆类按钮一并摘除, 防定格后仍可点出副作用
      card.querySelectorAll(".pr-btn").forEach(b => b.remove());
      card.appendChild(makePrMark("已拒绝", false));
    }
    // 右侧计划面板若正显示这份计划（且属于本会话）: 按钮区一并定格, 不留死按钮
    if (run.pendingPerms[rid]?.tool_name === "present_plan"
        && $("plan-actions").dataset.reqId === rid
        && $("plan-actions").dataset.reqSession === sid) {
      const pa = $("plan-actions");
      pa.innerHTML = "";
      pa.appendChild(makePrMark("已中断", false));
      $("plan-body").classList.add("stale");
    }
  }
  run.pendingPerms = {};
}

function respondPermission(requestId, approved, sid) {
  const run = runOf(sid);
  if (!run || !run.pendingPerms[requestId]) return;
  delete run.pendingPerms[requestId];
  sendWs({ type: "permission_response", request_id: requestId, approved }, sid);
  markPermResolved(sid, requestId, approved);
}

/* ---------- 轮次结束 / 错误 ---------- */
function endTurnUiReset() {
  const run = curRun();
  if (run) {
    run.busy = false;
    run.awaiting = false;
    run.queued = false;
    run.unread = 0;             // 前台亲眼看完了, 未读清零
    clearRateLimitNote(run);    // 轮次收口: 限流退避提示一并撤下
    flushAssistantBubble(run);
    // 未决审批登记一并收口（服务端已朝安全侧 DENY）: 不清的话 stale
    // 条目会让下个"等待授权"标签永久滞留
    settlePendingPermsOnTurnEnd(run, state.sessionId);
    // 轮次结束还有工具行停在"运行中"（被打断/异常, 结果永远来不了）: 收口
    sweepPendingToolCards(run);
    collapseFinishedToolGroups(colOf(state.sessionId));   // 大分组随轮次结束收起
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
    run.awaitT0 = Date.now();
    if (sid === state.sessionId) syncThinkingIndicator();
  }
}

/* ---------- 接力: 轮到待发送的后续消息了 ---------- */
/* 接力消息转正: 按 qid 逐条撤下对应待发送卡片（服务端把待发送区的多条
 * 消息合并成一轮接力, items 里逐条对应; 旧消息无 qid 时回退按文本匹配）,
 * 并补 user 气泡——计划接力路径发送时已乐观加上（挂 _qid）, 按 qid 查重
 * 跳过防双气泡; 普通排队/其他窗口/历史回放没有 _qid, 在此补上 */
function settleRelayedMessages(msg, sid) {
  const run = runOf(sid);
  const col = colOf(sid);
  const items = Array.isArray(msg.items) && msg.items.length
    ? msg.items
    : [{ qid: msg.qid, text: msg.text, attachments: msg.attachments }];
  items.forEach(it => {
    const qi = run.queue.findIndex(q =>
      it.qid ? q.qid === it.qid : q.text === it.text);
    if (qi >= 0) run.queue.splice(qi, 1);
    const dup = it.qid && col.querySelector(`.msg.user[_qid="${it.qid}"]`);
    if (!dup) addUserBubble(it.text, it.attachments, col, msg.ts);
  });
}

function onTurnStarted(msg, sid) {
  const run = runOf(sid);
  run.busy = true;
  run.awaiting = false;      // 新一轮: 上一轮的空窗状态作废, 等 await_output 重新点亮
  run.lastThinkRow = null;   // 新一轮开始: 打断标记只属于当前轮的思考行
  settleRelayedMessages(msg, sid);
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
  const items = run.queue.slice();
  run.queue.length = 0;
  if (sid === state.sessionId) {
    renderQueueCards();
    const texts = items.map(it => it.text).filter(t => t);
    const cur = $("input").value.trim();
    $("input").value = cur ? cur + "\n" + texts.join("\n") : texts.join("\n");
    autoGrow($("input"));
    // 排队区清空: 附件一并放回附件草稿, 不丢
    const atts = items.flatMap(it => it.attachments || []);
    if (atts.length) setAttachDraft(attachDraftOf().concat(atts));
    saveCurrentInput();
  }
}

function fmtTokens(n) {
  if (typeof n !== "number" || n <= 0) return "";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

/* 轮次用量行: turn_done 带来本轮 token 消耗, 渲染成一条淡色小字。
 * 全零（断线显形/被打断在首个请求前）时不渲染——没有信息量的行是噪音。 */
function addUsageNote(usage) {
  if (!usage || typeof usage !== "object") return;
  const total = (usage.input_tokens || 0) + (usage.output_tokens || 0)
    + (usage.cache_creation_input_tokens || 0) + (usage.cache_read_input_tokens || 0);
  if (!total) return;
  const parts = [];
  const out = fmtTokens(usage.output_tokens);
  const cin = fmtTokens(usage.cache_creation_input_tokens);
  const cred = fmtTokens(usage.cache_read_input_tokens);
  const inp = fmtTokens(usage.input_tokens);
  if (inp) parts.push("输入 " + inp);
  if (out) parts.push("输出 " + out);
  if (cin) parts.push("缓存写 " + cin);
  if (cred) parts.push("缓存读 " + cred);
  addNoteBubble("usage", "本轮 token · " + parts.join(" · "));
}

function onTurnDone(msg) {
  endTurnUiReset();
  addUsageNote(msg.usage);
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
    addBudgetNote(msg.usage);
  } else if (msg.iterations_exhausted) {
    addNoteBubble("warn", `已达单轮最大迭代次数（${msg.iterations} 次调用），已提前收束本轮`);
  }
  // 刷新侧栏标题/消息数，标题可能被自动命名更新
  (async () => { await loadSessions(); refreshDocTitle(); })();
}

/* 预算横幅: 带真实累计输出（与触发预算的计数同口径, 修复前显示的是
 * 最后一次调用的用量）+ 一键"继续"。收束点历史已完整落定, 继续 =
 * 用户手打"继续"发送, 无需任何服务端配合。 */
function addBudgetNote(usage) {
  const div = document.createElement("div");
  div.className = "note warn";
  const out = usage && usage.output_tokens ? fmtTokens(usage.output_tokens) : "";
  div.textContent = "⚠ 本轮输出 token 预算已用尽"
    + (out ? `（累计输出 ${out}，可调大 turnTokenBudget）` : "") + "，已提前收束本轮";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "note-act";
  btn.textContent = "继续";
  btn.onclick = () => { btn.disabled = true; sendFixedText("继续"); };
  div.appendChild(btn);
  msgCol().appendChild(div);
  scrollToBottom();
}

/* 固定文本直接开一轮: 与 sendCurrent 的非草稿、非忙碌路径同构（turn_done
 * 之后必然处于该状态）, 不经过输入框, 不动用户正在打的草稿。 */
function sendFixedText(text) {
  const run = curRun();
  if (!run || !run.ws || run.ws.readyState !== 1) {
    toast("连接未就绪，请手动发送「继续」");
    return;
  }
  addUserBubble(text, []);
  run.busy = true;
  run.awaiting = false;
  run.lastThinkRow = null;   // 新一轮开始: 打断标记只属于当前轮的思考行
  beginOptimisticThinking(run, state.sessionId, msgCol());
  syncThinkingIndicator();
  setBusyUi(true);
  renderSessionList();
  updateSendBtn();
  sendWs({ type: "user", text, attachments: [], qid: genQid() });
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

/* ---------- 压缩续接消息: 模型指令, 非对话内容 ----------
 * 旧版压缩把续接摘要作为 user 消息持久化, 回放显示成一整面墙。
 * 按固定前缀识别（与 compact.py 的 continuation_text 对应）后渲染为
 * 一行式提示卡; 新版压缩不再落盘, 只发 context_compacted 提示。 */
const COMPACT_MARK = "This session is being continued from a previous conversation";
function isCompactNotice(text) { return text.startsWith(COMPACT_MARK); }
function addCompactNotice(col, live) {
  const div = document.createElement("div");
  div.className = "note compact-note";
  div.textContent = (live ? "本轮已自动压缩上下文" : "此处之前的上下文已压缩")
    + "（摘要仅供模型使用，完整对话记录不受影响）";
  (col || msgCol()).appendChild(div);
  scrollToBottom();
}

/* ---------- 限流退避提示: 同一轮的多条原地更新一行, 有进展/收口即撤 ---------- */
function onRateLimitedRetry(msg, sid) {
  const run = runOf(sid);
  // 累计本条思考行里的退避等待: 收口时在"思考 · 持续了 X"后标注,
  // 不让纯 API 等待被读成模型在思考
  run.rlDelayMs = (run.rlDelayMs || 0) + Math.max(0, Number(msg.delay_s) || 0) * 1000;
  let el = run.rlNote;
  if (!el || !el.isConnected) {
    el = document.createElement("div");
    el.className = "note warn rl-note";
    colOf(sid).appendChild(el);
    run.rlNote = el;
  }
  el.textContent =
    `⚠ 触发限流，${Math.round(msg.delay_s)} 秒后进行第 ${msg.attempt}/${msg.max_retries} 次重试…`;
  if (sid === state.sessionId) scrollToBottom();
}

function clearRateLimitNote(run) {
  if (run && run.rlNote) {
    run.rlNote.remove();
    run.rlNote = null;
  }
}

/* ---------- 打断受理反馈: 静默窗口（退避/建连/长工具）内停止不是瞬时的,
 * 明确告诉用户"已受理、正在收束", 避免连点; 轮次收口自动清除 ---------- */
function onTurnInterrupting(msg, sid) {
  const run = runOf(sid);
  let el = run.rlNote;
  if (!el || !el.isConnected) {
    el = document.createElement("div");
    el.className = "note stopped rl-note";
    colOf(sid).appendChild(el);
    run.rlNote = el;
  }
  el.textContent = "正在中断当前轮，模型输出/命令收束后即停止…";
  if (sid === state.sessionId) scrollToBottom();
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

function addUserBubble(text, attachments, col, ts, qid) {
  // 兼容旧签名 addUserBubble(text, col): 第二参传的是列元素
  if (attachments instanceof HTMLElement) { col = attachments; attachments = null; }
  const div = document.createElement("div");
  div.className = "msg user";
  div._text = text;
  div._qid = qid || null;                     // 计划接力: turn_started 回放按 qid 去重
  // 去重选择器查的是 DOM 属性: JS 字段不会自动映射, 必须显式 setAttribute
  if (qid) div.setAttribute("_qid", qid);
  div._ts = ts || new Date().toISOString();   // minimap 相对时间用（历史回放传落盘 ts）
  const b = document.createElement("div");
  b.className = "bubble";
  if (text) {
    const t = document.createElement("div");
    t.className = "u-text";
    t.textContent = text;   // 用户输入永远纯文本
    b.appendChild(t);
  }
  // 附件: 图片缩略图网格 + 文件 chip
  const atts = attachments || [];
  const imgs = atts.filter(a => a.kind === "image");
  const files = atts.filter(a => a.kind === "file");
  if (imgs.length) {
    const grid = document.createElement("div");
    grid.className = "u-imgs" + (imgs.length === 1 ? " solo" : "");   // 单图放大展示
    imgs.forEach((im, i) => {
      const thumb = document.createElement("img");
      thumb.className = "u-img";
      thumb.alt = im.name || "";
      thumb.title = "点击查看大图";
      thumb.loading = "lazy";
      thumb.src = "data:" + (im.media_type || "image/png") + ";base64," + (im.data || "");
      thumb.onclick = () => openLightbox(imgs, i);   // 传整组: 灯箱内可 ←/→ 切换
      grid.appendChild(thumb);
    });
    b.appendChild(grid);
  }
  for (const f of files) b.appendChild(fileChipEl(f.name));
  div.appendChild(b);
  (col || msgCol()).appendChild(div);
  mmScheduleRebuild();
  scrollToBottom();
  return b;
}

/* ---------- 图片灯箱: 缩放 / 拖动 / 多图切换, 点遮罩关闭, Esc 退出 ---------- */
let _lightbox = null;
function openLightbox(list, index) {
  // 兼容旧签名 openLightbox(att): 单对象 → 包成数组
  const single = !Array.isArray(list) ? list : null;
  const imgs = single ? [single] : (list || []);
  if (!imgs.length) return;
  let idx = Math.max(0, Math.min(single ? 0 : (index || 0), imgs.length - 1));
  if (_lightbox) _lightbox.remove();

  const ov = document.createElement("div");
  ov.id = "img-lightbox";
  const img = document.createElement("img");
  img.alt = "";
  img.draggable = false;
  const cap = document.createElement("div");
  cap.className = "lb-cap";
  ov.append(img, cap);

  // 视图状态: scale 为相对适配尺寸的倍率; tx/ty 平移像素。切图即重置。
  let scale = 1, tx = 0, ty = 0;
  const zoomLabel = document.createElement("span");
  zoomLabel.className = "lb-zoom";
  const apply = () => {
    img.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
    zoomLabel.textContent = Math.round(scale * 100) + "%";
  };
  const clampScale = v => Math.max(0.2, Math.min(v, 8));
  // 平移范围: 图像中心最多拖出「自身半径 + 半个视口」, 保证总能拖回来
  const clampPan = () => {
    const r = img.getBoundingClientRect();
    const mx = r.width / 2 + innerWidth / 2;
    const my = r.height / 2 + innerHeight / 2;
    tx = Math.max(-mx, Math.min(mx, tx));
    ty = Math.max(-my, Math.min(my, ty));
  };
  const reset = () => { scale = 1; tx = 0; ty = 0; apply(); };
  // 以视口点 (cx,cy)(相对图片中心) 为锚点缩放, 鼠标下的像素保持不动
  const zoomTo = (ns, cx, cy) => {
    const os = scale;
    scale = clampScale(ns);
    if (cx !== undefined && scale !== os) {
      tx = (tx - cx) * (scale / os) + cx;
      ty = (ty - cy) * (scale / os) + cy;
    }
    clampPan(); apply();
  };

  const SVG = {
    minus: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"/></svg>',
    plus: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    reset: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>',
    prev: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>',
    next: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>'
  };

  const mkBtn = (svg, tip, fn, cls) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "lb-btn" + (cls ? " " + cls : "");
    btn.innerHTML = svg;
    btn.dataset.tip = tip;
    btn.onclick = e => { e.stopPropagation(); fn(); };
    return btn;
  };

  // 顶部工具条: − / 百分比 / ＋ / 重置
  const toolbar = document.createElement("div");
  toolbar.className = "lb-tools";
  toolbar.append(
    mkBtn(SVG.minus, "缩小 (−)", () => zoomTo(scale / 1.25)),
    zoomLabel,
    mkBtn(SVG.plus, "放大 (+)", () => zoomTo(scale * 1.25)),
    mkBtn(SVG.reset, "重置 (双击图片)", reset)
  );

  // 多图切换钮: 单图隐藏
  const multi = imgs.length > 1;
  const nav = d => { idx = (idx + d + imgs.length) % imgs.length; show(); };
  const prevBtn = mkBtn(SVG.prev, "上一张 (←)", () => nav(-1), "lb-nav prev");
  const nextBtn = mkBtn(SVG.next, "下一张 (→)", () => nav(1), "lb-nav next");
  if (!multi) { prevBtn.style.display = "none"; nextBtn.style.display = "none"; }

  const show = () => {
    const att = imgs[idx];
    reset();
    img.src = "data:" + (att.media_type || "image/png") + ";base64," + (att.data || "");
    img.alt = att.name || "";
    cap.textContent = (multi ? (idx + 1) + "/" + imgs.length + " · " : "") + (att.name || "");
  };

  // 滚轮缩放(锚点=鼠标); Ctrl+滚轮留给浏览器缩放
  ov.addEventListener("wheel", e => {
    if (e.ctrlKey) return;
    e.preventDefault();
    const r = img.getBoundingClientRect();
    zoomTo(
      scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15),
      e.clientX - (r.left + r.width / 2),
      e.clientY - (r.top + r.height / 2)
    );
  }, { passive: false });

  // 拖动平移; 未移动的纯点击(遮罩/图片/标题)关闭, 按钮除外
  let drag = null;
  ov.addEventListener("pointerdown", e => {
    if (e.button !== 0 || e.target.closest(".lb-btn")) return;
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, tx, ty, moved: false };
    try { ov.setPointerCapture(e.pointerId); } catch (_) {}
  });
  ov.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 3) return;
    drag.moved = true;
    tx = drag.tx + dx; ty = drag.ty + dy;
    clampPan(); apply();
  });
  const endDrag = e => {
    if (!drag || e.pointerId !== drag.id) return;
    const moved = drag.moved;
    drag = null;
    if (!moved && !e.target.closest(".lb-btn")) close();
  };
  ov.addEventListener("pointerup", endDrag);
  ov.addEventListener("pointercancel", () => { drag = null; });

  // 双击图片: 1x ↔ 2x(以双击点为中心)
  img.addEventListener("dblclick", e => {
    const r = img.getBoundingClientRect();
    if (scale !== 1) reset();
    else zoomTo(2, e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2));
  });

  const onEsc = e => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft" && multi) nav(-1);
    else if (e.key === "ArrowRight" && multi) nav(1);
    else if (e.key === "+" || e.key === "=") zoomTo(scale * 1.25);
    else if (e.key === "-") zoomTo(scale / 1.25);
    else if (e.key === "0") reset();
  };
  const onResize = () => { clampPan(); apply(); };
  const close = () => {
    document.removeEventListener("keydown", onEsc);
    window.removeEventListener("resize", onResize);
    ov.remove();
    _lightbox = null;
  };

  document.addEventListener("keydown", onEsc);
  window.addEventListener("resize", onResize);
  ov.append(toolbar, prevBtn, nextBtn);
  document.body.appendChild(ov);
  _lightbox = ov;
  show();
}

/* ---------- 待发送卡片: ↑立即(插队) / 编辑(放回输入框) / 删除 ---------- */
const Q_PROMOTE_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/></svg>';

function renderQueueCards() {
  const box = $("queue-cards");
  if (!box) return;
  box.innerHTML = "";
  const run = curRun();
  const items = run ? run.queue : [];
  items.forEach(item => {
    const card = document.createElement("div");
    card.className = "q-card";
    const t = document.createElement("span");
    t.className = "q-text";
    t.textContent = item.text || "(仅附件)";
    t.dataset.tip = item.text || "(仅附件)";   // 悬停看全文
    card.appendChild(t);
    // 附件 badge: 🖼2 / 📄1
    const atts = item.attachments || [];
    const nImg = atts.filter(a => a.kind === "image").length;
    const nFile = atts.filter(a => a.kind === "file").length;
    if (nImg || nFile) {
      const badge = document.createElement("span");
      badge.className = "q-badge";
      badge.textContent =
        (nImg ? "\uD83D\uDDBC" + nImg : "")
        + (nImg && nFile ? " " : "")
        + (nFile ? "\uD83D\uDCC4" + nFile : "");
      card.appendChild(badge);
    }
    const promote = document.createElement("button");
    promote.type = "button";
    promote.className = "q-promote";
    promote.innerHTML = Q_PROMOTE_SVG + "<span>立即</span>";
    promote.dataset.tip = "打断当前回复, 待发送的全部消息合并立即发送";
    promote.onclick = () => {
      card.classList.add("promoting");   // 已登记插队, 等当前回复收尾
      sendWs({ type: "queue_promote", qid: item.qid });
    };
    card.appendChild(promote);
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "q-ico";
    edit.innerHTML = PENCIL_SMALL_SVG;
    edit.dataset.tip = "编辑";
    edit.onclick = () => editQueued(item.qid);
    card.appendChild(edit);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "q-ico q-del";
    del.innerHTML = TRASH_SMALL_SVG;
    del.dataset.tip = "删除";
    del.onclick = () => removeQueued(item.qid);
    card.appendChild(del);
    box.appendChild(card);
  });
}

function editQueued(qid) {
  const run = curRun();
  if (!run) return;
  const idx = run.queue.findIndex(it => it.qid === qid);
  if (idx < 0) return;
  const [item] = run.queue.splice(idx, 1);
  sendWs({ type: "queue_remove", qid });
  const input = $("input");
  input.value = input.value ? input.value + "\n" + (item.text || "") : (item.text || "");
  autoGrow(input);
  input.focus();
  // 附件放回附件草稿, 不丢
  const atts = item.attachments || [];
  if (atts.length) setAttachDraft(attachDraftOf().concat(atts));
  saveCurrentInput();
  updateSendBtn();
  renderQueueCards();
}

function removeQueued(qid) {
  const run = curRun();
  if (!run) return;
  const idx = run.queue.findIndex(it => it.qid === qid);
  if (idx < 0) return;
  run.queue.splice(idx, 1);
  sendWs({ type: "queue_remove", qid });
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
  const hasAttach = attachDraftOf().length > 0;   // 有附件无文字也点亮发送
  const busy = !!(curRun() && curRun().busy);
  const btn = $("btn-send");
  const stop = !hasText && !hasAttach && busy;
  btn.dataset.mode = stop ? "stop" : "send";
  btn.dataset.tip = stop ? "中断对话" : "发送";
}
function setBusyUi(busy) {
  // 忙碌态占位符对齐 Claude.ai: 提示可以直接继续排队
  $("input").placeholder = busy ? "继续输入以排队后续修改" : "提出后续修改要求";
  updateSendBtn();
}

/* 本会话是否正卡在计划审批（pendingPerms 全部是 present_plan）。
 * sendCurrent 的"计划接力"路径与思考指示器的标签共用这个判断 */
function isAwaitingPlan(run) {
  if (!run || !run.pendingPerms) return false;
  const pending = Object.values(run.pendingPerms);
  return pending.length > 0 && pending.every(p => p.tool_name === "present_plan");
}

/* 底部"思考中"转圈 = 当前会话忙且正处于等待模型输出的空窗（await_output
 * 起至首个内容事件）。此前各事件分支里手工开关、切换会话不重算:
 * 切到空闲会话转圈残留、后台轮次跑完转圈不灭——统一在这里按当前会话重算。 */
function syncThinkingIndicator() {
  const run = curRun();
  const show = !!(run && run.busy && run.awaiting);
  $("thinking").style.display = show ? "flex" : "none";
  if (show) {
    const pending = run.pendingPerms ? Object.values(run.pendingPerms) : [];
    const t = $("thinking").querySelector(".t");
    let label;
    if (isAwaitingPlan(run)) {
      label = "等待计划审批…";
    } else if (pending.length) {
      label = "等待授权…";
    } else {
      label = "思考中…";
    }
    // 空窗已耗时: 长会话 prefill / 限流退避可达几十秒, 秒数可见才不像卡死
    const t0 = run.awaitT0 || (run.curThinking && run.curThinking.t0) || null;
    const secs = t0 ? Math.floor((Date.now() - t0) / 1000) : 0;
    t.textContent = secs >= 2 ? label + " " + secs + "s" : label;
  }
}
// 空窗计时走秒刷新: 只在转圈可见时重算, 空闲时零开销
setInterval(() => {
  const el = $("thinking");
  if (el && el.style.display !== "none") syncThinkingIndicator();
}, 1000);

async function sendCurrent() {
  const input = $("input");
  const text = input.value.trim();
  const attachments = attachDraftOf().slice();   // 发送快照, 与草稿解耦
  const run = curRun();
  const busy = !!(run && run.busy);
  if (!text && !attachments.length) return;   // 只发图不打字也允许
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
  const qid = genQid();   // 本地生成: 排队卡片与服务端排队区按同一 qid 配对
  // 计划审批挂起时追加 = 否决计划并立刻接力: 不进待发送卡（否则要手动点
  // "立即"才发得上）, 乐观加气泡 + 就地定格旧计划, 服务端打断当前轮后
  // 以这条消息为下一棒开跑（turn_started 回执按 qid 去重, 不重复加气泡）
  if (busy && isAwaitingPlan(runOf(state.sessionId))) {
    const pr = runOf(state.sessionId);
    addUserBubble(text, attachments, msgCol(), null, qid);
    input.value = "";
    setAttachDraft([]);          // 已发送: 清空本会话的附件草稿
    autoGrow(input);
    saveCurrentInput();          // 已发送: 清空本会话的输入草稿
    updateSendBtn();             // 输入已清空: 圆钮切回"停止"形态, 随时可中断
    expirePlanCard(pr, state.sessionId, "已过期 · 继续对话后重新规划");
    sendWs({ type: "user", text, attachments, qid });
    return;
  }
  // 本轮在跑: 消息进入输入框上方的待发送卡片, 轮到它时才出现在消息列
  if (busy) {
    runOf(state.sessionId).queue.push({ qid, text, attachments });
    input.value = "";
    setAttachDraft([]);          // 已发送: 清空本会话的附件草稿
    autoGrow(input);
    saveCurrentInput();          // 已发送: 清空本会话的输入草稿
    updateSendBtn();             // 输入已清空: 圆钮切回"停止"形态, 随时可中断
    renderQueueCards();
    sendWs({ type: "user", text, attachments, qid });
    return;
  }
  addUserBubble(text, attachments);
  input.value = "";
  setAttachDraft([]);          // 已发送: 清空本会话的附件草稿
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
    renderQueueCards();   // 草稿转正: 现在挂在具体会话上（新会话队列必为空, 清掉草稿态可能的残留）
    // 草稿列转正为该会话的消息列（气泡不挪窝）
    colOf("__draft__").id = "msg-col-" + state.sessionId;
    // 列表此刻才出现新条目并选中；WS 建立期间消息会排队，onopen 后冲刷
    await loadSessions();
    renderSessionList();
    markActiveSession();
    refreshDocTitle();
    connectWs(state.sessionId);
  }
  // 草稿态预选的权限模式: 先于首条消息冲进 pendingSends/WS,
  // onopen 按序发送保证服务端在建会话首条消息前就切好模式
  if (state.draftMode) {
    myRun.permissionMode = state.draftMode;
    sendWs({ type: "set_permission_mode", mode: state.draftMode });
    state.draftMode = null;
  }
  sendWs({ type: "user", text, attachments, workdir: firstWorkdir, qid });
}

function sendWs(obj, sid) {
  const run = sid ? runOf(sid) : curRun();
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

/* ---------- 附件入口 1: 📎 按钮 + 隐藏文件选择框 ---------- */
$("btn-attach").onclick = () => $("file-input").click();
$("file-input").addEventListener("change", () => {
  addFiles($("file-input").files);
  $("file-input").value = "";   // 允许重复选择同一文件
});

/* ---------- 附件入口 2: 拖拽到输入卡（dragover 高亮 + drop） ---------- */
const inputCard = $("input-card");
inputCard.addEventListener("dragover", ev => {
  ev.preventDefault();
  inputCard.classList.add("dragging");
});
inputCard.addEventListener("dragleave", () => inputCard.classList.remove("dragging"));
inputCard.addEventListener("drop", ev => {
  ev.preventDefault();
  inputCard.classList.remove("dragging");
  if (ev.dataTransfer && ev.dataTransfer.files.length) addFiles(ev.dataTransfer.files);
});

/* ---------- 附件入口 3: 粘贴剪贴板里的图片 ---------- */
$("input").addEventListener("paste", ev => {
  const files = ev.clipboardData && ev.clipboardData.files;
  if (files && files.length) {
    ev.preventDefault();
    addFiles(files);
  }
});

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
  if (DESKTOP) return;   // 桌面端: 自建右键菜单已挂, 浏览器版弹层不用
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
const MODE_ITEMS = [
  { value: "plan", label: "计划模式", icon: ICON_MODE_PLAN,
    desc: "先研究并给出计划，批准后自动开始实施。" },
  { value: "prompt", label: "每次询问", icon: ICON_MODE_HAND,
    desc: "改动前先征求我的意见。" },
  { value: "workspace-write", label: "自动编辑", icon: ICON_MODE_PENCIL,
    desc: "自动编辑工作区内的文件。" },
  { value: "danger-full-access", label: "完全访问", icon: ICON_MODE_SHIELD,
    desc: "减少确认次数，放开全部权限。" },
  // allow 不提供: 后端拒绝从设置/会话进入（连将来需要问的工具也一并放行,
  // 只允许 CLI REPL /mode allow 临时开启）
];
const THINK_ITEMS = [
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
  { value: "max", label: "最高" },
];
const modeDd = makeDropdown($("sel-mode"), {
  items: MODE_ITEMS, value: "prompt",
  onChange: v => {
    // 会话级: 切的是当前会话的模式（全局默认值在设置页改, 是新会话初值）
    const run = curRun();
    if (run) {
      run.permissionMode = v;
      sendWs({ type: "set_permission_mode", mode: v });
    } else {
      // 草稿态: 暂存, 建会话后先于首条消息下发（sendCurrent）
      state.draftMode = v;
    }
  },
});
function onModeChanged(msg, sid) {
  const run = runOf(sid);
  run.permissionMode = msg.permission_mode;
  // 列表缓存同步: 否则下次 renderSessionList/loadSessions 会用旧值,
  // 切会话时下拉框停留在别的会话的模式上（看起来像串了）
  const s = state.sessions.find(x => x.id === sid);
  if (s) s.permission_mode = msg.permission_mode;
  if (sid === state.sessionId) modeDd.setValue(msg.permission_mode);
}
const thinkDd = makeDropdown($("sel-thinking"), {
  items: THINK_ITEMS, value: "medium",
  onChange: v => {
    // 会话级: 有会话切本会话; 草稿态（无会话）切全局默认（新会话初值）
    const run = curRun();
    if (run) {
      run.thinkingLevel = v;
      sendWs({ type: "set_thinking_level", level: v });
    } else {
      saveSettings({ thinking_level: v });
    }
  },
});
function onThinkingChanged(msg, sid) {
  const run = runOf(sid);
  run.thinkingLevel = msg.thinking_level;
  const s = state.sessions.find(x => x.id === sid);
  if (s) s.thinking_level = msg.thinking_level;
  if (sid === state.sessionId) thinkDd.setValue(msg.thinking_level);
}
/* 模型下拉: 选项来自所有启用供应商的模型（loadProviders 后填充） */
const modelDd = makeDropdown($("sel-model"), {
  items: [], value: "",
  onChange: v => {
    const [provider_id, model_id] = v.split("|");
    // 会话级: 有会话切本会话; 草稿态（无会话）切全局 active（新会话初值）
    const run = curRun();
    if (run) {
      run.modelKey = v;
      sendWs({ type: "set_model", provider_id, model_id });
    } else {
      saveSettings({ provider_id, model_id });
    }
  },
});
function onModelChanged(msg, sid) {
  const run = runOf(sid);
  const key = msg.provider_id + "|" + msg.model_id;
  run.modelKey = key;
  const s = state.sessions.find(x => x.id === sid);
  if (s) { s.model_provider = msg.provider_id; s.model_id = msg.model_id; }
  if (sid === state.sessionId) modelDd.setValue(key);
}

async function loadSettings() {
  try {
    const [r, pr] = await Promise.all([fetch("/api/settings"), fetch("/api/providers")]);
    const s = await r.json();
    state.providerCfg = await pr.json();
    syncModelDropdown(s.provider_id, s.model_id);
    modeDd.setValue(s.permission_mode);
    thinkDd.setValue(s.thinking_level);
    // 全局默认值快照: 供切会话/草稿态回显兜底（见 state.globalDefaults 注释）
    state.globalDefaults = {
      permissionMode: s.permission_mode || "prompt",
      thinkingLevel: s.thinking_level || null,
      modelKey: s.provider_id && s.model_id ? s.provider_id + "|" + s.model_id : null,
    };
    if (s.max_iterations != null) {
      state.serverMaxIter = s.max_iterations;
      $("set-max-iter").value = String(s.max_iterations);
    }
    if (s.workspace) $("ws-tag-text").textContent = s.workspace;
    state.serverWorkspace = s.workspace || null;
    state.configured = s.configured !== false;   // 旧服务端无此字段时视为已配置
    state.defaultModel = s.model || null;
    refreshWorkdirTag();
    if (s.icon_ver) { iconVer = s.icon_ver; applyIconEverywhere(iconUrl()); }
    if (s.bg_ver) { bgVer = s.bg_ver; syncBgLayers(); }
    else syncBgLayers();   // 服务端无壁纸: 走一遍以清掉本地残留标记的效果
    // 标题栏版本徽标: 桌面壳优先（打包后的后端不带 pyproject.toml, 服务端读不到）,
    // 服务端值兜底（浏览器/源码运行）。都拿不到就藏着, 不留空壳。
    const ver = $("tb-version");
    let v = null;
    if (window.xcodeAppVersion) {
      try { v = await window.xcodeAppVersion(); } catch (_) { /* 壳异常 → 兜底 */ }
    }
    if (!v) v = s.app_version || null;
    if (ver && v) { ver.textContent = "v" + v; ver.hidden = false; }
  } catch (e) { console.error("加载设置失败", e); }
}

/* ---------- 应用图标: 设置 → 外观 可上传替换, 服务端落盘 ~/.x-code/appearance/icon.png ---------- */
let iconVer = 0;   // 图标文件版本（mtime）: 用 ?v= 穿透浏览器缓存
const iconUrl = () => "/api/icon" + (iconVer ? `?v=${iconVer}` : "");
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
/* 左栏当前选中的供应商（仅前端内存, 切换只是换右侧表单, 不丢未保存修改） */
let provSelectedId = null;
function provById(id) {
  return (state.providerCfg?.providers || []).find(p => p.id === id);
}
/* 左栏列表项: 名称 + 启用状态圆点（绿=启用, 灰=停用） */
function buildProvItem(p) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "prov-item" + (p.id === provSelectedId ? " on" : "");
  item.dataset.id = p.id;
  const name = document.createElement("span");
  name.className = "prov-item-name";
  name.textContent = p.name || "未命名供应商";
  const dot = document.createElement("i");
  dot.className = "dot" + (p.enabled !== false ? " on" : "");
  item.append(name, dot);
  item.onclick = () => {
    if (provSelectedId === p.id) return;
    provSelectedId = p.id;
    document.querySelectorAll("#prov-list .prov-item")
      .forEach(x => x.classList.toggle("on", x === item));
    renderProvDetail();
  };
  return item;
}
/* 右侧详情: 无选中时给空态提示 */
function renderProvDetail() {
  const wrap = $("prov-detail");
  if (!wrap) return;
  wrap.innerHTML = "";
  const p = provById(provSelectedId);
  if (!p) {
    const empty = document.createElement("div");
    empty.className = "prov-empty";
    empty.textContent = "左侧选择供应商，或点击「＋ 添加供应商」";
    wrap.appendChild(empty);
    return;
  }
  wrap.appendChild(buildProviderCard(p));
}
function renderProviderSettings() {
  const list = $("prov-list");
  if (!list) return;
  const ps = state.providerCfg?.providers || [];
  // 选中项校验: 空或已被删除时回退到第一个供应商
  if (!ps.some(p => p.id === provSelectedId)) provSelectedId = ps[0]?.id ?? null;
  list.innerHTML = "";
  for (const p of ps) list.appendChild(buildProvItem(p));
  renderProvDetail();
}
/* 左栏单项就地同步（名称/圆点）, 不重绘整栏以免打断输入焦点 */
function syncProvItem(p) {
  const item = document.querySelector(`#prov-list .prov-item[data-id="${CSS.escape(p.id)}"]`);
  if (!item) return;
  item.querySelector(".prov-item-name").textContent = p.name || "未命名供应商";
  item.querySelector(".dot").classList.toggle("on", p.enabled !== false);
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
  name.addEventListener("input", () => { p.name = name.value; syncProvItem(p); scheduleProvSave(); });
  const en = document.createElement("label");
  en.className = "prov-switch";
  const enBox = document.createElement("input");
  enBox.type = "checkbox";
  enBox.checked = p.enabled !== false;
  enBox.addEventListener("change", () => { p.enabled = enBox.checked; syncProvItem(p); scheduleProvSave(); });
  const track = document.createElement("i");
  track.className = "track";
  en.append(enBox, track, document.createTextNode("已启用"));
  const del = document.createElement("button");
  del.type = "button";
  del.className = "prov-del";
  del.innerHTML = TRASH_SMALL_SVG;
  del.dataset.tip = "删除供应商";
  del.onclick = async () => {
    if (!await confirmDialog(`删除供应商「${p.name}」？其模型将从下拉中移除。`,
        { title: "删除供应商", okText: "删除", danger: true })) return;
    state.providerCfg.providers = state.providerCfg.providers.filter(x => x !== p);
    if (state.providerCfg.active && state.providerCfg.active.provider === p.id) {
      state.providerCfg.active = {};
    }
    if (provSelectedId === p.id) provSelectedId = null;   // 回退逻辑在 renderProviderSettings 里
    renderProviderSettings();
    scheduleProvSave();   // 删除也自动保存
  };
  head.append(name, en, del);
  card.appendChild(head);

  /* 连接配置: Base URL / API KEY / 接口协议 三列 */
  const grid = document.createElement("div");
  grid.className = "prov-grid";
  const urlField = document.createElement("div");
  urlField.className = "prov-field";
  urlField.innerHTML = "<label>BASE URL</label>";
  const url = document.createElement("input");
  url.className = "set-input mono";
  url.value = p.base_url || "";
  // 占位示例按协议切换: 两个端点形状不同, 配错是 403/404 的常见根源
  const URL_HINTS = {
    anthropic: "https://open.bigmodel.cn/api/anthropic",
    openai: "https://open.bigmodel.cn/api/coding/paas/v4",
  };
  url.placeholder = URL_HINTS[p.protocol || "anthropic"] || "https://...";
  url.addEventListener("input", () => { p.base_url = url.value; scheduleProvSave(); });
  urlField.appendChild(url);
  const protoField = document.createElement("div");
  protoField.className = "prov-field";
  protoField.innerHTML = "<label>接口协议</label>";
  const proto = document.createElement("select");
  proto.className = "set-input";
  proto.title = "anthropic: Claude/Messages 协议端点; openai: OpenAI Chat Completions 协议端点";
  for (const [val, label] of [
    ["anthropic", "Anthropic（Claude 协议）"],
    ["openai", "OpenAI（Chat Completions）"],
  ]) {
    const opt = document.createElement("option");
    opt.value = val;
    opt.textContent = label;
    proto.appendChild(opt);
  }
  proto.value = p.protocol || "anthropic";
  proto.addEventListener("change", () => {
    p.protocol = proto.value;
    url.placeholder = URL_HINTS[proto.value] || "https://...";
    scheduleProvSave();
  });
  protoField.appendChild(proto);
  const keyField = document.createElement("div");
  keyField.className = "prov-field";
  keyField.innerHTML = "<label>API KEY</label>";
  const keyRow = document.createElement("div");
  keyRow.className = "key-row";
  const key = document.createElement("input");
  key.type = "password";
  key.className = "set-input mono";
  key.value = p.api_key || "";
  key.addEventListener("input", () => { p.api_key = key.value; scheduleProvSave(); });
  const eye = document.createElement("button");
  eye.type = "button";
  eye.className = "key-eye";
  eye.textContent = "👁";
  eye.onclick = () => { key.type = key.type === "password" ? "text" : "password"; };
  keyRow.append(key, eye);
  keyField.appendChild(keyRow);
  grid.append(urlField, protoField, keyField);
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
    nm.addEventListener("input", () => { m.name = nm.value; scheduleProvSave(); });
    const id = document.createElement("input");
    id.className = "set-input m-id mono";
    id.value = m.id || "";
    id.placeholder = "模型 ID（API 名）";
    id.addEventListener("input", () => { m.id = id.value; scheduleProvSave(); });
    const tg = document.createElement("input");
    tg.className = "set-input m-tags";
    tg.value = (m.tags || []).join(",");
    tg.placeholder = "标签（逗号分隔，如 视觉,1M）";
    tg.addEventListener("input", () => {
      m.tags = tg.value.split(/[,，]/).map(x => x.trim()).filter(Boolean);
      scheduleProvSave();
    });
    const delB = document.createElement("button");
    delB.type = "button";
    delB.className = "m-del";
    delB.textContent = "✕";
    delB.dataset.tip = "删除模型";
    delB.onclick = () => {
      p.models = p.models.filter(x => x !== m);
      row.remove();
      scheduleProvSave();
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
    scheduleProvSave();   // 新行落盘; 模型 id 允许为空, 服务端只校验列表存在
  };
  mField.appendChild(addM);
  card.appendChild(mField);
  return card;
}

$("btn-add-provider").onclick = () => {
  const id = "prov-" + Date.now().toString(36);
  const p = {
    id, name: "新供应商", base_url: "", api_key: "", enabled: true,
    protocol: "anthropic",
    models: [{ id: "", name: "", tags: [] }],
  };
  state.providerCfg.providers.push(p);
  provSelectedId = id;   // 新增即选中
  renderProviderSettings();
  const name = $("prov-detail").querySelector(".prov-name");
  if (name) { name.focus(); name.select(); }
  scheduleProvSave();   // 新增即自动保存; 缺 Base URL 时红字提示, 填好自动补存
};

/* ---------- 设置 → 外观: 背景图片（亚克力磨砂的"壁纸"） ----------
 * 与应用图标同模式: POST /api/bg 落盘 ~/.x-code/appearance/bg-user.png, localStorage 只存
 * 启用标记（xc-bg=1）。应用方式: <html data-bg="1"> 让遮罩/半透明令牌生效
 * （预绘制脚本抢在首帧前设置, 避免闪烁）; 壁纸本体由 syncBgLayers 预加载
 * 成功后再写到 body 内联背景上, 避免解码期间半成品闪烁。 */
const bgUrl = () => "/api/bg" + (bgVer ? `?v=${bgVer}` : "");
function bgPref() { return localStorage.getItem(BG_KEY) === "1"; }

function syncBgLayers() {
  const on = bgPref() && bgVer > 0;
  const bi = $("bg-bright"); if (bi) bi.disabled = !on;   // 无壁纸时滑块无意义
  if (on) document.documentElement.dataset.bg = "1";
  else delete document.documentElement.dataset.bg;
  if (!on) {
    document.body.style.backgroundImage = "";
    return;
  }
  const img = new Image();
  img.onload = () => {
    if (!bgPref()) return;               // 加载期间被清除了
    document.body.style.backgroundImage = `url("${bgUrl()}")`;
    document.body.style.backgroundSize = "cover";
    document.body.style.backgroundPosition = "center";
  };
  img.src = bgUrl();
}
/* 启动装载: 本地标记开启才发请求拿壁纸（版本号稍后由 /api/settings 校准,
 * 校准值若不同, loadSettings 里会再跑一遍本函数换新地址） */
syncBgLayers();

$("btn-bg-upload").onclick = () => $("bg-file").click();
$("bg-file").addEventListener("change", () => {
  const file = $("bg-file").files[0];
  $("bg-file").value = "";   // 清空: 允许重复选择同一文件
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) { toast("图片过大（限 20MB）"); return; }
  const rd = new FileReader();
  rd.onload = async () => {
    const data = String(rd.result || "");
    if (!/^data:image\/(png|jpeg|webp);base64,/.test(data)) {
      toast("仅支持 PNG / JPEG / WebP");
      return;
    }
    try {
      const r = await fetch("/api/bg", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data }),
      });
      if (!r.ok) throw new Error((await r.json()).detail || "HTTP " + r.status);
      bgVer = (await r.json()).ver;
      localStorage.setItem(BG_KEY, "1");   // 上传即启用
      syncBgLayers();
      toast("背景已更新");
    } catch (e) { toast("背景更新失败: " + e.message); }
  };
  rd.readAsDataURL(file);
});
$("btn-bg-clear").onclick = async () => {
  try {
    const r = await fetch("/api/bg", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: null }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "HTTP " + r.status);
    bgVer = 0;
    localStorage.removeItem(BG_KEY);
    syncBgLayers();
    toast("已清除背景图");
  } catch (e) { toast("清除失败: " + e.message); }
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

/* ---------- 设置 → 行为: 每轮最大迭代次数（新会话的默认值） ---------- */
function maxIterValid(v) { return Number.isInteger(v) && v >= 1 && v <= 10000; }
function currentMaxIter() {
  const v = parseInt($("set-max-iter").value, 10);
  return maxIterValid(v) ? v : null;
}
{
  const inp = $("set-max-iter");
  const submit = async () => {
    const cur = state.serverMaxIter;
    if (inp.value.trim() === "") {   // 清空 = 放弃编辑, 回显当前值
      if (cur != null) inp.value = String(cur);
      inp.classList.remove("invalid");
      return;
    }
    const v = parseInt(inp.value, 10);
    if (!maxIterValid(v)) { inp.classList.add("invalid"); return; }
    inp.classList.remove("invalid");
    await saveSettings({ max_iterations: v });
    toast("已保存，对新会话生效");
  };
  inp.addEventListener("blur", submit);
  inp.addEventListener("keydown", ev => {
    ev.stopPropagation();   // 别让 Enter/Esc 冒泡成全局快捷键
    if (ev.key === "Enter") ev.target.blur();
  });
}

/* ---------- 设置 → 配置文件: 用系统默认编辑器打开 settings.json ---------- */
$("btn-open-config").onclick = async () => {
  try {
    const r = await fetch("/api/open-config", { method: "POST" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
    toast("已打开配置文件");
  } catch (e) { toast("打开失败: " + e.message); }
};
/* ---------- 自动保存: 脏标记 + 800ms 防抖, 合并连续输入为一次请求 ----------
 * 编辑只改内存 state.providerCfg; 防抖窗口内反复触发只重置计时器, 停顿后才
 * 真正 POST。启用的供应商缺 Base URL 属无效中间态: 置脏但不发请求, 红字提示,
 * 待填好停顿后自动补存 —— 打字/粘贴中途绝不打扰后端。 */
let provSaveTimer = null;
let provDirty = false;
const PROV_SAVE_DELAY = 800;
function provStatus(text, isErr = false) {
  const el = $("prov-save-status");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("err", isErr);
}
function scheduleProvSave() {
  provDirty = true;
  provStatus("未保存…");
  clearTimeout(provSaveTimer);
  provSaveTimer = setTimeout(persistProviders, PROV_SAVE_DELAY);
}
async function persistProviders() {
  clearTimeout(provSaveTimer);
  provSaveTimer = null;
  if (!provDirty || !state.providerCfg) return;
  // 接口地址必填: 留空会回退到错误的服务端点, 是 403 类问题的根源。
  // 无效中间态不落盘, 等用户填好后的下一次防抖自动保存。
  for (const p of (state.providerCfg.providers || [])) {
    if (p.enabled !== false && !(p.base_url || "").trim()) {
      provStatus(`「${p.name || "未命名供应商"}」缺少 Base URL, 暂未保存`, true);
      return;
    }
  }
  provStatus("保存中…");
  try {
    const r = await fetch("/api/providers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.providerCfg),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      provStatus("保存失败: " + (err.detail || r.status), true);
      toast("保存失败: " + (err.detail || r.status));
      return;
    }
    const saved = await r.json();
    provDirty = false;
    /* 不重绘表单（重建 DOM 会丢输入焦点）: 只把服务端归一化后的
     * base_url / protocol 按 id 原位写回现有对象, 保持引用不变,
     * 输入框闭包依旧生效; 名称等非归一化字段以本地为准, 避免覆盖正在输入的内容 */
    for (const sp of (saved.providers || [])) {
      const lp = (state.providerCfg.providers || []).find(x => x.id === sp.id);
      if (!lp) continue;
      lp.base_url = sp.base_url;
      lp.protocol = sp.protocol;
    }
    provStatus("已保存");
    // 刷新 composer 模型下拉; 仅当当前选中项已不存在时才走全量 loadSettings 兜底
    const cur = modelDd.getValue();
    modelDd.setItems(modelItems(), cur);
    if (cur && !modelItems().some(i => i.value === cur)) await loadSettings();
  } catch (e) {
    provStatus("保存失败: " + e.message, true);
    toast("保存失败: " + e.message);
  }
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
    thinkDd.setValue(s.thinking_level);
    if (!state.sessionId) modeDd.setValue(s.permission_mode);   // 草稿态: 无会话, 显示全局默认
    // 草稿态下 REST 修改的全局默认, 同步进快照（切会话/下次进草稿的兜底值）
    if (s.thinking_level != null) state.globalDefaults.thinkingLevel = s.thinking_level;
    if (s.permission_mode) state.globalDefaults.permissionMode = s.permission_mode;
    if (s.max_iterations != null) $("set-max-iter").value = String(s.max_iterations);
    if (s.provider_id && s.model_id) {
      state.globalDefaults.modelKey = s.provider_id + "|" + s.model_id;
      if (!state.sessionId) modelDd.setValue(state.globalDefaults.modelKey);
    }
    return s;
  } catch (e) { toast("设置失败: " + e.message); }
}

/* ============================================================
 * 设置弹窗: 主题外观（深色 / 浅色 / 跟随系统）
 * ============================================================ */
const THEME_ITEMS = [
  { value: "dark", label: "深色" },
  { value: "light", label: "浅色" },
  { value: "system", label: "跟随系统" },
  { value: "acrylic", label: "亚克力（深色）" },
  { value: "acrylic-light", label: "亚克力（浅色）" },
];
const themeDd = makeDropdown($("sel-theme"), {
  items: THEME_ITEMS, value: themePref(),
  onChange: v => {
    localStorage.setItem(THEME_KEY, v);
    applyTheme();
    applyFx();
    applyAccentVars();   // 自定义色的派生令牌跟随深浅主题
    syncAccentInput();
  },
});

/* ---------- 通知: 完成提示音 + 桌面通知, 开关即时生效并持久化 ---------- */
const ONOFF_ITEMS = [{ value: "1", label: "开启" }, { value: "0", label: "关闭" }];
const writeBoolPref = key => v => localStorage.setItem(key, v);
const soundDd = makeDropdown($("sel-notify-sound"), {
  items: ONOFF_ITEMS, value: notifySoundPref() ? "1" : "0",
  onChange: writeBoolPref(NOTIFY_SOUND_KEY),
});
const desktopDd = makeDropdown($("sel-notify-desktop"), {
  items: ONOFF_ITEMS, value: notifyDesktopPref() ? "1" : "0",
  onChange: v => {
    writeBoolPref(NOTIFY_DESKTOP_KEY)(v);
    if (v === "1") {
      // 用户手势上下文里才申请权限（浏览器禁止静默弹权限框）
      if (ensureNotifyPermission() !== "granted") toast("浏览器未授予通知权限，弹窗将不生效");
    }
  },
});
$("btn-notify-test").onclick = () => {
  playChime();
  // 桌面壳: 直接发一条原生 toast, 让用户当场验证系统通知链路通不通
  if (DESKTOP) {
    nativeNotify("x-code 桌面通知测试", "收到这条说明原生通知链路正常");
    toast("已播放提示音并发送系统通知");
    return;
  }
  // 未授权时回落 toast, 让用户立刻知道桌面弹窗这条路通不通
  if ("Notification" in window && Notification.permission !== "granted") {
    ensureNotifyPermission();
    toast("已播放提示音；桌面通知未授权" +
      (Notification.permission === "denied" ? "（被浏览器拒绝）" : "，可再点一次确认授权"));
  }
};

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
  // 亚克力下 soft 必须半透明: 浅色默认分支混白是实色, 内联样式优先级高于
  // CSS 令牌块, 会盖掉亚克力的 --accent-soft 覆盖 → 用户气泡死白
  st.setProperty("--accent-soft",
    themeIsAcrylic() ? `rgba(${r}, ${g}, ${b}, ${dark ? .14 : .12})`
    : dark ? `rgba(${r}, ${g}, ${b}, .13)` : mixHex(c, "#ffffff", 0.9));
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

/* ---------- 界面 / 聊天内容字号: 直接输入像素值, 各自独立 ----------
 * --fs-ui  界面文字（侧栏/设置/按钮等）, 基准 14px
 * --fs-chat 聊天内容（气泡正文/代码块/diff 行）, 基准 14px
 * 两个键、两个变量, 互不影响。 */
const FS_UI_KEY = "xc-fs-ui";
const FS_CHAT_KEY = "xc-fs-chat";
const FS_UI_BASE = 14;      // CSS 基准: 界面基础字号
const FS_CHAT_BASE = 14;    // CSS 基准: 聊天内容基础字号
const FS_UI_RANGE = [10, 24];
const FS_CHAT_RANGE = [10, 24];
function fsPref(key, base, range) {
  const v = parseFloat(localStorage.getItem(key));
  return Number.isFinite(v) && v >= range[0] && v <= range[1] ? v : base;
}
function fsUiPref()   { return fsPref(FS_UI_KEY, FS_UI_BASE, FS_UI_RANGE); }
function fsChatPref() { return fsPref(FS_CHAT_KEY, FS_CHAT_BASE, FS_CHAT_RANGE); }
function applyFontSize() {
  const st = document.documentElement.style;
  st.setProperty("--fs-ui", (fsUiPref() / FS_UI_BASE).toFixed(4));
  st.setProperty("--fs-chat", (fsChatPref() / FS_CHAT_BASE).toFixed(4));
}
/* 输入即时生效; 清空或非法值在失焦时回退默认并回显 */
function bindFsInput(inpId, key, base, range) {
  const inp = $(inpId);
  inp.addEventListener("input", () => {
    const v = parseFloat(inp.value.trim());
    const ok = Number.isFinite(v) && v >= range[0] && v <= range[1];
    inp.classList.toggle("invalid", !ok);
    if (ok) { localStorage.setItem(key, String(v)); applyFontSize(); }
  });
  inp.addEventListener("keydown", ev => {
    ev.stopPropagation();   // 别让 Enter/Esc 冒泡成全局快捷键
    if (ev.key === "Enter") ev.target.blur();
  });
  inp.addEventListener("blur", () => {
    const v = parseFloat(inp.value.trim());
    if (!(Number.isFinite(v) && v >= range[0] && v <= range[1])) localStorage.removeItem(key);
    inp.value = String(fsPref(key, base, range));
    inp.classList.remove("invalid");
    applyFontSize();
  });
}
bindFsInput("fs-ui-input", FS_UI_KEY, FS_UI_BASE, FS_UI_RANGE);
bindFsInput("fs-code-input", FS_CHAT_KEY, FS_CHAT_BASE, FS_CHAT_RANGE);
applyFontSize();

/* ---------- 壁纸亮度: 0-100 滑块, 50=默认观感, 持久化 localStorage ----------
 * 只缩放压暗层(遮罩 alpha / 亚克力 tint / 表面不透明度), 不动实底卡片:
 * --bg-dim = 2 - v/50 (v=50→1 现状, v=100→0 不压暗, v=0→2 加倍压暗)
 * --bg-surf = 1 - 0.26*clamp((v-50)/50) 限幅 (v>50 更透更亮, v<50 更实更暗;
 *   用减号: 若用加号 v=100 会算出 1.26, color-mix 份额超 100% 被归一化成全实底)
 * 注意: index.html head 预绘制脚本复制了同一套公式, 改这里必须同步改那边 */
const BG_BRIGHT_KEY = "xc-bg-bright";
const BG_BRIGHT_DEFAULT = 50;
function bgBrightPref() {
  const v = parseFloat(localStorage.getItem(BG_BRIGHT_KEY));
  return Number.isFinite(v) && v >= 0 && v <= 100 ? v : BG_BRIGHT_DEFAULT;
}
function applyBgBrightness() {
  const v = bgBrightPref();
  const st = document.documentElement.style;
  st.setProperty("--bg-dim", (2 - v / 50).toFixed(4));
  st.setProperty("--bg-surf", (1 - 0.26 * Math.max(-1, Math.min(1, (v - 50) / 50))).toFixed(4)); // 与 index.html 预绘制脚本同步
  const inp = $("bg-bright"), out = $("bg-bright-val");
  if (inp) { inp.value = String(v); out.textContent = String(v); }
}
{
  const inp = $("bg-bright");
  inp.addEventListener("input", () => {
    const v = Math.round(parseFloat(inp.value));
    localStorage.setItem(BG_BRIGHT_KEY, String(v));
    applyBgBrightness();
  });
  inp.addEventListener("keydown", ev => ev.stopPropagation()); // 别冒泡成全局快捷键
}
applyBgBrightness();

/* ---------- 设置 → MCP 服务器: 左列表 + 右表单, 防抖自动保存并热生效 ----------
 * 数据模型直接用 settings.json 的 mcpServers 原始结构（name → spec）,
 * 保存 POST /api/mcp/servers: 服务端校验 → 落盘 → 热应用（连接新服务器、
 * 断开删除的）。连接状态来自同一响应, 显示在每个列表项与表单头部。 */
let mcpCfg = { mcpServers: {} };   // 工作副本（编辑只动内存, 停顿后落盘）
let mcpStatuses = [];              // 最近一次服务端返回的连接状态
let mcpSelected = null;            // 左栏选中的服务器名
let mcpSaveTimer = null;
let mcpDirty = false;
const MCP_SAVE_DELAY = 800;
/* 显示名 → JSON key: 首次创建时用。key 决定工具名前缀 mcp__<key>__*, 只在
 * 建名时约束; 后续改名 = 删除旧服务器 + 新建, 不偷偷改 key（会断开重连） */
function mcpKeyFor(name) {
  const k = (name || "").trim().replace(/[^A-Za-z0-9_-]/g, "-")
    .replace(/^-+|-+$/g, "") || "server";
  // 撞名兜底: 追加序号保证 key 唯一
  let key = k, i = 2;
  while (mcpCfg.mcpServers[key] != null) key = `${k}-${i++}`;
  return key;
}
function mcpStatusOf(name) {
  return mcpStatuses.find(s => s.name === name);
}
function mcpStatus(text, isErr = false) {
  const el = $("mcp-save-status");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("err", isErr);
}
function scheduleMcpSave() {
  mcpDirty = true;
  mcpStatus("未保存…");
  clearTimeout(mcpSaveTimer);
  mcpSaveTimer = setTimeout(persistMcpServers, MCP_SAVE_DELAY);
}
async function persistMcpServers() {
  clearTimeout(mcpSaveTimer);
  mcpSaveTimer = null;
  if (!mcpDirty) return;
  // 无效中间态不落盘（口径与 /api/mcp/servers 校验一致）, 红字提示待补全
  for (const [name, spec] of Object.entries(mcpCfg.mcpServers)) {
    const t = spec.type || "stdio";
    if (t === "stdio" && !(spec.command || "").trim()) {
      mcpStatus(`「${name}」缺少 command, 暂未保存`, true);
      return;
    }
    if (t !== "stdio" && !(spec.url || "").trim()) {
      mcpStatus(`「${name}」缺少 URL, 暂未保存`, true);
      return;
    }
  }
  mcpStatus("保存并连接中…");
  try {
    const r = await fetch("/api/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mcpCfg),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      mcpStatus("保存失败: " + (err.detail || r.status), true);
      toast("MCP 保存失败: " + (err.detail || r.status));
      return;
    }
    const saved = await r.json();
    mcpDirty = false;
    mcpCfg = { mcpServers: saved.mcpServers || mcpCfg.mcpServers };
    mcpStatus(saved.servers.every(s => s.status !== "connected")
      ? "已保存（无已连接服务器）" : "已保存");
    renderMcpSettings();
  } catch (e) {
    mcpStatus("保存失败: " + e.message, true);
    toast("保存失败: " + e.message);
  }
}
async function loadMcpServers() {
  try {
    const d = await fetch("/api/mcp/servers").then(r => r.json());
    mcpCfg = { mcpServers: d.mcpServers || {} };
    mcpStatuses = d.status || [];
    mcpDirty = false;
  } catch (e) { console.error("加载 MCP 配置失败", e); }
}
/* 左栏列表项: 名称 + 连接状态点（绿=已连接, 红=失败, 灰=未连） */
function buildMcpItem(name) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "prov-item" + (name === mcpSelected ? " on" : "");
  item.dataset.id = name;
  const label = document.createElement("span");
  label.className = "prov-item-name";
  label.textContent = name;
  const dot = document.createElement("i");
  const st = mcpStatusOf(name)?.status;
  dot.className = "dot" + (st === "connected" ? " on" : st === "failed" ? " bad" : "");
  if (st === "failed") {
    const err = mcpStatusOf(name)?.error || "";
    dot.title = err;
    label.title = `${name} — ${err}`;
  } else {
    dot.title = st === "connected" ? "已连接" : "未连接";
  }
  item.append(label, dot);
  item.onclick = () => {
    if (mcpSelected === name) return;
    mcpSelected = name;
    document.querySelectorAll("#mcp-list .prov-item")
      .forEach(x => x.classList.toggle("on", x === item));
    renderMcpDetail();
  };
  return item;
}
/* 单行字段: label + input（存取都走 spec 对象, 变更即标脏） */
function renderMcpDetail() {
  const wrap = $("mcp-detail");
  if (!wrap) return;
  wrap.innerHTML = "";
  const spec = mcpCfg.mcpServers[mcpSelected];
  if (!spec) {
    const empty = document.createElement("div");
    empty.className = "prov-empty";
    empty.textContent = "左侧选择服务器，或点击「＋ 添加服务器」";
    wrap.appendChild(empty);
    return;
  }
  const transport = spec.type || "stdio";
  const st = mcpStatusOf(mcpSelected);
  const card = document.createElement("div");
  card.className = "prov-card";

  /* 头部: 显示名（=key, 即工具前缀）/ 传输类型 / 连接状态 / 删除 */
  const head = document.createElement("div");
  head.className = "prov-head";
  const name = document.createElement("input");
  name.className = "prov-name";
  name.value = mcpSelected;
  name.title = "服务器名即工具前缀 mcp__<名>__*；改名等于删除后重建";
  /* 改名 = 换 key。用 input 事件实时提交（而非 change）: change 只在失焦
   * 时触发, 用户改完名直接去点其他字段时, 后续编辑会写进旧 key（实测踩过:
   * 磁盘上是 new-server, UI 显示 time, 参数改了却不落盘）。实时换 key 后
   * renderMcpDetail 重建表单, 焦点会丢——所以仅在 key 真正变化时重建,
   * 且重建后把焦点还给名字框并移光标到末尾, 打字不中断。 */
  let lastName = name.value;
  name.addEventListener("input", () => {
    const nn = name.value.trim();
    if (nn === lastName) return;
    if (!nn) return;   // 清空过程中不动 key, 留给 blur 兜底恢复
    if (mcpCfg.mcpServers[nn] != null) {   // 撞名: 回退到上一个合法名
      name.value = lastName;
      toast("同名服务器已存在");
      return;
    }
    const next = {};
    for (const [k, v] of Object.entries(mcpCfg.mcpServers)) {
      next[k === lastName ? nn : k] = v;
    }
    mcpCfg.mcpServers = next;
    const oldKey = lastName;
    lastName = nn;
    mcpSelected = nn;
    renderMcpSettings();
    const nameAgain = $("mcp-detail").querySelector(".prov-name");
    if (nameAgain) {
      nameAgain.focus();
      const L = nameAgain.value.length;
      nameAgain.setSelectionRange(L, L);
    }
    scheduleMcpSave();
  });
  name.addEventListener("blur", () => {
    // 兜底: 失焦时名字为空/非法 → 恢复成上一个合法名
    if (!name.value.trim() && lastName) {
      name.value = lastName;
      return;
    }
    if (name.value.trim() !== lastName) {
      name.value = lastName;
      mcpStatus(`名称未变化（${lastName}）`, false);
    }
  });
  const badge = document.createElement("span");
  badge.className = "mcp-badge" + (st?.status === "connected" ? " ok"
    : st?.status === "failed" ? " bad" : "");
  badge.textContent = st?.status === "connected"
    ? `已连接 · ${st.tools.length} 工具`
    : st?.status === "failed" ? "连接失败" : "未连接";
  badge.title = st?.error || badge.textContent;
  const del = document.createElement("button");
  del.type = "button";
  del.className = "prov-del";
  del.innerHTML = TRASH_SMALL_SVG;
  del.dataset.tip = "删除服务器";
  del.onclick = async () => {
    if (!await confirmDialog(`删除 MCP 服务器「${mcpSelected}」？其工具将立即从会话中移除。`,
        { title: "删除服务器", okText: "删除", danger: true })) return;
    delete mcpCfg.mcpServers[mcpSelected];
    mcpSelected = null;
    renderMcpSettings();
    scheduleMcpSave();
  };
  head.append(name, badge, del);
  card.appendChild(head);

  /* 传输类型: stdio 本地子进程 / http streamable / sse */
  const grid = document.createElement("div");
  grid.className = "prov-grid";
  const typeField = document.createElement("div");
  typeField.className = "prov-field";
  typeField.innerHTML = "<label>传输类型</label>";
  const typeSel = document.createElement("select");
  typeSel.className = "set-input";
  for (const [val, label] of [
    ["stdio", "stdio（本地子进程）"],
    ["http", "HTTP（Streamable）"],
    ["sse", "SSE（已废弃，兼容旧服务器）"],
  ]) {
    const o = document.createElement("option");
    o.value = val; o.textContent = label;
    typeSel.appendChild(o);
  }
  typeSel.value = transport;
  typeSel.addEventListener("change", () => {
    spec.type = typeSel.value;
    renderMcpDetail();   // 字段集随类型切换（command/url 二选一）
    scheduleMcpSave();
  });
  typeField.appendChild(typeSel);
  grid.appendChild(typeField);

  const timeoutField = document.createElement("div");
  timeoutField.className = "prov-field";
  timeoutField.innerHTML = "<label>调用超时（秒）</label>";
  const timeout = document.createElement("input");
  timeout.type = "number"; timeout.min = "1"; timeout.className = "set-input";
  timeout.value = spec.timeout ?? 60;
  timeout.addEventListener("input", () => {
    const v = parseInt(timeout.value, 10);
    if (Number.isInteger(v) && v > 0) spec.timeout = v;
    scheduleMcpSave();
  });
  timeoutField.appendChild(timeout);
  grid.appendChild(timeoutField);
  card.appendChild(grid);

  if (transport === "stdio") {
    const cmdF = document.createElement("div");
    cmdF.className = "prov-field";
    cmdF.innerHTML = "<label>COMMAND</label>";
    const cmd = document.createElement("input");
    cmd.className = "set-input mono";
    cmd.placeholder = "npx / uvx / python";
    cmd.value = spec.command || "";
    cmd.addEventListener("input", () => { spec.command = cmd.value; scheduleMcpSave(); });
    cmdF.appendChild(cmd);
    card.appendChild(cmdF);

    const argsF = document.createElement("div");
    argsF.className = "prov-field";
    argsF.innerHTML = '<label>参数（JSON 数组）</label>';
    const args = document.createElement("input");
    args.className = "set-input mono";
    args.placeholder = '["-y", "@modelcontextprotocol/server-everything"]';
    args.value = JSON.stringify(spec.args ?? []);
    args.addEventListener("input", () => {
      try {
        const v = JSON.parse(args.value || "[]");
        if (Array.isArray(v)) { spec.args = v; args.classList.remove("invalid"); scheduleMcpSave(); }
        else args.classList.add("invalid");
      } catch { args.classList.add("invalid"); }
    });
    argsF.appendChild(args);
    card.appendChild(argsF);

    const envF = document.createElement("div");
    envF.className = "prov-field";
    envF.innerHTML = "<label>环境变量（JSON 对象）</label>";
    const env = document.createElement("input");
    env.className = "set-input mono";
    env.placeholder = '{"API_KEY": "${MY_KEY}"}';
    env.value = JSON.stringify(spec.env ?? {});
    env.addEventListener("input", () => {
      try {
        const v = JSON.parse(env.value || "{}");
        if (v && typeof v === "object" && !Array.isArray(v)) {
          spec.env = v; env.classList.remove("invalid"); scheduleMcpSave();
        } else env.classList.add("invalid");
      } catch { env.classList.add("invalid"); }
    });
    envF.appendChild(env);
    card.appendChild(envF);
  } else {
    const urlF = document.createElement("div");
    urlF.className = "prov-field";
    urlF.innerHTML = "<label>URL</label>";
    const url = document.createElement("input");
    url.className = "set-input mono";
    url.placeholder = "http://localhost:3000/mcp";
    url.value = spec.url || "";
    url.addEventListener("input", () => { spec.url = url.value; scheduleMcpSave(); });
    urlF.appendChild(url);
    card.appendChild(urlF);

    const hdF = document.createElement("div");
    hdF.className = "prov-field";
    hdF.innerHTML = "<label>请求头（JSON 对象）</label>";
    const hd = document.createElement("input");
    hd.className = "set-input mono";
    hd.placeholder = '{"Authorization": "Bearer ${TOKEN}"}';
    hd.value = JSON.stringify(spec.headers ?? {});
    hd.addEventListener("input", () => {
      try {
        const v = JSON.parse(hd.value || "{}");
        if (v && typeof v === "object" && !Array.isArray(v)) {
          spec.headers = v; hd.classList.remove("invalid"); scheduleMcpSave();
        } else hd.classList.add("invalid");
      } catch { hd.classList.add("invalid"); }
    });
    hdF.appendChild(hd);
    card.appendChild(hdF);
  }

  /* 连接失败原因就地显示（列表圆点 title 里也有, 这里给完整信息） */
  if (st?.status === "failed" && st.error) {
    const errLine = document.createElement("div");
    errLine.className = "mcp-error-line";
    errLine.textContent = st.error;
    card.appendChild(errLine);
  }
  wrap.appendChild(card);
}
function renderMcpSettings() {
  const list = $("mcp-list");
  if (!list) return;
  const names = Object.keys(mcpCfg.mcpServers);
  if (mcpSelected == null || !names.includes(mcpSelected)) mcpSelected = names[0] ?? null;
  list.innerHTML = "";
  for (const n of names) list.appendChild(buildMcpItem(n));
  renderMcpDetail();
}
$("btn-add-mcp").onclick = () => {
  const key = mcpKeyFor("new-server");
  mcpCfg.mcpServers[key] = { type: "stdio", command: "" };
  mcpSelected = key;
  renderMcpSettings();
  const name = $("mcp-detail").querySelector(".prov-name");
  if (name) { name.focus(); name.select(); }
  scheduleMcpSave();   // 新增即保存; 缺 command 时红字提示, 填好自动补存
};
$("btn-mcp-reload").onclick = async () => {
  // 有未保存编辑先强制落盘再重连——静默丢弃会让用户以为改好了,
  // 重连却跑在旧配置上（实测踩过: 改完参数点重连, 改动全丢）
  if (mcpDirty) await persistMcpServers();
  if (mcpSaveTimer) {   // 落盘被校验拦下(红字提示)时不再继续, 保留现场
    toast("先解决未保存的配置, 再重连");
    return;
  }
  mcpStatus("重连中…");
  try {
    const r = await fetch("/api/mcp/reload", { method: "POST" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
    mcpStatuses = d.servers || [];
    mcpStatus(d.servers.every(s => s.status !== "connected")
      ? "已重连（无已连接服务器）" : "已重连");
    renderMcpSettings();
  } catch (e) {
    mcpStatus("重连失败: " + e.message, true);
    toast("重连失败: " + e.message);
  }
};

/* ============================================================
 * 设置页: 命令白名单（GET/POST/DELETE /api/settings/allowlist）
 * ============================================================ */
async function renderAllowlistSettings() {
  await renderRuleListSettings({
    listEl: "al-rule-list", url: "/api/settings/allowlist",
    key: "rules", emptyText: "还没有规则。审批弹窗里的“总是允许”会自动把命令前缀加进来。",
  });
}
async function renderDenylistSettings() {
  await renderRuleListSettings({
    listEl: "dl-rule-list", url: "/api/settings/denylist",
    key: "rules", emptyText: "还没有拒绝规则。白名单放行 \"git push\" 的同时, 可以在这里加 \"git push --force\" 拦住强推。",
  });
}
async function renderSensitivePathsSettings() {
  await renderRuleListSettings({
    listEl: "sp-path-list", url: "/api/settings/sensitive-paths",
    key: "paths", emptyText: "还没有自定义敏感路径。加项目里的 secrets/、.env.production 等, 写入时任何模式都强制确认。",
  });
}

/* 通用清单管理器: 白名单/拒绝清单/敏感路径三个分区共用同一交互
 * （GET 拉取渲染, DELETE 按原文移除）。 */
async function renderRuleListSettings({ listEl, url, key, emptyText }) {
  const list = $(listEl);
  if (!list) return;
  let items = [];
  try {
    const r = await fetch(url);
    if (r.ok) items = (await r.json())[key] || [];
  } catch (e) { /* 服务不可达: 列表留空 */ }
  list.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "al-empty";
    empty.textContent = emptyText;
    list.appendChild(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "al-rule-item";
    const code = document.createElement("code");
    code.textContent = item;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "icon-act al-del";
    del.textContent = "删除";
    del.onclick = async () => {
      del.disabled = true;
      try {
        const r = await fetch(url, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [key === "paths" ? "path" : "rule"]: item }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        renderRuleListSettings({ listEl, url, key, emptyText });
      } catch (e) {
        del.disabled = false;
        toast("删除失败: " + e.message);
      }
    };
    row.appendChild(code);
    row.appendChild(del);
    list.appendChild(row);
  }
}

/* 添加框的通用行为: POST 一条新规则/路径后重渲染本分区。 */
function bindRuleListInput(inputId, btnId, url, bodyKey, listSpec, doneMsg) {
  const submit = async () => {
    const inp = $(inputId);
    const value = (inp.value || "").trim();
    if (!value) return;
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [bodyKey]: value }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      inp.value = "";
      renderRuleListSettings(listSpec);
      toast(doneMsg);
    } catch (e) {
      toast("添加失败: " + e.message);
    }
  };
  $(btnId).onclick = submit;
  $(inputId).addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });
}
bindRuleListInput("al-rule-input", "btn-al-add", "/api/settings/allowlist",
  "rule", { listEl: "al-rule-list", url: "/api/settings/allowlist", key: "rules" },
  "白名单已更新");
bindRuleListInput("dl-rule-input", "btn-dl-add", "/api/settings/denylist",
  "rule", { listEl: "dl-rule-list", url: "/api/settings/denylist", key: "rules" },
  "拒绝清单已更新");
bindRuleListInput("sp-path-input", "btn-sp-add", "/api/settings/sensitive-paths",
  "path", { listEl: "sp-path-list", url: "/api/settings/sensitive-paths", key: "paths" },
  "敏感路径已更新");

/* ============================================================
 * 设置页: Skills（GET /api/skills, POST /api/skills/install,
 * DELETE /api/skills/{name}）—— 安装/卸载后端即时热生效
 * ============================================================ */
function skillStatus(msg, isErr) {
  const el = $("skill-install-status");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("err", Boolean(isErr));
}

async function loadSkills() {
  const list = $("skill-list");
  if (!list) return [];
  let skills = [];
  try {
    const r = await fetch("/api/skills");
    if (r.ok) skills = (await r.json()).skills || [];
  } catch (e) { /* 服务不可达: 列表留空 */ }
  list.innerHTML = "";
  if (!skills.length) {
    const empty = document.createElement("div");
    empty.className = "al-empty";
    empty.textContent = "还没有安装技能。上方输入 GitHub 仓库即可安装社区 skills。";
    list.appendChild(empty);
    return skills;
  }
  for (const s of skills) {
    const item = document.createElement("div");
    item.className = "skill-item";
    const head = document.createElement("div");
    head.className = "skill-item-head";
    const name = document.createElement("span");
    name.className = "skill-name";
    name.textContent = s.name;
    const badge = document.createElement("span");
    badge.className = `skill-src skill-src-${s.source === "project" ? "project" : "user"}`;
    badge.textContent = s.source === "project" ? "项目级" : "用户级";
    head.appendChild(name);
    head.appendChild(badge);
    if (s.description) {
      const desc = document.createElement("div");
      desc.className = "skill-desc";
      desc.textContent = s.description;
      head.appendChild(desc);
    }
    const dir = document.createElement("div");
    dir.className = "skill-dir";
    dir.textContent = s.dir;
    item.appendChild(head);
    item.appendChild(dir);
    if (s.source !== "project") {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "icon-act al-del skill-del";
      del.textContent = "卸载";
      del.onclick = async () => {
        del.disabled = true;
        try {
          const r = await fetch(`/api/skills/${encodeURIComponent(s.name)}`,
                               { method: "DELETE" });
          if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
          toast("已卸载 " + s.name);
          loadSkills();
        } catch (e) {
          del.disabled = false;
          toast("卸载失败: " + e.message);
        }
      };
      item.appendChild(del);
    }
    list.appendChild(item);
  }
  return skills;
}

async function installSkillFromInput() {
  const inp = $("skill-repo-input");
  const sub = $("skill-subpath-input");
  const chk = $("skill-overwrite-chk");
  const btn = $("btn-skill-install");
  const repo = (inp.value || "").trim();
  if (!repo) { skillStatus("请填写仓库地址", true); return; }
  btn.disabled = true;
  skillStatus("正在克隆并解析技能…");
  try {
    const r = await fetch("/api/skills/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repo,
        subpath: (sub.value || "").trim(),
        overwrite: chk ? chk.checked : false,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    const names = (data.installed || []).map(s => s.name).join(", ");
    skillStatus(`已安装: ${names}`);
    inp.value = ""; sub.value = "";
    loadSkills();
    toast("技能已安装并生效");
  } catch (e) {
    skillStatus("安装失败: " + e.message, true);
  } finally {
    btn.disabled = false;
  }
}
$("btn-skill-install").onclick = installSkillFromInput;
$("skill-repo-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") installSkillFromInput();
});

function openSettings() {
  themeDd.setValue(themePref());   // 每次打开回显当前值
  $("fs-ui-input").value = String(fsUiPref());
  $("fs-code-input").value = String(fsChatPref());
  $("bg-bright").value = String(bgBrightPref());
  $("bg-bright-val").textContent = String(bgBrightPref());
  syncAccentInput();  // 迭代次数: 未加载过(服务端值未知)时留空给 placeholder 兜底, 已知则回显
  if (state.serverMaxIter != null) $("set-max-iter").value = String(state.serverMaxIter);
  loadProviders().then(renderProviderSettings);   // 拉取供应商配置并渲染
  loadMcpServers().then(renderMcpSettings);       // 拉取 MCP 配置与连接状态并渲染
  renderAllowlistSettings();                      // 拉取命令白名单并渲染
  renderDenylistSettings();                       // 拒绝清单
  renderSensitivePathsSettings();                 // 用户敏感路径
  loadSkills();                                   // 拉取已装技能并渲染
  skillStatus("");                                // 清掉上次的安装状态
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
/* 初始化页: 协议切换 → Base URL 默认值联动（智谱 Coding Plan 三端点中
 * x-code 用得到两个; /api/v1 是 OpenAI Response 协议, 供 Codex, 不适用）。
 * 只改"用户还没手动输入过"的值, 避免覆盖用户粘贴的地址。 */
const OB_BASE_DEFAULTS = {
  anthropic: "https://open.bigmodel.cn/api/anthropic",
  openai: "https://open.bigmodel.cn/api/coding/paas/v4",
};
let _ob_base_touched = false;
$("ob-proto").addEventListener("change", () => {
  const baseInput = $("ob-base");
  if (!_ob_base_touched) baseInput.value = OB_BASE_DEFAULTS[$("ob-proto").value];
});
$("ob-base").addEventListener("input", () => { _ob_base_touched = true; });

async function saveOnboarding() {
  const key = $("ob-key").value.trim();
  const base = $("ob-base").value.trim();
  const model = $("ob-model").value.trim();
  const proto = $("ob-proto").value;
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
      prov.protocol = proto;   // 协议变化时覆盖旧值（init 页是显式选择）
      prov.models = prov.models || [];
      if (!prov.models.some(m => m.id === model)) prov.models.push({ id: model, name: model, tags: [] });
    } else {
      providers.push({
        id: "default", name: "默认供应商",
        base_url: base, api_key: key, enabled: true,
        protocol: proto,
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
  initUpdateCheck();   // 桌面壳: 静默检查更新（浏览器/源码运行无桥, 内部直接跳过）
})();
(() => {
/* ===== 悬浮循环滚动(跑马灯): 侧栏被截断的项目名 / 任务标题, 悬浮时循环滚动展示全文 ===== */
  const HS_SEL = ".session-item .title, .project-item .p-name";
  const HS_SPEED = 40;    // 滚动速度 px/s
  const HS_GAP = 56;      // 首尾相接处的间距 px
  const HS_DELAY = 300;   // 悬停多久后开始滚动 ms
  let hsCur = null;       // { el, html, timer }

  if (!document.getElementById("hs-marquee-style")) {   // 一次性注入样式
    const st = document.createElement("style");
    st.id = "hs-marquee-style";
    st.textContent =
      ".hs-on{text-overflow:clip}" +
      ".hs-track{display:inline-flex;white-space:nowrap;will-change:transform;animation:hs-marquee 10s linear infinite}" +
      ".hs-track>span{flex:none;padding-right:" + HS_GAP + "px}" +
      "@keyframes hs-marquee{from{transform:translateX(0)}to{transform:translateX(-50%)}}";
    document.head.appendChild(st);
  }

  const hsStop = () => {
    if (!hsCur) return;
    clearTimeout(hsCur.timer);
    const el = hsCur.el, html = hsCur.html;
    hsCur = null;
    if (!el.isConnected) return;   // 列表已重渲染, 元素已丢弃, 无需还原
    el.classList.remove("hs-on");
    el.innerHTML = html;           // 移除轨道, 还原原始内容
  };

  document.addEventListener("pointerover", e => {
    const el = e.target.closest && e.target.closest(HS_SEL);
    if (hsCur && el === hsCur.el) return;
    hsStop();
    if (!el) return;
    hsCur = { el, html: el.innerHTML, timer: setTimeout(() => {
      const dist = el.scrollWidth - el.clientWidth;
      if (!el.isConnected || dist < 3) return;   // 未截断(或已被重渲染移除)则不滚动
      const dur = Math.max(3, Math.round((el.scrollWidth + HS_GAP) / HS_SPEED));   // 一圈的秒数
      el.classList.add("hs-on");
      el.innerHTML =
        '<span class="hs-track" style="animation-duration:' + dur + 's">' +
        "<span>" + hsCur.html + "</span><span>" + hsCur.html + "</span></span>";
    }, HS_DELAY) };
  });

  document.addEventListener("pointerout", e => {
    if (hsCur && !hsCur.el.contains(e.relatedTarget)) hsStop();   // 离开该标题才停
  });
  window.addEventListener("blur", () => hsStop());
})();
