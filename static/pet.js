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
  const BUBBLE_MAX = 80;   // 权限气泡要装下命令摘要; CSS 限宽自动折行

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

  /* 内置台词库: 事件台词的随机池, 也是 AI 失败/未开时的回退语料。
   * 前五组跟事件走(接单/收工/批准/干活点评/摸鱼闲聊), 后七组是事件驱动
   * 台词(见 initFloat evalEvents): toolstreak 同工具连用 / errstreak 连续
   * 出错 / longrun 单轮太久 / waitlong 审批等太久 / idlelong 闲太久 /
   * latenight 深夜 / friday 周五下午。
   * 宠物 pet.json 的 persona.lines 同名分组整体覆盖(见 initFloat applyPersona)
   * ——人设注入改 pet.json 即可, 换宠物=换性格。 */
  const PET_LINES = {
    ack: ["接单！", "来活儿了！", "包在我身上", "接单, 冲！"],
    done: ["收工！", "收工！这班没白上", "搞定, 撒花", "活儿干完了, 溜了"],
    approve: ["好, 批了！", "收到, 马上开干", "准了, 看我的"],
    quip: ["这 bug 我闻到了", "稳住, 快收工了", "键盘敲得飞起啊",
      "这波操作有点东西", "莫慌, 稳得很", "又在憋大招?"],
    idle: ["摸鱼中, 别催", "喝口水, 歇会儿", "有活儿随时叫我", "zzz…"],
    toolstreak: ["又来? 这都第六遍了", "同一个工具按到包浆了", "熟能生巧, 但也别太熟"],
    errstreak: ["翻车二连… 问题不大", "又炸了, 深呼吸", "这代码跟咱有仇吧"],
    longrun: ["这轮跑挺久啊, 硬活儿", "还在磨, 我陪你", "大工程啊这是"],
    waitlong: ["条子呢~ 冻结中", "批一下呗, 等急了", "嘘… 在等你点头"],
    idlelong: ["闲了半天了哦", "摸鱼摸得挺扎实", "有活儿随时叫我"],
    latenight: ["这个点了, 头发保重", "深夜卷王就是你", "早点睡, 明天再战"],
    friday: ["周五下午了, 收着点干", "熬过这几分钟就是周末", "周末不加班, 答应我"],
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
  // 宠物大小缩放区间: 主窗滑杆/悬浮窗 applyScale/召唤建窗共用一份口径
  const SCALE_RANGE = [0.5, 2];
  const clampScale = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return 1;
    return Math.min(SCALE_RANGE[1], Math.max(SCALE_RANGE[0], n));
  };
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
      // 图集加载失败不能静默: 旧表现是白屏一只不剩, 用户以为应用坏了。
      // 常见于刚刷新进目录的新宠物被 HTTP 缓存 404 ——缓存层已有修复,
      // 这里兜底给出可见提示。
      img.onerror = () => {
        this.el.style.background = "none";
        this.el.textContent = ":( 图集加载失败";
        this.el.style.cssText +=
          ";display:flex;align-items:center;justify-content:center;" +
          "font-size:11px;color:#f66;text-align:center;white-space:normal;";
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

    // ---- 大小缩放: pref().scale ∈ [0.5, 2]（主窗设置页滑杆写入）。
    //      CSS 变量驱动 #pet-text/#pet-sprite 的 zoom, 窗口尺寸经
    //      resizePet 同步放大——两处都幂等, 重复应用无副作用。
    //      clampScale 在共用作用域（initMain 召唤建窗也要用同一口径）。
    // 窗口创建时就按保存的缩放开好了尺寸(main.rs inner_size), 启动这轮
    // applyScale 只同步 CSS 变量、不再 set_size——对透明悬浮窗做无谓的
    // 等值 resize 会触发 WebView2 重排, 有把透明底打回白色"框"的风险
    let appliedScale = clampScale(pref().scale);
    function applyScale() {
      const s = clampScale(pref().scale);
      $("pet-float").style.setProperty("--pet-scale", String(s));
      if (s === appliedScale) return s;
      appliedScale = s;
      // 窗口还没就绪/无桥(浏览器直开 pet.html)时静默跳过; resizePet 幂等
      try { bridge()?.resizePet?.(s); } catch { }
      return s;
    }
    // 主窗调滑杆 → pref 落 localStorage → 本窗 storage 事件实时跟随
    window.addEventListener("storage", (e) => {
      if (e.key !== PKEY || !e.newValue) return;
      applyScale();
    });
    applyScale();

    const pet = {
      base: "idle",          // 循环态
      overlay: null,         // 一次性态(jumping/waving/review), 播完回 base
      alt: 0,                // 工具调用交替换跑动方向
      failTimer: 0, bubbleTimer: 0, lastToolBubble: 0,
      persona: null,         // 宠物人设(pet.json persona 字段, 启动时装载)
    };

    const setStatus = (name) => { statusEl.textContent = STATUS_TEXT[name] || STATUS_TEXT.idle; };

    function setBase(name) {
      pet.base = name;
      setStatus(name);
      // 拖动中不动 sprite: 跑动姿势由 dragPose 按 drag.dir 维持, 中途的
      // WS 事件若在此换动作, 下个 move 才被纠正, 观感即"转向慢半拍"
      if (!pet.overlay && sprite && !drag) sprite.play(name);
    }

    function overlayOnce(name) {
      pet.overlay = name;
      setStatus(name);
      // 同上: 拖动期间一次性动作不播(拖动姿态优先), 只记标记
      if (sprite && !drag) {
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

    // 互斥显示: 状态行与气泡共用一个位置, 气泡可见时状态行让位
    // (气泡文字本身已表达状态, 两个框叠着既挤又重复)
    function syncTextVisibility() {
      statusEl.style.visibility = bubbleEl.hidden ? "visible" : "hidden";
    }

    function bubble(text, ms = 2400) {
      if (!text) return;
      const t = String(text);
      bubbleEl.textContent = t.length > BUBBLE_MAX ? t.slice(0, BUBBLE_MAX - 1) + "…" : t;
      bubbleEl.classList.remove("bubble-dim");
      bubbleEl.hidden = false;
      syncTextVisibility();
      clearTimeout(pet.bubbleTimer);
      if (ms > 0) pet.bubbleTimer = setTimeout(() => {
        bubbleEl.hidden = true;
        syncTextVisibility();
      }, ms);
    }

    function hideBubble() {
      clearTimeout(pet.bubbleTimer);
      bubbleEl.hidden = true;
      syncTextVisibility();
    }

    function fail(text) {
      clearTimeout(pet.failTimer);
      setBase("failed");
      bubble(text || "出事了…");
      pet.failTimer = setTimeout(() => {
        // 回落时按聚合状态重算: 别的会话还在跑就继续"打工", 别装闲
        if (pet.base === "failed") recomputeBase();
      }, 3500);
    }

    /* ---- 多会话聚合: 桌宠跟的是全局, 不是某个会话 ----
     * 每个会话记一个 phase: run=轮次进行中 / await=等模型输出空窗 / idle=空闲。
     * 姿态优先级: 待批权限 > 任一会话打工 > 任一会话等输出 > 摸鱼。
     * 只有最后一个活跃会话收工那一刻才播"收工", 单个会话结束不打扰。 */
    const sessionPhase = new Map();   // sid -> "run" | "await" | "idle"
    const pendingPerms = new Map();   // sid -> {request_id, tool_name, title}

    const permKey = (sid) => String(sid != null ? sid : "?");
    const anyPhase = (...phases) =>
      [...sessionPhase.values()].some(v => phases.includes(v));
    const othersActive = (sid) =>
      [...sessionPhase].some(([k, v]) => k !== sid && (v === "run" || v === "await"));

    function recomputeBase() {
      if (pendingPerms.size) {
        if (pet.base !== "waiting") setBase("waiting");
        return;
      }
      if (anyPhase("run")) { setBase(pet.runDir || "running"); return; }
      if (anyPhase("await")) {
        if (pet.base !== "waiting") setBase("waiting");
        return;
      }
      if (pet.base !== "idle") setBase("idle");
    }

    /* 展示最近一个待批请求（多个会话同时等批时显示最新的那个）。
     * 气泡说清楚"谁想跑什么命令", 命令比会话名优先保住不截断 */
    function showLatestPerm() {
      let sid = null, p = null;
      for (const [k, v] of pendingPerms) { sid = k; p = v; }
      if (!p) { pet.pendingPerm = null; return; }
      pet.pendingPerm = { session_id: sid, request_id: p.request_id };
      setBase("waiting");
      const cta = "（点我同意）";
      const body = (p.title ? `「${p.title}」` : "")
        + (p.cmd ? `想运行: ${p.cmd}` : `想用 ${p.tool_name || "工具"}`);
      const max = BUBBLE_MAX - cta.length;
      bubble((body.length > max ? body.slice(0, max - 1) + "…" : body) + cta, 0);
    }

    function forgetPerm(sid) {
      if (!pendingPerms.delete(permKey(sid))) return;
      if (pendingPerms.size) { showLatestPerm(); return; }
      stats.waitSince = 0;   // 待批清空: 催条子计时归零
      pet.pendingPerm = null;
      recomputeBase();
    }

    /* 临时气泡播完把权限气泡找回来: 等批期间别的会话的工具/接单气泡
     * 不该把"点我同意"顶没 */
    function transientBubble(text, ms) {
      bubble(text, ms);
      if (pendingPerms.size) {
        clearTimeout(pet.permTimer);
        pet.permTimer = setTimeout(() => {
          if (pendingPerms.size && pet.base === "waiting") showLatestPerm();
        }, (ms || 2400) + 200);
      }
    }

    /* ---- AI 互动: 台词包 / 周期点评 / 悬浮输入框(点歌+聊天) ----
     * 人设来自宠物 pet.json 的 persona 字段(随 /api/pets 返回, 启动时装载):
     * name/style 由后端拼进 system prompt; lines 同名分组覆盖内置台词包。
     * AI 开关与模型在主窗设置页(xc-pet.aiChatter/aiProvider/aiModel),
     * 悬浮窗每次现读 pref(), 主窗改动经 storage 事件天然生效。 */
    const lines = {
      ack: PET_LINES.ack.slice(), done: PET_LINES.done.slice(),
      approve: PET_LINES.approve.slice(), quip: PET_LINES.quip.slice(),
      idle: PET_LINES.idle.slice(),
    };
    const lastPick = {};
    const pick = (group) => {
      const arr = lines[group];
      if (!arr || !arr.length) return "";
      if (arr.length === 1) return arr[0];
      let i;
      do { i = Math.random() * arr.length | 0; } while (i === lastPick[group]);
      lastPick[group] = i;
      return arr[i];
    };
    function applyPersona(persona) {
      pet.persona = persona && typeof persona === "object" ? persona : null;
      const pack = pet.persona && pet.persona.lines;
      if (!pack) return;
      for (const g of Object.keys(lines)) {
        const ls = Array.isArray(pack[g])
          ? pack[g].filter(s => typeof s === "string" && s.trim()).slice(0, 12)
          : null;
        if (ls && ls.length) {
          lines[g] = ls;
          delete lastPick[g];
        }
      }
    }

    /* ---- 现场统计器: 事件驱动台词的信号源, 全部本地数据 ----
     * runStart 本轮开工时刻 / tools 本轮工具直方图 / lastToolRun 同名
     * 工具连用计数 / errStreak 连续出错轮数 / idleSince 何时开始闲 /
     * waitSince 待批从何时起 / lastSpont 最近一次主动开口 / said 各事件
     * 的上次开口时间(独立冷却)。只记名字和数字, 不碰代码内容。 */
    const stats = {
      runStart: 0, tools: {}, lastTool: "", lastToolRun: 0,
      errStreak: 0, idleSince: Date.now(), waitSince: 0,
      lastSpont: 0, lastFiller: 0, said: {},
    };
    const EVT_COLD = {   // 各事件的冷却(ms): 每类话不烦人
      toolstreak: 0,          // 每轮同一条连击只说一次(计数 === 阈值时触发)
      errstreak: 600000,      // 出错吐槽 10 分钟一次
      longrun: 0,             // 每轮"跑很久"只说一次
      waitlong: 0,            // 每次待批期间只催一次
      idlelong: 0,            // 每段空闲只念一次
      latenight: 21600000,    // 深夜梗 6 小时一次
      friday: 72000000,       // 周五梗 20 小时一次
    };
    const MIN_GAP = 90000;    // 任意两句主动话之间至少隔 90s

    function canSay(tag, now) {
      if (now - stats.lastSpont < MIN_GAP) return false;
      return now - (stats.said[tag] || 0) >= (EVT_COLD[tag] || 0);
    }

    // AI 请求: 8s 超时; 任何失败返回 null, 调用方回退台词包
    async function aiChat(payload) {
      const p = pref();
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 8000);
        const r = await fetch("/api/pet/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            persona: pet.persona || {},
            provider_id: p.aiProvider || "",
            model_id: p.aiModel || "",
            ...payload,
          }),
          signal: ctrl.signal,
        });
        clearTimeout(timer);
        if (!r.ok) return null;
        const data = await r.json();
        return data && typeof data === "object" ? data : null;
      } catch { return null; }
    }

    /* 事件开口: AI 开 = LLM 生成(人设 + "刚刚发生"事件上下文, 失败回退
     * 台词包); AI 关 = 直接抽台词包。返回是否真的开了口。 */
    async function quipLine(tag, event, extra) {
      if (pref().aiChatter === true) {
        const data = await aiChat({
          state: {
            event, base: pet.base,
            run_minutes: extra && extra.runMinutes,
            top_tools: extra && extra.topTools,
            errors: extra && extra.errors,
            hour: new Date().getHours(),
            busy_sessions: [...sessionPhase.values()].filter(v => v !== "idle").length,
          },
        });
        const say = data && typeof data.say === "string" ? data.say.trim() : "";
        if (say) return say;
      }
      return pick(tag || (/^running/.test(pet.base) ? "quip" : "idle"));
    }

    async function speakEvent(tag, event, extra) {
      const now = Date.now();
      if (!canSay(tag, now)) return;
      // 等批场景说"催条子"要走 transientBubble(说完把"点我同意"找回来),
      // 其余事件不打断权限气泡
      const inWait = !!pendingPerms.size;
      if (tag !== "waitlong" && (inWait || !bubbleEl.hidden)) return;
      stats.said[tag] = now;
      stats.lastSpont = now;
      const say = await quipLine(tag, event, extra);
      // 请求期间状态可能变: 落话前再让一次位(同上, waitlong 例外)
      if (tag !== "waitlong" && (pendingPerms.size || !bubbleEl.hidden)) return;
      if (tag === "waitlong") transientBubble(say, 2600);
      else bubble(say, 3200);
    }

    /* 30s 一跳的事件评估器: 一跳最多说一句, 按优先级短路。
     * 兜底随机点评退居最后(间隔 6 分钟起 + 30% 概率), 让位给真事件 */
    let tickTimer = 0;
    function scheduleTick() {
      clearTimeout(tickTimer);
      tickTimer = setTimeout(async () => {
        await evalEvents();
        scheduleTick();
      }, 30000);
    }
    async function evalEvents() {
      const now = Date.now();
      const d = new Date();
      const hour = d.getHours();
      // 1. 审批等超 1 分钟: 催条子(等批期间唯一允许的话)
      if (pendingPerms.size) {
        if (stats.waitSince && now - stats.waitSince >= 60000) {
          await speakEvent("waitlong", "主人的审批请求等了一分多钟还没批");
        }
        return;   // 等批期间别的都不说
      }
      // 2. 连续出错
      if (stats.errStreak >= 2) {
        await speakEvent("errstreak", `连着 ${stats.errStreak} 轮都出错了`);
        return;
      }
      const running = /^running/.test(pet.base);
      const runMin = stats.runStart ? Math.floor((now - stats.runStart) / 60000) : 0;
      if (running) {
        // 3. 单轮超 15 分钟
        if (runMin >= 15) {
          await speakEvent("longrun", `这一轮已经跑了 ${runMin} 分钟还没完`,
            { runMinutes: runMin });
          if (stats.said.longrun === now) return;
        }
        // 4. 深夜 / 周五下午
        if (hour >= 23 || hour < 5) {
          await speakEvent("latenight", `现在已经是深夜 ${hour} 点还在干活`);
          if (stats.said.latenight === now) return;
        } else if (d.getDay() === 5 && hour >= 14 && hour < 18) {
          await speakEvent("friday", "今天是周五下午, 主人还在干活");
          if (stats.said.friday === now) return;
        }
        // 5. 兜底: 带现场统计的随机点评(上句主动话 6 分钟后, 30% 概率)
        if (now - Math.max(stats.lastSpont, stats.lastFiller) >= 360000
            && Math.random() < 0.3) {
          stats.lastFiller = now;
          stats.lastSpont = now;
          const topTools = Object.entries(stats.tools).sort((a, b) => b[1] - a[1])
            .slice(0, 3).map(([t, n]) => `${t}×${n}`).join(" ");
          const say = await quipLine("", "", {
            runMinutes: runMin, topTools, errors: stats.errStreak,
          });
          if (!pendingPerms.size && bubbleEl.hidden) bubble(say, 3200);
        }
      } else {
        // 6. 闲超 30 分钟
        if (now - stats.idleSince >= 1800000) {
          await speakEvent("idlelong", "主人已经闲了半个多小时了");
        }
      }
    }
    scheduleTick();

    // 任务转发: pet → 主窗, 走 storage 事件(跨 WebView2 实测可靠; 与事件
    // 转发相反的方向, 同一套打法)。to 字段区分方向, 双方各自过滤;
    // __n 用自增序号——storage 事件只在值变化时触发, 时间戳撞毫秒会丢
    let petCmdSeq = 0;
    const sendPetCmd = (cmd) => {
      try {
        localStorage.setItem("xc-pet-cmd",
          JSON.stringify({ ...cmd, to: "main", __n: ++petCmdSeq }));
      } catch { }
    };

    /* 悬浮输入框 = 全功能任务入口: 文本发给主窗, 落到专属「桌宠」会话跑
     * 完整 agent 轮次——与主界面输入框同一条链路。点歌由 music_play
     * 工具执行; 权限请求照常顶到"等你批条子"(点宠物批准)。回复经
     * xc-pet-cmd 回传, 以气泡形式短驻展示。 */
    const inputEl = $("pet-input");
    // 原生前台聚焦: 透明置顶小窗默认不抢前台, 原生窗不在前台时 IME
    // 挂不到本窗(候选框飘左上角/切不出输入法)——点输入框第一下就要
    // set_focus, 这之后才谈得上正常打字
    const focusNative = () => { try { bridge()?.focusPet?.(); } catch { } };
    if (inputEl) {
      inputEl.addEventListener("pointerdown", focusNative);
      inputEl.addEventListener("focus", focusNative);
      // 打字期间(含 IME 组合态)焦点在手, 框经 :focus 保持可见——
      // 特意不监听 mouseleave/blur 收框: 鼠标挪开打字曾把框藏掉,
      // 表现为"输入途中消失"。收框只有两条路: Esc 或发送后
      inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { inputEl.blur(); return; }
        if (e.key !== "Enter" || e.isComposing) return;
        const text = inputEl.value.trim();
        if (!text) return;
        inputEl.value = "";
        inputEl.blur();
        sendPetCmd({ type: "task", text: text.slice(0, 2000) });
        transientBubble("派活了, 干着呢…", 15000);   // 回复到达即替换
      });
    }
    // 主窗回传: 任务回复(收口时最后一段正文; 空回复=纯工具轮, 歌已在放,
    // 亮起的播放条就是答复, 不说话)
    window.addEventListener("storage", (e) => {
      if (e.key !== "xc-pet-cmd" || !e.newValue) return;
      try {
        const cmd = JSON.parse(e.newValue);
        if (cmd.to === "pet" && cmd.type === "chat" && cmd.text) {
          transientBubble(String(cmd.text), 4000);
        }
      } catch { }
    });

    function onEvent(msg) {
      if (!msg || !msg.type) return;
      const sid = permKey(msg.session_id);
      switch (msg.type) {
        case "turn_started":
          clearTimeout(pet.failTimer);
          if (!anyPhase("run")) stats.runStart = Date.now();   // 新一轮开工
          sessionPhase.set(sid, "run");
          // 本轮现场清零: 工具直方图/连击/每轮一次的台词标记
          stats.tools = {};
          stats.lastTool = ""; stats.lastToolRun = 0;
          delete stats.said.toolstreak;
          delete stats.said.longrun;
          recomputeBase();
          transientBubble(pick("ack"));
          break;
        case "tool_use_started": {
          sessionPhase.set(sid, "run");
          // 现场统计: 只记工具名与次数; 同名连用 6 次说一句
          const tn = String(msg.tool_name || "工具");
          stats.tools[tn] = (stats.tools[tn] || 0) + 1;
          stats.lastToolRun = tn === stats.lastTool ? stats.lastToolRun + 1 : 1;
          stats.lastTool = tn;
          if (stats.lastToolRun >= 6) {
            speakEvent("toolstreak", `${tn} 连着用了 ${stats.lastToolRun} 次`);
          }
          // 每次工具换一个跑动方向, 桌面上看得到"在忙"
          pet.alt = (pet.alt + 1) % 3;
          pet.runDir = pet.alt === 0 ? "running"
            : pet.alt === 1 ? "running-right" : "running-left";
          recomputeBase();
          const now = Date.now();
          if (now - pet.lastToolBubble > 900) {
            pet.lastToolBubble = now;
            transientBubble((msg.tool_name || "工具") + "…");
          }
          break;
        }
        case "tool_result":
          if (/^running/.test(pet.base)) overlayOnce("review");   // 瞄一眼结果
          forgetPerm(sid);   // 出结果 = 该会话的待批请求已有去向
          break;
        case "permission_request":
          if (!pendingPerms.size) stats.waitSince = Date.now();   // 新一轮待批
          delete stats.said.waitlong;
          pendingPerms.set(sid, {
            request_id: msg.request_id || null,
            tool_name: msg.tool_name || "",
            title: msg.session_title || "",
            cmd: msg.command_summary || "",
          });
          showLatestPerm();
          break;
        case "permission_resolved":
          // 请求已有去向（主窗批的/桌宠批的/已过期）: 立刻撤下待批展示
          forgetPerm(sid);
          break;
        case "await_output":
          sessionPhase.set(sid, "await");
          recomputeBase();
          if (!pendingPerms.size && pet.base === "waiting") bubble("等你补充…", 0);
          break;
        case "turn_done": {
          sessionPhase.set(sid, "idle");
          forgetPerm(sid);
          clearTimeout(pet.failTimer);
          stats.errStreak = 0;   // 正常收工: 出错连击清零
          if (!anyPhase("run")) {
            stats.runStart = 0;
            stats.idleSince = Date.now();
          }
          if (othersActive(sid) || pendingPerms.size) {
            recomputeBase();   // 还有别的会话在忙: 不播收工, 继续打工
          } else if (msg.interrupted) {
            fail("停了…");
          } else {
            pet.base = "idle";
            setStatus("idle");
            overlayOnce("jumping");
            bubble(pick("done"));
          }
          break;
        }
        case "error": {
          sessionPhase.set(sid, "idle");
          forgetPerm(sid);
          stats.errStreak += 1;   // 连续出错: 评估器里说一句(带冷却)
          if (!anyPhase("run")) {
            stats.runStart = 0;
            stats.idleSince = Date.now();
          }
          const text = msg.message ? String(msg.message) : "出事了…";
          if (othersActive(sid) || pendingPerms.size) {
            transientBubble("出错: " + text, 3000);
            recomputeBase();
          } else {
            fail(text);
          }
          break;
        }
        case "rate_limited_retry":
          transientBubble(`限流了, ${msg.delay_s}s 后重试…`);
          break;
        case "turn_interrupting":
          forgetPerm(sid);   // 叫停会连着把该会话的待批请求按拒绝收走
          transientBubble("等等, 我停——");
          break;
      }
    }

    /* 点宠物批条子: waiting 态 + 有未决请求时, 单击(非拖拽) = 批准。
     * 走 REST /api/permissions/respond, 与主窗 WS 批复同一条 prompter
     * 链路——请求已被主窗处理时 resolve 静默忽略 stale id, 幂等。 */
    async function approvePendingPerm() {
      const p = pet.pendingPerm;
      if (!p || !p.request_id) return false;
      const sid = p.session_id || new URLSearchParams(location.search).get("session") || "";
      pet.pendingPerm = null;   // 先清: 防双击重复提交
      try {
        const r = await fetch("/api/permissions/respond", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sid, request_id: p.request_id, approved: true }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        pendingPerms.delete(permKey(sid));
        if (pendingPerms.size) showLatestPerm();
        else recomputeBase();   // 别的会话还在跑就继续打工, 全闲才回 idle
        overlayOnce("jumping");
        transientBubble(pick("approve"));
        return true;
      } catch (e) {
        bubble("没批成…去主窗口看看");
        return false;
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
    // drag.acc: 反向行程累计器(饱和区间 ±DIR_REV_PX)。每个 move 事件的
    // 增量与当前朝向相反时累计、同向时清零, 越过阈值立刻掉头——转向跟随
    // 鼠标的"最近运动方向", 而不是相对起点的累计位移(旧法: 先右拖 100px
    // 再往回, dx 仍为正, 人物迟迟不掉头, 观感即"转向延迟")。
    let drag = null;      // {sx,sy,wx,wy,lastX,acc,dir,moved,dustT,hadBubble}
    let wasDrag = false;  // 松手后短暂置真, 让 click 区分"拖完"与"点击"
    const DIR_REV_PX = 4; // 反向掉头阈值(每侧): 约 8px 反向行程即转向

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
        lastX: e.screenX,
        acc: 0, dir: 1,
        moved: false, dustT: 0,
        hadBubble: !bubbleEl.hidden,
      };
      try { spriteEl.setPointerCapture(e.pointerId); } catch { }
      e.preventDefault();
    });
    // 窗口挪动按帧合并: pointermove 频率可高于刷新率, 逐事件 IPC 会在
    // 队列里排队造成"窗追不上手"的滞后; rAF 保证每帧只发最新坐标。
    let pendingPos = null, moveRaf = 0;
    const flushMove = () => {
      moveRaf = 0;
      if (!pendingPos || !drag) return;
      bridge().movePet(pendingPos[0], pendingPos[1]).catch(() => { });
      pendingPos = null;
    };
    window.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.screenX - drag.sx, dy = e.screenY - drag.sy;
      if (!drag.moved) {
        if (Math.hypot(dx, dy) < 5) return;   // 抖动阈值: 按住不动不算拖
        drag.moved = true;
        drag.lastX = e.screenX;               // 阈值内的位移不计入转向判定
        bubbleEl.hidden = true;               // 拖动时气泡让路
      }
      pendingPos = [drag.wx + dx, drag.wy + dy];
      if (!moveRaf) moveRaf = requestAnimationFrame(flushMove);
      // 转向: 跟随最近的运动方向(增量 + 饱和反向累计器), 与起点无关
      const step = e.screenX - drag.lastX;
      drag.lastX = e.screenX;
      if (step) {
        const dirNow = step >= 0 ? 1 : -1;
        if (dirNow === drag.dir) {
          drag.acc = 0;                       // 顺朝向的运动, 清掉反向累计
        } else {
          drag.acc = Math.min(drag.acc + Math.abs(step), DIR_REV_PX);
          if (drag.acc >= DIR_REV_PX) {       // 反向行程足够 → 立刻掉头
            drag.dir = dirNow;
            drag.acc = 0;
            dragTilt(6 * drag.dir);           // 身体朝运动方向倾
          }
        }
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
      if (moveRaf) { cancelAnimationFrame(moveRaf); moveRaf = 0; }
      if (pendingPos) {   // 收尾前把最后一帧位置落盘, 避免停在半路
        bridge().movePet(pendingPos[0], pendingPos[1]).catch(() => { });
        pendingPos = null;
      }
      dragTilt("");
      if (!d.moved) return;
      wasDrag = true;
      setTimeout(() => (wasDrag = false), 350);
      puff(3);
      // 落地回弹: 压扁→弹起→复原(CSS 动画盖掉内联 transform)
      spriteEl.classList.remove("pet-land");
      void spriteEl.offsetWidth;
      spriteEl.classList.add("pet-land");
      if (d.hadBubble && bubbleEl.textContent) {
        bubbleEl.hidden = false;
        syncTextVisibility();
      }
      if (sprite) sprite.play(pet.base);
      setStatus(pet.base);
    }
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", endDrag);
    spriteEl.addEventListener("click", () => {
      if (wasDrag) return;
      // 等批条子时点击仍是批准——功能优先于玩具
      if (pet.base === "waiting" && pet.pendingPerm) {
        approvePendingPerm();
        return;
      }
      // 点击宠物 = 开/关音乐播放。队列是空的就不折腾主窗了, 直接引导点歌
      let hasSong = false;
      try {
        const m = JSON.parse(localStorage.getItem("xc-music") || "{}");
        hasSong = Array.isArray(m.queue) && m.queue.length > 0;
      } catch { }
      overlayOnce("waving");
      if (hasSong) {
        sendPetCmd({ type: "music_toggle" });
        bubble("♪ …", 2600);   // 回执到达即替换
      } else {
        bubble("先点首歌呗", 2200);
      }
    });

    // 量图集里"角色最高点距格顶"的最小像素数（所有行所有帧取最小）。
    // 不同宠物的头顶留白差异很大: 菲比有宽檐帽、头顶留白 ~30px,
    // Hoops 的头发几乎顶格。文字栈的下沉量据此自适应, 别把气泡压头上。
    function measureHeadroom(img, rows) {
      try {
        const w = img.naturalWidth, h = img.naturalHeight;
        if (!w || !h) return null;
        const cellH = Math.max(1, Math.floor(h / rows));
        const c = document.createElement("canvas");
        c.width = w; c.height = h;
        const ctx = c.getContext("2d");
        ctx.drawImage(img, 0, 0);
        const data = ctx.getImageData(0, 0, w, h).data;
        let headroom = cellH;
        for (let row = 0; row < rows; row++) {
          const top = row * cellH;
          for (let dy = 0; dy < headroom; dy++) {
            let hit = false;
            for (let x = 0; x < w; x++) {
              if (data[((top + dy) * w + x) * 4 + 3] > 16) { hit = true; break; }
            }
            if (hit) { headroom = dy; break; }
          }
        }
        return headroom;
      } catch {
        return null;   // canvas 被污染等意外: 回落 CSS 默认下沉量
      }
    }

    // ---- 启动: 拉宠物列表 → 渲染 → 打招呼 ----
    (async () => {
      try {
        // cache-bust 必须带: WebView2 启发式缓存曾把 /api/pets 存成旧列表,
        // 刷新进来的新宠物在这里"不存在", 选它唤醒就白屏(重启才恢复)。
        // 服务端已加 no-store, 此处时间戳是双保险。
        const res = await fetch(`/api/pets?_=${Date.now()}`);
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const pets = data.pets || [];
        const chosen = pets.find(p => p.id === pref().petId) || pets[0];
        if (!chosen) {
          statusEl.textContent = "没有宠物";
          bubble("把宠物文件夹放进 pets 目录(设置里可查路径)", 0);
          return;
        }
        // 人设随清单一起来: lines 覆盖内置台词包, name/style 留给 AI 请求
        applyPersona(chosen.persona);
        const sheetUrl = `/api/pets/${encodeURIComponent(chosen.id)}/sheet?_=${Date.now()}`;
        sprite = new PetSprite(spriteEl, sheetUrl, chosen.rows);
        // 气泡下沉量自适应: 量出头顶留白, 下沉 ≤ 留白-6px 视觉间隙,
        // 至多 22px（Codex 契约格顶留白量级）; 头顶顶格的宠物就不下沉。
        const mimg = new Image();
        mimg.onload = () => {
          const headroom = measureHeadroom(mimg, chosen.rows);
          if (headroom != null) {
            const sink = Math.max(0, Math.min(22, headroom - 6));
            $("pet-text").style.marginBottom = `-${sink}px`;
          }
        };
        mimg.src = sheetUrl;   // 同一 URL, 走 PetSprite 已建的 HTTP 缓存
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
      "permission_request", "permission_resolved", "await_output", "turn_done",
      "error", "rate_limited_retry", "turn_interrupting"]);
    let seq = 0;
    /* 权限气泡要展示具体命令: 从工具入参里取主字段压成一行 */
    function permCmdSummary(input) {
      try {
        const d = JSON.parse(input || "{}");
        const raw = String(d.command || d.file_path || d.path || d.pattern || d.url || "");
        return raw.replace(/\s+/g, " ").trim().slice(0, 80);
      } catch { return ""; }
    }
    window.xcodePet = {
      // sid 由 app.js handleServerMessage 调用时传入（本 IIFE 里没有这个变量,
      // 早期版本在这里读裸名 sid —— 每条无 session_id 的服务端事件都抛
      // ReferenceError, 把 turn_done/tool_result 的处理一起炸掉, 表现为
      // 正文照常流出但永远转圈、工具卡停在"运行中"、点停止无反应）
      onEvent(msg, sid) {
        if (!msg || !PET_EVT.has(msg.type)) return;
        // 补会话 id: 桌宠"点宠物批准"要按 session_id 定位批复端点
        let withSid = (msg.session_id != null) ? msg
          : { ...msg, session_id: sid != null ? sid : null };
        // 权限请求要标注会话名 + 具体命令（多会话聚合后宠物说得出"谁想跑什么"）
        if (withSid.type === "permission_request") {
          let title = "";
          try { title = window.xcodeSessionTitle?.(withSid.session_id) || ""; } catch { }
          withSid = {
            ...withSid,
            session_title: title,
            command_summary: permCmdSummary(withSid.input),
          };
        }
        try { ch && ch.postMessage(withSid); } catch { }
        try {
          localStorage.setItem("xc-pet-evt", JSON.stringify({ ...withSid, __n: ++seq }));
        } catch { }
      },
    };

    // 召唤入口: 底栏按钮 + 设置页按钮。悬浮窗是 Tauri 命令开的,
    // 其他环境(Electron/浏览器)没有桥——底栏按钮藏掉, 设置按钮置灰提示。
    // scale 传当前缩放, 开窗即按用户设置定尺寸。
    const petFloat = () => window.xcodeDesktopPet.petFloat(pref().scale).catch(e => console.error("[pet]", e));
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

    // ---- 大小滑杆: 50%–200% 写 pref().scale 并持久化。有桌面桥时
    //      即时 resize 悬浮窗（storage 事件也会通知已开的悬浮窗,
    //      这里直接调是双保险: 桥可能在 storage 事件送达前尚未就绪）。
    const PET_SCALE_RANGE = [0.5, 2];
    const clampScale = (v) => {
      const n = Number(v);
      if (!Number.isFinite(n)) return 1;
      return Math.min(PET_SCALE_RANGE[1], Math.max(PET_SCALE_RANGE[0], n));
    };
    const sizeInput = $("pet-size");
    if (sizeInput) {
      const cur = clampScale(pref().scale);
      sizeInput.value = String(Math.round(cur * 100));
      $("pet-size-val").textContent = `${Math.round(cur * 100)}%`;
      sizeInput.addEventListener("input", () => {
        const pct = parseInt(sizeInput.value, 10) || 100;
        $("pet-size-val").textContent = `${pct}%`;
        const s = clampScale(pct / 100);
        savePref({ ...pref(), scale: s });
        try { window.xcodeDesktopPet?.resizePet?.(s); } catch { }
      });
    }
    // ---- AI 互动: 开关 + 桌宠专属模型。偏好同住 xc-pet, 悬浮窗经 storage
    //      事件实时跟随(pref() 现读, 无需专门通知)。默认关: 点评和智能
    //      点歌都要花所配模型的 token, 由用户显式打开。模型下拉数据异步
    //      拉取后 setItems 补齐, 拉失败时保底"跟随全局模型"单项。 ----
    const chatter = $("pet-ai-chatter");
    if (chatter) {
      chatter.checked = pref().aiChatter === true;
      chatter.addEventListener("change", () => {
        savePref({ ...pref(), aiChatter: chatter.checked });
        toast(chatter.checked ? "桌宠 AI 互动已开启" : "桌宠 AI 互动已关闭");
      });
    }
    const modelDdEl = $("sel-pet-model");
    if (modelDdEl && typeof makeDropdown === "function") {
      const p = pref();
      const saved = (p.aiProvider && p.aiModel) ? `${p.aiProvider}|${p.aiModel}` : "";
      const modelDd = makeDropdown(modelDdEl, {
        items: [{ value: "", label: "跟随全局模型" }],
        value: saved,
        onChange: (v) => {
          const i = v.indexOf("|");
          savePref({
            ...pref(),
            aiProvider: i > 0 ? v.slice(0, i) : "",
            aiModel: i > 0 ? v.slice(i + 1) : "",
          });
        },
      });
      (async () => {
        try {
          const cfg = await (await fetch("/api/providers")).json();
          const items = [{ value: "", label: "跟随全局模型" }];
          for (const prov of (cfg.providers || [])) {
            if (prov.enabled === false) continue;
            for (const m of (prov.models || [])) {
              items.push({
                value: `${prov.id}|${m.id}`,
                label: `${prov.name || prov.id} / ${m.id}`,
              });
            }
          }
          // 已保存的组合不在列表里(供应商被删/禁用): 保留原值并标注, 不静默丢
          if (saved && !items.some(it => it.value === saved)) {
            items.push({ value: saved, label: `已保存: ${saved}（列表里没有）` });
          }
          modelDd.setItems(items, saved);
        } catch { }
      })();
    }
    // 刷新: 重扫宠物目录并重绘列表(带 cache-bust, 刚替换的精灵图也能立即生效)
    const rescanBtn = $("btn-pet-rescan");
    if (rescanBtn) {
      rescanBtn.addEventListener("click", async () => {
        rescanBtn.disabled = true;
        try {
          await renderPicker(true);
          toast("宠物列表已刷新");
        } finally {
          rescanBtn.disabled = false;
        }
      });
    }

    renderPicker();
  }

  async function renderPicker(bust) {
    const wrap = $("pet-picker");
    if (!wrap) return;
    // bust: 刷新按钮传真值——列表 URL 与缩略图都加时间戳, 绕过 HTTP 缓存,
    // 否则刚替换的 spritesheet 会显示旧图
    const bustArg = bust ? `?_=${Date.now()}` : "";
    let data = null;
    try { data = await (await fetch(`/api/pets${bustArg}`)).json(); } catch { }
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
      row.querySelector(".pet-item-badge").textContent =
        p.source === "codex" ? "Codex" : p.source === "user" ? "我的" : "内置";
      const mini = row.querySelector(".pet-sprite-mini");
      mini.style.backgroundImage = `url("/api/pets/${encodeURIComponent(p.id)}/sheet${bustArg}")`;
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
