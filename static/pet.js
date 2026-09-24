/* ============================================================
 * x-code 桌宠(兼容 Codex 宠物格式)
 * 一个文件、两种形态, 页面自识别:
 *   - 主界面(index.html): #btn-pet 召唤按钮 + 设置→外观 的宠物选择器,
 *     并把 WS 事件经 BroadcastChannel 转发给悬浮窗
 *   - 悬浮窗(pet.html): 订阅主窗双通道转发的事件(BroadcastChannel +
 *     localStorage storage 事件, 按 __n 去重)驱动状态机
 * 精灵图渲染与 Codex 同法: 8 列固定、CSS background-position 逐帧位移,
 * 行/帧时长是 Codex 官方契约(见 ROWS_SPEC), pet.json 不携带时序。
 * ============================================================ */
(function () {
  if (window.__xcodePet) return;   // 防重复初始化
  window.__xcodePet = true;

  const $ = (id) => document.getElementById(id);
  const PKEY = "xc-pet";
  const BUBBLE_MAX = 26;

  // Codex 官方逐帧行为: 行号、每帧毫秒; once = 播完回落到 fallback
  // (与 scripts/gen_default_pet.py 的 SPEC 同一张表)
  const ROWS_SPEC = {
    idle:            { row: 0, ms: [280, 110, 110, 140, 140, 320] },
    "running-right": { row: 1, ms: [120, 120, 120, 120, 120, 120, 120, 220] },
    "running-left":  { row: 2, ms: [120, 120, 120, 120, 120, 120, 120, 220] },
    waving:          { row: 3, ms: [140, 140, 140, 280], once: true },
    jumping:         { row: 4, ms: [140, 140, 140, 140, 280], once: true },
    failed:          { row: 5, ms: [140, 140, 140, 140, 140, 140, 140, 240] },
    waiting:         { row: 6, ms: [150, 150, 150, 150, 150, 260] },
    running:         { row: 7, ms: [120, 120, 120, 120, 120, 220] },
    review:          { row: 8, ms: [150, 150, 150, 150, 150, 280] },
  };
  const CELL_W = 192, CELL_H = 208, SHEET_W = 1536;

  const STATUS_TEXT = {
    idle: "摸鱼中", running: "打工中", "running-left": "打工中",
    "running-right": "打工中", waiting: "等你批条子", failed: "出事了…",
    jumping: "收工！", waving: "你好呀", review: "瞅一眼",
  };

  // 注: 不跟随系统的"减弱动效"(prefers-reduced-motion)定格——桌宠是
  // 玩赏性的小动画, 用户明确要求拖动/状态都要动起来

  function pref() {
    try { return JSON.parse(localStorage.getItem(PKEY)) || {}; }
    catch { return {}; }
  }
  function savePref(p) {
    try { localStorage.setItem(PKEY, JSON.stringify(p)); } catch { }
  }
  const toast = window.xcodeToast || (msg => console.log("[pet]", msg));

  // ---------- 精灵图渲染器 ----------
  class PetSprite {
    constructor(el, url, rows) {
      this.el = el;
      this.rows = rows || 9;
      this.timer = 0;
      this.st = null;
      const img = new Image();
      img.onload = () => {
        this.el.style.backgroundImage = `url("${url}")`;
        this.el.style.backgroundSize = `${SHEET_W}px ${this.rows * CELL_H}px`;
        if (this.st) this.play(this.st.name, this.st);   // 加载前收到的状态补播
      };
      img.src = url;
    }

    play(name, opts = {}) {
      const spec = ROWS_SPEC[name] || ROWS_SPEC.idle;
      clearTimeout(this.timer);
      this.st = {
        name, spec, i: 0,
        once: spec.once || !!opts.once,
        fallback: opts.fallback || null,
      };
      this.frame();
    }

    frame() {
      const st = this.st;
      if (!st) return;
      this.show(st.name, st.i);
      this.timer = setTimeout(() => {
        if (this.st !== st) return;
        if (st.i + 1 < st.spec.ms.length) {
          st.i += 1;
          this.frame();
        } else if (st.once) {
          this.st = null;   // 先清再换, 防 fallback===name 时死循环
          this.play(st.fallback && st.fallback !== st.name ? st.fallback : "idle");
        } else {
          st.i = 0;
          this.frame();
        }
      }, st.spec.ms[st.i]);
    }

    show(name, i) {
      const spec = ROWS_SPEC[name] || ROWS_SPEC.idle;
      const col = Math.min(i, spec.ms.length - 1);
      this.el.style.backgroundPosition = `-${col * CELL_W}px -${spec.row * CELL_H}px`;
    }
  }

  // ---------- 悬浮窗形态(pet.html) ----------
  function initFloat() {
    const spriteEl = $("pet-sprite");
    const bubbleEl = $("pet-bubble");
    const statusEl = $("pet-status");
    let sprite = null;
    const pet = {
      base: "idle",          // 循环态
      overlay: null,         // 一次性态(jumping/waving/review), 播完回 base
      alt: 0,                // 工具调用交替换跑动方向
      failTimer: 0, bubbleTimer: 0, lastToolBubble: 0,
    };

    const setStatus = (name) => { statusEl.textContent = STATUS_TEXT[name] || STATUS_TEXT.idle; };

    function setBase(name) {
      pet.base = name;
      setStatus(name);
      if (!pet.overlay && sprite) sprite.play(name);
    }

    function overlayOnce(name) {
      pet.overlay = name;
      setStatus(name);
      if (sprite) {
        sprite.play(name, {
          fallback: pet.base,
        });
      }
      // once 播完 PetSprite 自己回落 base, 这里只负责收回 overlay 标记与状态行
      setTimeout(() => {
        if (pet.overlay === name) {
          pet.overlay = null;
          setStatus(pet.base);
        }
      }, (ROWS_SPEC[name].ms.reduce((a, b) => a + b, 0)) + 60);
    }

    function bubble(text, ms = 2400) {
      if (!text) return;
      const t = String(text);
      bubbleEl.textContent = t.length > BUBBLE_MAX ? t.slice(0, BUBBLE_MAX - 1) + "…" : t;
      bubbleEl.classList.remove("bubble-dim");
      bubbleEl.hidden = false;
      clearTimeout(pet.bubbleTimer);
      if (ms > 0) pet.bubbleTimer = setTimeout(() => (bubbleEl.hidden = true), ms);
    }

    function fail(text) {
      clearTimeout(pet.failTimer);
      setBase("failed");
      bubble(text || "出事了…");
      pet.failTimer = setTimeout(() => {
        if (pet.base === "failed") setBase("idle");
      }, 3500);
    }

    function onEvent(msg) {
      if (!msg || !msg.type) return;
      switch (msg.type) {
        case "turn_started":
          clearTimeout(pet.failTimer);
          setBase("running");
          bubble("接单！");
          break;
        case "tool_use_started": {
          // 每次工具换一个跑动方向, 桌面上看得到"在忙"
          pet.alt = (pet.alt + 1) % 3;
          const dir = pet.alt === 0 ? "running" : pet.alt === 1 ? "running-right" : "running-left";
          setBase(dir);
          const now = Date.now();
          if (now - pet.lastToolBubble > 900) {
            pet.lastToolBubble = now;
            bubble((msg.tool_name || "工具") + "…");
          }
          break;
        }
        case "tool_result":
          if (/^running/.test(pet.base)) overlayOnce("review");   // 瞄一眼结果
          break;
        case "permission_request":
          setBase("waiting");
          bubble("等你批条子" + (msg.tool_name ? `: ${msg.tool_name}` : ""), 0);
          break;
        case "await_output":
          setBase("waiting");
          bubble("等你补充…", 0);
          break;
        case "turn_done":
          clearTimeout(pet.failTimer);
          if (msg.interrupted) fail("停了…");
          else {
            pet.base = "idle";
            overlayOnce("jumping");
            bubble("收工！");
          }
          break;
        case "error":
          fail(msg.message ? String(msg.message) : "出事了…");
          break;
        case "rate_limited_retry":
          bubble(`限流了, ${msg.delay_s}s 后重试…`);
          break;
        case "turn_interrupting":
          bubble("等等, 我停——");
          break;
      }
    }

    // ---- 事件来源: 主窗双通道转发, BroadcastChannel 与 localStorage 的
    //      storage 事件互为备份(跨 WebView2 窗口至少一条通), 按 __n 去重。
    //      不再直连 /ws——后端只把事件推给发起该轮的那条连接, 多开的
    //      第二条连接什么也收不到(实测宠物因此永远"摸鱼中")。 ----
    let lastSeq = -1;
    const dispatch = (msg) => {
      if (!msg || typeof msg !== "object") return;
      if (typeof msg.__n === "number") {
        if (msg.__n <= lastSeq) return;   // 双通道重复投递
        lastSeq = msg.__n;
      }
      onEvent(msg);
    };
    try {
      const ch = new BroadcastChannel("xcode-pet");
      ch.onmessage = (e) => dispatch(e.data);
    } catch { }
    window.addEventListener("storage", (e) => {
      if (e.key !== "xc-pet-evt" || !e.newValue) return;
      try { dispatch(JSON.parse(e.newValue)); } catch { }
    });

    // ---- 交互: 按住拖动(宠物随鼠标跑动) / 点击打招呼 ----
    const bridge = () => window.xcodeDesktopPet;
    const floatEl = $("pet-float");
    let drag = null;      // {sx,sy,wx,wy,dir,moved,dustT,hadBubble}
    let wasDrag = false;  // 松手后短暂置真, 让 click 区分"拖完"与"点击"

    // 拖动扬尘: 脚边冒几粒灰, 600ms 内飘散
    function puff(n) {
      for (let i = 0; i < (n || 1); i++) {
        const s = document.createElement("span");
        s.className = "dust-puff";
        s.style.left = `calc(50% + ${(Math.random() * 56 - 28).toFixed(0)}px)`;
        s.style.bottom = `${(24 + Math.random() * 14).toFixed(0)}px`;
        s.style.setProperty("--dx", `${(Math.random() * 44 - 22).toFixed(0)}px`);
        floatEl.appendChild(s);
        setTimeout(() => s.remove(), 650);
      }
    }
    const dragTilt = (deg) => {
      spriteEl.style.transform = deg ? `rotate(${deg.toFixed(1)}deg)` : "";
    };
    // 拖动期间姿态自检: 期望始终是朝运动方向的跑动; 若被别处重置,
    // 下一个 move 事件会把跑动补播回来(播放器 play 幂等, 同名不重启)
    function dragPose() {
      if (!sprite) return;
      const want = drag.dir >= 0 ? "running-right" : "running-left";
      if (!sprite.st || sprite.st.name !== want) sprite.play(want);
    }

    spriteEl.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || !bridge()?.movePet) return;
      pet.overlay = null;                 // 拖动姿态优先于一次性动作
      drag = {
        sx: e.screenX, sy: e.screenY,
        wx: window.screenX, wy: window.screenY,
        dir: 1, moved: false, dustT: 0,
        hadBubble: !bubbleEl.hidden,
      };
      try { spriteEl.setPointerCapture(e.pointerId); } catch { }
      e.preventDefault();
    });
    window.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.screenX - drag.sx, dy = e.screenY - drag.sy;
      if (!drag.moved) {
        if (Math.hypot(dx, dy) < 5) return;   // 抖动阈值: 按住不动不算拖
        drag.moved = true;
        bubbleEl.hidden = true;               // 拖动时气泡让路
      }
      bridge().movePet(drag.wx + dx, drag.wy + dy).catch(() => { });
      // 方向跟随累计位移(比逐事件速度稳, 不受抖动影响)
      const dir = dx >= 0 ? 1 : -1;
      if (dir !== drag.dir) {
        drag.dir = dir;
        dragTilt(6 * dir);                    // 身体朝运动方向倾
      }
      dragPose();
      const now = performance.now();
      if (now - drag.dustT > 130) {
        drag.dustT = now;
        puff(1);
      }
    });
    function endDrag() {
      if (!drag) return;
      const d = drag;
      drag = null;
      dragTilt("");
      if (!d.moved) return;
      wasDrag = true;
      setTimeout(() => (wasDrag = false), 350);
      puff(3);
      // 落地回弹: 压扁→弹起→复原(CSS 动画盖掉内联 transform)
      spriteEl.classList.remove("pet-land");
      void spriteEl.offsetWidth;
      spriteEl.classList.add("pet-land");
      if (d.hadBubble && bubbleEl.textContent) bubbleEl.hidden = false;
      if (sprite) sprite.play(pet.base);
      setStatus(pet.base);
    }
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", endDrag);
    spriteEl.addEventListener("click", () => {
      if (!wasDrag && pet.base === "idle") overlayOnce("waving");
    });

    // ---- 启动: 拉宠物列表 → 渲染 → 打招呼 ----
    (async () => {
      try {
        const res = await fetch("/api/pets");
        const data = await res.json();
        const pets = data.pets || [];
        const chosen = pets.find(p => p.id === pref().petId) || pets[0];
        if (!chosen) {
          statusEl.textContent = "没有宠物";
          bubble("把宠物文件夹放进 pets 目录(设置里可查路径)", 0);
          return;
        }
        sprite = new PetSprite(spriteEl, `/api/pets/${encodeURIComponent(chosen.id)}/sheet`, chosen.rows);
        sprite.play("idle");
        overlayOnce("waving");
        bubble("你好呀");
      } catch {
        statusEl.textContent = "加载失败";
        bubble("桌宠加载失败, 稍后重开悬浮窗");
      }
    })();
  }

  // ---------- 主界面形态(index.html) ----------
  function initMain() {
    // 把 WS 状态事件转发给桌宠悬浮窗(app.js handleServerMessage 首行调用)。
    // 双通道: BroadcastChannel 在跨 WebView2 窗口时实测收不到;
    // localStorage 的 storage 事件是同源多窗的可靠第二条路。两条都写,
    // 悬浮窗按序号去重。只转状态事件——text_delta 这类流式令牌不转发。
    let ch = null;
    try { ch = new BroadcastChannel("xcode-pet"); } catch { }
    const PET_EVT = new Set(["turn_started", "tool_use_started", "tool_result",
      "permission_request", "await_output", "turn_done", "error",
      "rate_limited_retry", "turn_interrupting"]);
    let seq = 0;
    window.xcodePet = {
      onEvent(msg) {
        if (!msg || !PET_EVT.has(msg.type)) return;
        try { ch && ch.postMessage(msg); } catch { }
        try {
          localStorage.setItem("xc-pet-evt", JSON.stringify({ ...msg, __n: ++seq }));
        } catch { }
      },
    };

    // 召唤入口: 底栏按钮 + 设置页按钮。悬浮窗是 Tauri 命令开的,
    // 其他环境(Electron/浏览器)没有桥——底栏按钮藏掉, 设置按钮置灰提示
    const petFloat = () => window.xcodeDesktopPet.petFloat().catch(e => console.error("[pet]", e));
    const btn = $("btn-pet");
    if (btn) {
      if (!window.xcodeDesktopPet) btn.hidden = true;
      else {
        btn.hidden = false;
        btn.addEventListener("click", petFloat);
      }
    }
    const summon = $("btn-pet-summon");
    if (summon) {
      if (!window.xcodeDesktopPet) {
        summon.disabled = true;
        summon.title = "悬浮窗仅桌面版(Tauri)支持";
      } else {
        summon.addEventListener("click", petFloat);
      }
    }
    // 打开宠物目录: 资源管理器里直接投放宠物文件夹
    const dirBtn = $("btn-pet-dir");
    if (dirBtn) {
      dirBtn.addEventListener("click", async () => {
        try {
          const r = await fetch("/api/pets/open-dir", { method: "POST" });
          if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status);
        } catch (e) { toast("打开失败: " + e.message); }
      });
    }

    renderPicker();
  }

  async function renderPicker() {
    const wrap = $("pet-picker");
    if (!wrap) return;
    let data = null;
    try { data = await (await fetch("/api/pets")).json(); } catch { }
    const pets = (data && data.pets) || [];
    const dirEl = $("pet-dir");
    if (dirEl) dirEl.textContent = data && data.petsDir
      ? `宠物目录: ${data.petsDir}` : "";

    wrap.innerHTML = "";
    if (!pets.length) {
      const empty = document.createElement("div");
      empty.className = "pet-list-empty";
      empty.textContent = "还没有宠物: 点「打开目录」, 把 Codex 格式宠物文件夹(pet.json + spritesheet 图)放进去即可识别";
      wrap.appendChild(empty);
      return;
    }
    const cur = pref().petId;
    pets.forEach((p, idx) => {
      const on = p.id === cur || (!cur && idx === 0);
      const row = document.createElement("div");
      row.className = "pet-item" + (on ? " on" : "");
      row.innerHTML =
        '<div class="pet-item-thumb"><div class="pet-sprite-mini"></div></div>' +
        '<div class="pet-item-info"><div class="pet-item-name"></div><div class="pet-item-desc"></div></div>' +
        '<span class="pet-item-badge"></span>' +
        '<span class="pet-item-check"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 12.5l5 5 10-11"/></svg></span>';
      row.querySelector(".pet-item-name").textContent = p.displayName || p.id;
      row.querySelector(".pet-item-desc").textContent =
        p.description || `${p.rows} 行标准动作图集`;
      row.querySelector(".pet-item-badge").textContent = p.source === "codex" ? "Codex" : "本地";
      const mini = row.querySelector(".pet-sprite-mini");
      mini.style.backgroundImage = `url("/api/pets/${encodeURIComponent(p.id)}/sheet")`;
      mini.style.backgroundSize = `${SHEET_W}px ${p.rows * CELL_H}px`;
      mini.style.backgroundPosition = "0 0";   // idle 首帧做封面
      row.addEventListener("click", () => {
        savePref({ ...pref(), petId: p.id });
        wrap.querySelectorAll(".pet-item").forEach(c => c.classList.remove("on"));
        row.classList.add("on");
        toast("已切换桌宠, 重新召唤悬浮窗后生效");
      });
      wrap.appendChild(row);
    });
  }

  if (document.getElementById("pet-float")) initFloat();
  else initMain();
})();
