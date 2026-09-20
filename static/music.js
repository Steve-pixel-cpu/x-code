"use strict";
/* ============================================================
 * 摸鱼电台: 网易云迷你播放器（独立文件, 不碰 app.js 的会话逻辑）
 * 播放条停靠在侧栏底部空白区（#side-chat 内, 会话列表下方）,
 * 不与主区输入框重叠; 歌单/搜索面板从其上方弹出。
 * 在线流式播放免费曲库; VIP/无版权歌（后端 url=null）自动跳下一首。
 * ============================================================ */
(function () {
  if (window.__xcodeMusic) return;      // 防重复初始化
  window.__xcodeMusic = true;

  const DESKTOP_PAGE = !!document.getElementById("messages");   // 主界面才挂播放器
  const $ = id => document.getElementById(id);
  const $$ = sel => document.querySelectorAll(sel);
  const toastFn = window.xcodeToast || (msg => console.log("[music]", msg));

  const mstate = {
    queue: [],            // 当前播放队列 [{id,name,artist,album,duration,fee,pic}]
    qname: "",            // 队列来源名（榜单名 /「搜索: xx」/「播放列表」）
    index: -1,            // 当前歌在队列里的下标
    mode: "order",        // order | one | shuffle
    playing: false,
    failedStreak: 0,      // 连续跳过计数: 防"全队列不可播"死循环
    listCache: {},        // pid → {name, songs}（本次会话内缓存）
  };
  let audio = null;

  const MKEY = "xc-music";
  const pref = (() => {
    try { return JSON.parse(localStorage.getItem(MKEY)) || {}; }
    catch { return {}; }
  })();
  function savePref() {
    try {
      localStorage.setItem(MKEY, JSON.stringify({
        mode: mstate.mode,
        vol: audio ? Math.round(audio.volume * 100) : (pref.vol ?? 70),
        dock: !$("music-dock").hidden,
      }));
    } catch { /* 存不了就算了 */ }
  }

  function fmtTime(s) {
    if (!isFinite(s) || s < 0) s = 0;
    return Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");
  }

  function escapeText(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function getAudio() {
    if (audio) return audio;
    audio = new Audio();
    audio.preload = "auto";
    audio.volume = (pref.vol ?? 70) / 100;
    audio.addEventListener("ended", () => musicNext(true));
    audio.addEventListener("timeupdate", () => {
      $("music-progress-fill").style.width
        = (audio.currentTime / (audio.duration || 1) * 100) + "%";
      $("music-time").textContent = fmtTime(audio.currentTime);
    });
    audio.addEventListener("play", () => { setPlayingUi(true); markPlayingRow(); });
    audio.addEventListener("pause", () => { setPlayingUi(false); markPlayingRow(); });
    return audio;
  }

  /* ---- 播放条 UI ---- */
  function setPlayingUi(on) {
    mstate.playing = on;
    document.querySelector("#music-toggle .ic-play").style.display = on ? "none" : "";
    document.querySelector("#music-toggle .ic-pause").style.display = on ? "" : "none";
    $("music-cover").classList.toggle("spin", on);
    if ("mediaSession" in navigator) {
      navigator.mediaSession.playbackState = on ? "playing" : "paused";
    }
  }

  function curSong() { return mstate.queue[mstate.index] || null; }

  function refreshBar() {
    const s = curSong();
    $("music-title").textContent = s ? s.name : "未在播放";
    $("music-artist").textContent = s ? s.artist : "";
    if (s) {
      document.title = s.name + " - " + s.artist;   // 摸鱼: 标题栏只显示歌名
      if (s.pic) {
        $("music-cover").src = s.pic;
        if ("mediaSession" in navigator) {
          try {
            navigator.mediaSession.metadata = new MediaMetadata({
              title: s.name, artist: s.artist, album: s.album,
              artwork: [{ src: s.pic, sizes: "300x300" }],
            });
          } catch { /* 无关紧要 */ }
        }
      }
    } else {
      document.title = "x-code";
    }
  }

  /* ---- 核心: 播放队列里的某一首 ---- */
  async function playIndex(idx) {
    if (!mstate.queue.length) return;
    mstate.index = ((idx % mstate.queue.length) + mstate.queue.length) % mstate.queue.length;
    const s = mstate.queue[mstate.index];
    refreshBar();
    markPlayingRow();
    const a = getAudio();
    try {
      const r = await fetch(`/api/music/url?id=${s.id}&br=128000`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (!data.url) {
        toastFn(`「${s.name}」需要 VIP 或暂无版权, 已跳过`);
        return skipFailed();
      }
      a.src = data.url;
      await a.play();
      mstate.failedStreak = 0;
    } catch (e) {
      if (e && e.name === "AbortError") return;   // 用户快速切歌: 旧请求作废
      toastFn("播放失败: " + (e && e.message || e));
      return skipFailed();
    }
  }

  /* 当前歌拿不到直链/播放出错: 自动下一首, 连续失败超限就停 */
  function skipFailed() {
    mstate.failedStreak++;
    if (mstate.failedStreak > Math.min(mstate.queue.length, 10)) {
      mstate.failedStreak = 0;
      getAudio().pause();
      toastFn("连着多首都不可播, 已停止（换个歌单试试）");
      return;
    }
    musicNext(true);
  }

  function musicNext(auto) {
    if (!mstate.queue.length) return;
    if (mstate.mode === "one" && auto) {        // 单曲循环: 重头再放
      audio.currentTime = 0;
      audio.play().catch(() => {});
      return;
    }
    let idx;
    if (mstate.mode === "shuffle") {
      idx = mstate.queue.length > 1 ? (Math.random() * mstate.queue.length | 0) : 0;
      if (idx === mstate.index && mstate.queue.length > 1) idx = (idx + 1) % mstate.queue.length;
    } else {
      idx = mstate.index + 1;
    }
    playIndex(idx);
  }
  function musicPrev() { playIndex(mstate.index - 1); }

  /* ---- 歌单 / 搜索 ---- */
  async function loadPlaylist(pid, tabKey) {
    const body = document.querySelector(`#music-panel [data-tab-body="${tabKey}"]`);
    body.innerHTML = '<div class="mp-empty">加载中…</div>';
    try {
      let data = mstate.listCache[pid];
      if (!data) {
        const r = await fetch(`/api/music/playlist/${pid}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        data = await r.json();
        mstate.listCache[pid] = data;
      }
      mstate.qname = data.name;
      renderSongList(body, data.songs, { replaceQueue: true });
    } catch (e) {
      body.innerHTML = `<div class="mp-empty">加载失败: ${escapeText(e.message)}</div>`;
    }
  }

  function songRow(s, i, deletable) {
    const row = document.createElement("div");
    row.className = "mp-song";
    row.dataset.songId = s.id;
    row.innerHTML =
      '<span class="mp-s-eq"><i></i><i></i><i></i></span>' +
      `<span class="mp-s-idx">${i + 1}</span>` +
      '<span class="mp-s-main"><span class="mp-s-name"></span>' +
      '<span class="mp-s-artist"></span></span>' +
      (s.fee === 1 ? '<span class="mp-s-tag">VIP</span>' : "") +
      `<span class="mp-s-dur">${fmtTime(s.duration)}</span>` +
      (deletable
        ? '<button class="mp-s-del" data-tip="移出列表"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>'
        : "");
    row.querySelector(".mp-s-name").textContent = s.name;
    row.querySelector(".mp-s-artist").textContent = s.artist;
    return row;
  }

  function renderSongList(body, songs, { replaceQueue = true } = {}) {
    if (!songs.length) {
      body.innerHTML = '<div class="mp-empty">没有结果</div>';
      return;
    }
    if (replaceQueue) {
      mstate.queue = songs.slice();
      mstate.index = -1;
    }
    body.innerHTML = "";
    songs.forEach((s, i) => {
      const row = songRow(s, i, false);
      row.onclick = () => playIndex(i);
      body.appendChild(row);
    });
    markPlayingRow();
  }

  async function doSearch() {
    const kw = $("music-search-input").value.trim();
    if (!kw) return;
    const body = $("music-search-results");
    body.innerHTML = '<div class="mp-empty">搜索中…</div>';
    try {
      const r = await fetch(`/api/music/search?kw=${encodeURIComponent(kw)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      mstate.qname = "搜索: " + kw;
      renderSongList(body, data.songs, { replaceQueue: true });
    } catch (e) {
      body.innerHTML = `<div class="mp-empty">搜索失败: ${escapeText(e.message)}</div>`;
    }
  }

  /* ---- 播放列表 Tab ---- */
  function renderQueue() {
    const body = document.querySelector('#music-panel [data-tab-body="queue"]');
    if (!mstate.queue.length) {
      body.innerHTML = '<div class="mp-empty">播放列表是空的, 去热歌榜或搜索里点几首吧</div>';
      return;
    }
    body.innerHTML =
      '<div class="mp-queue-head"><span></span><button id="music-queue-clear">清空</button></div>';
    body.querySelector("span").textContent
      = `${mstate.qname || "播放列表"} · ${mstate.queue.length} 首`;
    body.querySelector("#music-queue-clear").onclick = () => {
      getAudio().pause();
      mstate.queue = [];
      mstate.index = -1;
      refreshBar();
      renderQueue();
    };
    const list = document.createElement("div");
    mstate.queue.forEach((s, i) => {
      const row = songRow(s, i, true);
      if (i === mstate.index) row.classList.add("playing");
      row.querySelector(".mp-s-del").onclick = (ev) => {
        ev.stopPropagation();
        removeQueueAt(i);
      };
      row.onclick = () => playIndex(i);
      list.appendChild(row);
    });
    body.appendChild(list);
  }

  function removeQueueAt(i) {
    mstate.queue.splice(i, 1);
    if (i < mstate.index) {
      mstate.index--;
    } else if (i === mstate.index) {
      mstate.index = Math.min(mstate.index, mstate.queue.length - 1);
      if (mstate.playing && mstate.index >= 0) {
        playIndex(mstate.index);
      } else {
        getAudio().pause();
        refreshBar();
      }
    }
    renderQueue();
  }

  function markPlayingRow() {
    const curId = curSong() ? String(curSong().id) : null;
    $$("#music-panel .mp-song").forEach(row => {
      const isCur = row.dataset.songId === curId;
      row.classList.toggle("playing", isCur);
      const idx = row.querySelector(".mp-s-idx");
      const eq = row.querySelector(".mp-s-eq");
      if (idx) idx.style.display = isCur ? "none" : "";
      if (eq) eq.style.display = isCur && mstate.playing ? "inline-flex" : "none";
    });
  }

  /* ---- 面板 / Tab ---- */
  function switchTab(key) {
    $$("#music-panel .mp-tabs > button[data-tab]").forEach(b => {
      b.classList.toggle("on", b.dataset.tab === key);
    });
    $$("#music-panel .mp-tab-body").forEach(b => {
      b.style.display = b.dataset.tabBody === key ? "" : "none";
    });
    if (key === "queue") renderQueue();
  }

  function ensurePlaylistTab(tabKey, pid) {
    const body = document.querySelector(`#music-panel [data-tab-body="${tabKey}"]`);
    if (!body.children.length) loadPlaylist(pid, tabKey);
  }

  /* 面板锚在 dock 正上方（dock 收起时锚在侧栏底栏上方） */
  function anchorPanel() {
    const panel = $("music-panel");
    const dock = $("music-dock");
    const anchorEl = !dock.hidden ? dock : document.querySelector(".side-bottom");
    if (!anchorEl) return;
    const r = anchorEl.getBoundingClientRect();
    panel.style.left = Math.round(r.left) + "px";
    panel.style.bottom = Math.round(window.innerHeight - r.top + 8) + "px";
  }

  function openMusicPanel() {
    anchorPanel();
    $("music-panel").hidden = false;
    ensurePlaylistTab("hot", 3778678);   // 默认拉热歌榜
    const onTab = document.querySelector("#music-panel .mp-tabs > button.on");
    if (onTab && onTab.dataset.tab === "queue") renderQueue();
  }
  function closeMusicPanel() { $("music-panel").hidden = true; }

  function showDock(show) {
    $("music-dock").hidden = !show;
    if (!show) closeMusicPanel();
    savePref();
  }

  /* ---- 事件绑定（元素都在主界面 index.html 里）---- */
  if (!DESKTOP_PAGE) return;   // loading 页等无播放器结构: 不绑定

  /* 上次亮着播放条 → 这次直接恢复 */
  if (pref.dock) $("music-dock").hidden = false;

  $("btn-music").onclick = () => {
    if ($("music-dock").hidden) {          // 第一次: 亮出停靠播放条
      showDock(true);
    } else if ($("music-panel").hidden) {
      openMusicPanel();
    } else {
      closeMusicPanel();
    }
  };
  $("music-hide").onclick = () => showDock(false);
  $("music-toggle").onclick = () => {
    if (!curSong()) { openMusicPanel(); return; }   // 还没选歌: 引导去面板
    const a = getAudio();
    if (a.paused) a.play().catch(() => {}); else a.pause();
  };
  $("music-next").onclick = () => musicNext(false);
  $("music-prev").onclick = musicPrev;
  $("music-list-btn").onclick = () => {
    if ($("music-panel").hidden) openMusicPanel();
    switchTab("queue");
  };
  $("music-close").onclick = closeMusicPanel;
  $("music-mode-btn").onclick = () => {
    mstate.mode = mstate.mode === "order" ? "one"
      : mstate.mode === "one" ? "shuffle" : "order";
    applyModeIcon();
    savePref();
    toastFn({ order: "顺序播放", one: "单曲循环", shuffle: "随机播放" }[mstate.mode]);
  };
  $("music-vol").value = pref.vol ?? 70;
  $("music-vol").oninput = (ev) => {
    getAudio().volume = ev.target.value / 100;
    savePref();
  };
  $$("#music-panel .mp-tabs > button[data-tab]").forEach(b => {
    b.onclick = () => {
      switchTab(b.dataset.tab);
      if (b.dataset.tab === "hot") ensurePlaylistTab("hot", 3778678);
      if (b.dataset.tab === "new") ensurePlaylistTab("new", 3779629);
    };
  });
  $("music-search-input").addEventListener("keydown", ev => {
    ev.stopPropagation();
    if (ev.key === "Enter") doSearch();
  });
  $("music-seek").onclick = (ev) => {
    if (!audio || !isFinite(audio.duration)) return;
    const rect = ev.currentTarget.getBoundingClientRect();
    audio.currentTime = (ev.clientX - rect.left) / rect.width * audio.duration;
  };
  /* 窗口缩放时面板跟着 dock 重新锚定 */
  window.addEventListener("resize", () => {
    if (!$("music-panel").hidden) anchorPanel();
  });
  /* 点面板外收起（播放条按钮除外） */
  document.addEventListener("mousedown", (ev) => {
    if ($("music-panel").hidden) return;
    const panel = $("music-panel");
    const trigger = $("btn-music");
    const dock = $("music-dock");
    if (!panel.contains(ev.target)
        && !(trigger && trigger.contains(ev.target))
        && !(dock && dock.contains(ev.target))) {
      closeMusicPanel();
    }
  });

  /* mediaSession 硬件键（耳机切歌等） */
  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.setActionHandler("play", () => getAudio().play().catch(() => {}));
      navigator.mediaSession.setActionHandler("pause", () => getAudio().pause());
      navigator.mediaSession.setActionHandler("previoustrack", musicPrev);
      navigator.mediaSession.setActionHandler("nexttrack", () => musicNext(false));
    } catch { /* 部分平台不支持 */ }
  }

  applyModeIcon();

  function applyModeIcon() {
    const btn = $("music-mode-btn");
    btn.querySelector(".ic-order").style.display = mstate.mode === "order" ? "" : "none";
    btn.querySelector(".ic-one").style.display = mstate.mode === "one" ? "" : "none";
    btn.querySelector(".ic-shuffle").style.display = mstate.mode === "shuffle" ? "" : "none";
  }
})();
