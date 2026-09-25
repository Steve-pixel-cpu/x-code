"use strict";
/* ============================================================
 * 摸鱼电台: 网易云迷你播放器（独立文件, 不碰 app.js 的会话逻辑）
 * 播放条停靠在侧栏底部空白区（#side-chat 内, 会话列表下方）,
 * 不与主区输入框重叠; 面板从其上方弹出。
 * 找歌只有搜索; 收藏与自定义歌单存服务端 ~/.x-code/music-library.json,
 * 当前播放列表（队列）存 localStorage —— 重启后接着听。
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
    qname: "",            // 队列来源名（「收藏」/「我的歌单: xx」/「搜索: xx」）
    index: -1,            // 当前歌在队列里的下标
    mode: "order",        // order | one | shuffle
    playing: false,
    failedStreak: 0,      // 连续跳过计数: 防"全队列不可播"死循环
    library: { favorites: [], playlists: [] },   // 本地曲库, 写操作后整体刷新
    mineView: null,       // 我的歌单 Tab: null=歌单列表; 数字=正在看的歌单 id
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
        queue: mstate.queue, qname: mstate.qname, index: mstate.index,
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
    savePref();   // 记住听到哪首, 重启恢复
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

  /* ---- 本地曲库 API（收藏 / 我的歌单）---- */
  async function libFetch(url, opts) {
    const r = await fetch(url, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  }

  async function refreshLibrary() {
    mstate.library = await libFetch("/api/music/library");
  }

  function isFav(songId) {
    return mstate.library.favorites.some(s => s.id === songId);
  }

  async function toggleFav(s) {
    try {
      if (isFav(s.id)) {
        await libFetch(`/api/music/favorites/${s.id}`, { method: "DELETE" });
        toastFn(`已取消收藏「${s.name}」`);
      } else {
        await libFetch("/api/music/favorites", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ songs: [s] }),
        });
        toastFn(`已收藏「${s.name}」`);
      }
      await refreshLibrary();
      refreshFavHearts();
      if (!$("music-panel").hidden) {
        const onTab = document.querySelector("#music-panel .mp-tabs > button.on");
        if (onTab && onTab.dataset.tab === "fav") renderFavTab();
      }
    } catch (e) {
      toastFn("操作失败: " + (e.message || e));
    }
  }

  /* 曲库变化后, 页面上所有行的心形跟着刷新（不动列表结构） */
  function refreshFavHearts() {
    $$("#music-panel .mp-song").forEach(row => {
      const btn = row.querySelector(".mp-s-fav");
      if (btn) btn.classList.toggle("on", isFav(Number(row.dataset.songId)));
    });
  }

  /* 把歌追加进当前播放列表（不打断正在放的歌, 也不动当前下标） */
  function appendQueue(songs, label) {
    const known = new Set(mstate.queue.map(s => s.id));
    let n = 0;
    songs.forEach(s => {
      if (!known.has(s.id)) { mstate.queue.push(s); known.add(s.id); n++; }
    });
    savePref();
    if (!n) {
      toastFn(`${label}都已在播放列表里`);
      return;
    }
    toastFn(songs.length === 1
      ? `已把${label}加入播放列表`
      : `已把${label}的 ${n} 首加入播放列表`);
    if (!$("music-panel").hidden) {
      const onTab = document.querySelector("#music-panel .mp-tabs > button.on");
      if (onTab && onTab.dataset.tab === "queue") renderQueue();
    }
  }

  /* 浏览面（搜索结果）点歌: 已在队列就跳过去播, 否则插播到当前歌后面。
     队列是唯一的播放真相源 —— 搜索只浏览, 绝不整表替换队列
     （老行为把搜索结果整个灌进播放列表, 就是「点＋却加了一页」的根源）。 */
  function playFromBrowse(s) {
    const at = mstate.queue.findIndex(q => q.id === s.id);
    if (at >= 0) { playIndex(at); return; }
    const insertAt = mstate.index >= 0 ? mstate.index + 1 : mstate.queue.length;
    mstate.queue.splice(insertAt, 0, s);
    savePref();
    playIndex(insertAt);
  }

  /* ---- 「添加到…」菜单: 行上的 ＋ 弹出, 可加入 播放列表 / 任意歌单 / 新歌单 ---- */
  function closeAddMenu() {
    const m = $("music-add-menu");
    if (m) m.remove();
    document.removeEventListener("mousedown", onAddMenuAway, true);
    document.removeEventListener("keydown", onAddMenuEsc, true);
  }
  function onAddMenuAway(e) {
    if (!e.target.closest || !e.target.closest("#music-add-menu")) closeAddMenu();
  }
  function onAddMenuEsc(e) { if (e.key === "Escape") closeAddMenu(); }

  async function addToPlaylist(pl, songs, label) {
    try {
      const r = await libFetch(`/api/music/playlists/${pl.id}/songs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ songs }),
      });
      await refreshLibrary();
      toastFn(r.added
        ? `已把${label}加入「${r.name}」（新增 ${r.added} 首）`
        : `「${r.name}」里已经有这些歌了`);
    } catch (e) {
      toastFn("添加失败: " + (e.message || e));
    }
  }

  function openAddMenu(anchor, songs, label) {
    if (!anchor || !anchor.isConnected) return;   // 按钮已不在 DOM（重渲染过）就不弹
    closeAddMenu();
    const items = [];
    if (!songs.every(s => mstate.queue.some(q => q.id === s.id))) {
      items.push({ text: "播放列表", hint: "追加到当前队列",
        act: () => appendQueue(songs, label) });
    }
    (mstate.library.playlists || []).forEach(pl => {
      items.push({ text: pl.name, hint: pl.songs.length + " 首",
        act: () => addToPlaylist(pl, songs, label) });
    });
    items.push({ text: "＋ 新建歌单…", hint: "", act: async () => {
      const name = await promptDialog(`把${label}存入新歌单`, {
        title: "新建歌单", placeholder: "歌单名称", okText: "创建",
      });
      if (!name) return;
      try {
        const pl = await libFetch("/api/music/playlists", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, songs }),
        });
        await refreshLibrary();
        toastFn(`已创建歌单「${pl.name}」（${pl.songs.length} 首）`);
      } catch (e) {
        toastFn("创建失败: " + (e.message || e));
      }
    }});

    const m = document.createElement("div");
    m.id = "music-add-menu";
    items.forEach(it => {
      const b = document.createElement("button");
      b.type = "button";
      b.innerHTML = '<span class="mpam-name"></span><span class="mpam-hint"></span>';
      b.querySelector(".mpam-name").textContent = it.text;
      b.querySelector(".mpam-hint").textContent = it.hint;
      b.onclick = () => { closeAddMenu(); it.act(); };
      m.appendChild(b);
    });
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    const mw = m.offsetWidth;
    const mh = m.offsetHeight;
    let left = Math.max(8, Math.min(r.left, window.innerWidth - mw - 8));
    let bottom = window.innerHeight - r.top + 6;      // 默认: 按钮上方
    if (r.top - mh - 12 < 0) {                        // 上方放不下 → 按钮下方
      bottom = Math.max(8, window.innerHeight - r.bottom - 6);
    }
    m.style.left = left + "px";
    m.style.bottom = bottom + "px";
    document.addEventListener("mousedown", onAddMenuAway, true);
    document.addEventListener("keydown", onAddMenuEsc, true);
  }

  /* ---- 歌曲行: ❤ 收藏 + ＋ 加入播放列表 ---- */
  function songRow(s, i, opts = {}) {
    const row = document.createElement("div");
    row.className = "mp-song";
    row.dataset.songId = s.id;
    const delBtn = opts.deletable
      ? '<button class="mp-s-del" data-tip="移出列表"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>'
      : "";
    row.innerHTML =
      '<span class="mp-s-eq"><i></i><i></i><i></i></span>' +
      `<span class="mp-s-idx">${i + 1}</span>` +
      '<span class="mp-s-main"><span class="mp-s-name"></span>' +
      '<span class="mp-s-artist"></span></span>' +
      (s.fee === 1 ? '<span class="mp-s-tag">VIP</span>' : "") +
      `<span class="mp-s-dur">${fmtTime(s.duration)}</span>` +
      `<button class="mp-s-add" data-tip="添加到…"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>` +
      `<button class="mp-s-fav${isFav(s.id) ? " on" : ""}" data-tip="收藏"><svg width="13" height="13" viewBox="0 0 24 24"><path d="M12 21s-7.5-4.9-10-9.6C.4 8 2 4.5 5.5 4.2 7.7 4 9.5 5.3 12 7.8c2.5-2.5 4.3-3.8 6.5-3.6C22 4.5 23.6 8 22 11.4 19.5 16.1 12 21 12 21z" fill="${isFav(s.id) ? "currentColor" : "none"}" stroke="currentColor" stroke-width="1.8"/></svg></button>` +
      delBtn;
    row.querySelector(".mp-s-name").textContent = s.name;
    row.querySelector(".mp-s-artist").textContent = s.artist;
    row.querySelector(".mp-s-fav").onclick = (ev) => {
      ev.stopPropagation();
      toggleFav(s);
    };
    row.querySelector(".mp-s-add").onclick = (ev) => {
      ev.stopPropagation();
      const btn = ev.currentTarget;   // currentTarget 只在派发期间有效, 异步前先抓下来
      refreshLibrary().catch(() => {}).then(() =>
        openAddMenu(btn, [s], "「" + s.name + "」"));
    };
    if (opts.deletable) {
      row.querySelector(".mp-s-del").onclick = (ev) => {
        ev.stopPropagation();
        opts.onDelete(s, i);
      };
    }
    return row;
  }

  /* 纯渲染列表。行点击/删除行为由 onPlay/onDelete 决定, 这里不碰队列
     （老版本在这里 replaceQueue 整表灌队列, 是「加一首变加一页」的根源）。 */
  function renderSongList(body, songs, { onPlay = null, deletable = false, onDelete = null } = {}) {
    if (!songs.length) {
      body.innerHTML = '<div class="mp-empty">没有结果</div>';
      return;
    }
    body.innerHTML = "";
    songs.forEach((s, i) => {
      const row = songRow(s, i, {
        deletable,
        onDelete: onDelete || ((song) => {
          /* 默认删除行为: 从当前队列移除 */
          const idx = mstate.queue.findIndex(q => q.id === song.id);
          if (idx >= 0) removeQueueAt(idx);
        }),
      });
      row.onclick = () => (onPlay ? onPlay(s, i) : playFromBrowse(s));
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
      renderSongList(body, data.songs);   // 纯浏览: 点行插播, 不动队列
    } catch (e) {
      body.innerHTML = `<div class="mp-empty">搜索失败: ${escapeText(e.message)}</div>`;
    }
  }

  /* ---- 播放列表 Tab ---- */
  function renderQueue() {
    const body = document.querySelector('#music-panel [data-tab-body="queue"]');
    if (!mstate.queue.length) {
      body.innerHTML = '<div class="mp-empty">播放列表是空的, 去搜索里点 ＋ 吧</div>';
      return;
    }
    body.innerHTML =
      '<div class="mp-queue-head"><span></span><span class="mp-queue-ops">' +
      '<button id="music-queue-save">存为歌单</button>' +
      '<button id="music-queue-clear">清空</button></span></div>';
    body.querySelector("span").textContent
      = `${mstate.qname || "播放列表"} · ${mstate.queue.length} 首`;
    body.querySelector("#music-queue-clear").onclick = () => {
      getAudio().pause();
      mstate.queue = [];
      mstate.index = -1;
      refreshBar();
      savePref();
      renderQueue();
    };
    body.querySelector("#music-queue-save").onclick = async () => {
      const name = await promptDialog("把当前播放列表保存为歌单", {
        title: "存为歌单", value: mstate.qname || "我的歌单", okText: "保存",
      });
      if (!name) return;   // 取消或空名
      try {
        const pl = await libFetch("/api/music/playlists", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, songs: mstate.queue }),
        });
        await refreshLibrary();
        toastFn(`已存为歌单「${pl.name}」（${pl.songs.length} 首）`);
      } catch (e) {
        toastFn("保存失败: " + (e.message || e));
      }
    };
    const list = document.createElement("div");
    mstate.queue.forEach((s, i) => {
      const row = songRow(s, i, {
        deletable: true,
        onDelete: () => removeQueueAt(i),
      });
      if (i === mstate.index) row.classList.add("playing");
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
    savePref();
    renderQueue();
  }

  /* ---- 收藏 Tab ---- */
  function renderFavTab() {
    const body = document.querySelector('#music-panel [data-tab-body="fav"]');
    const favs = mstate.library.favorites || [];
    if (!favs.length) {
      body.innerHTML = '<div class="mp-empty">还没有收藏, 去搜索里点 ♡ 吧</div>';
      return;
    }
    body.innerHTML =
      '<div class="mp-queue-head"><span></span><span class="mp-queue-ops">' +
      '<button id="music-fav-playall">播放全部</button></span></div>';
    body.querySelector("span").textContent = `收藏 · ${favs.length} 首`;
    body.querySelector("#music-fav-playall").onclick = () => {
      mstate.queue = favs.slice();
      mstate.qname = "收藏";
      mstate.index = -1;
      savePref();
      playIndex(0);
    };
    const list = document.createElement("div");
    favs.forEach((s, i) => {
      const row = songRow(s, i, {
        deletable: true,
        onDelete: async (song) => {
          try {
            await libFetch(`/api/music/favorites/${song.id}`, { method: "DELETE" });
            await refreshLibrary();
            renderFavTab();
          } catch (e) {
            toastFn("移除失败: " + (e.message || e));
          }
        },
      });
      row.onclick = () => {
        mstate.queue = favs.slice();   // 整个收藏作为队列, 可上下切换
        mstate.qname = "收藏";
        savePref();
        playIndex(i);
      };
      list.appendChild(row);
    });
    body.appendChild(list);
  }

  /* ---- 我的歌单 Tab: null=列表视图, 数字=歌单详情视图 ---- */
  function renderMineTab() {
    const body = document.querySelector('#music-panel [data-tab-body="mine"]');
    if (mstate.mineView === null) { renderMineList(body); return; }

    const pl = (mstate.library.playlists || []).find(p => p.id === mstate.mineView);
    if (!pl) { mstate.mineView = null; renderMineList(body); return; }

    body.innerHTML =
      '<div class="mp-queue-head mp-pl-detail-head">' +
      '<button id="music-pl-back">‹ 歌单</button><span></span>' +
      '<span class="mp-queue-ops">' +
      '<button id="music-pl-append">并入播放列表</button>' +
      '<button id="music-pl-del">删除歌单</button></span></div>';
    body.querySelector("span").textContent = `${pl.name} · ${pl.songs.length} 首`;
    body.querySelector("#music-pl-back").onclick = () => {
      mstate.mineView = null;
      renderMineTab();
    };
    body.querySelector("#music-pl-append").onclick = () => {
      if (!pl.songs.length) { toastFn("歌单是空的"); return; }
      appendQueue(pl.songs, `歌单「${pl.name}」`);
    };
    body.querySelector("#music-pl-del").onclick = async () => {
      if (!(await confirmDialog(`删除歌单「${pl.name}」?`, { title: "删除歌单", okText: "删除", danger: true }))) return;
      try {
        await libFetch(`/api/music/playlists/${pl.id}`, { method: "DELETE" });
        mstate.mineView = null;
        await refreshLibrary();
        toastFn(`已删除歌单「${pl.name}」`);
        renderMineTab();
      } catch (e) {
        toastFn("删除失败: " + (e.message || e));
      }
    };
    if (!pl.songs.length) {
      const empty = document.createElement("div");
      empty.className = "mp-empty";
      empty.textContent = "歌单是空的, 去搜索里点 ＋ 后从播放列表存过来";
      body.appendChild(empty);
      return;
    }
    const list = document.createElement("div");
    pl.songs.forEach((s, i) => {
      const row = songRow(s, i, {
        deletable: true,
        onDelete: async (song) => {
          try {
            await libFetch(`/api/music/playlists/${pl.id}/songs/${song.id}`, { method: "DELETE" });
            await refreshLibrary();
            renderMineTab();
          } catch (e) {
            toastFn("移出失败: " + (e.message || e));
          }
        },
      });
      row.onclick = () => {
        mstate.queue = pl.songs.slice();   // 整个歌单作为队列
        mstate.qname = "我的歌单: " + pl.name;
        savePref();
        playIndex(i);
      };
      list.appendChild(row);
    });
    body.appendChild(list);
  }

  function renderMineList(body) {
    const pls = mstate.library.playlists || [];
    body.innerHTML =
      '<div class="mp-pl-new"><input id="music-pl-name" type="text" ' +
      'placeholder="新歌单名称，回车创建" spellcheck="false" autocomplete="off"></div>';
    const input = body.querySelector("#music-pl-name");
    input.addEventListener("keydown", async (ev) => {
      ev.stopPropagation();
      if (ev.key !== "Enter") return;
      const name = input.value.trim();
      if (!name) return;
      try {
        const pl = await libFetch("/api/music/playlists", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        await refreshLibrary();
        toastFn(`已创建歌单「${pl.name}」`);
        renderMineList(body);
      } catch (e) {
        toastFn("创建失败: " + (e.message || e));
      }
    });

    if (!pls.length) {
      const empty = document.createElement("div");
      empty.className = "mp-empty";
      empty.textContent = "还没有歌单, 上面输入名称创建";
      body.appendChild(empty);
      return;
    }
    const list = document.createElement("div");
    pls.forEach((pl) => {
      const row = document.createElement("div");
      row.className = "mp-pl";
      row.innerHTML =
        '<span class="mp-pl-name"></span>' +
        '<span class="mp-pl-meta"></span>' +
        '<span class="mp-queue-ops">' +
        '<button class="mp-pl-play" data-tip="播放"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button>' +
        '<button class="mp-pl-rename" data-tip="重命名"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4L16.5 3.5z"/></svg></button>' +
        '<button class="mp-pl-del" data-tip="删除歌单"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M8 6V4h8v2m1 0v14a1 1 0 01-1 1H8a1 1 0 01-1-1V6"/></svg></button>' +
        '</span>';
      row.querySelector(".mp-pl-name").textContent = pl.name;
      row.querySelector(".mp-pl-meta").textContent = pl.songs.length + " 首";
      row.onclick = () => {           // 点名称进详情
        mstate.mineView = pl.id;
        renderMineTab();
      };
      row.querySelector(".mp-pl-play").onclick = (ev) => {
        ev.stopPropagation();
        if (!pl.songs.length) { toastFn("歌单是空的"); return; }
        mstate.queue = pl.songs.slice();
        mstate.qname = "我的歌单: " + pl.name;
        mstate.index = -1;
        savePref();
        playIndex(0);
      };
      row.querySelector(".mp-pl-rename").onclick = async (ev) => {
        ev.stopPropagation();
        const name = await promptDialog("重命名歌单", {
          title: "重命名", value: pl.name, okText: "确定",
        });
        if (!name) return;
        try {
          await libFetch(`/api/music/playlists/${pl.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
          });
          await refreshLibrary();
          renderMineList(body);
        } catch (e) {
          toastFn("重命名失败: " + (e.message || e));
        }
      };
      row.querySelector(".mp-pl-del").onclick = async (ev) => {
        ev.stopPropagation();
        if (!(await confirmDialog(`删除歌单「${pl.name}」?`, { title: "删除歌单", okText: "删除", danger: true }))) return;
        try {
          await libFetch(`/api/music/playlists/${pl.id}`, { method: "DELETE" });
          await refreshLibrary();
          toastFn(`已删除歌单「${pl.name}」`);
          renderMineList(body);
        } catch (e) {
          toastFn("删除失败: " + (e.message || e));
        }
      };
      list.appendChild(row);
    });
    body.appendChild(list);
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
    if (key === "fav") renderFavTab();
    if (key === "mine") renderMineTab();
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

  async function openMusicPanel() {
    anchorPanel();
    $("music-panel").hidden = false;
    try { await refreshLibrary(); } catch { /* 离线也不挡着用搜索 */ }
    const onTab = document.querySelector("#music-panel .mp-tabs > button.on");
    if (onTab) switchTab(onTab.dataset.tab);   // 重渲染当前 Tab（收藏实心态等）
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
  /* 上次的队列/当前歌恢复回来（收藏和歌单在服务端, 不走这里） */
  if (Array.isArray(pref.queue) && pref.queue.length) {
    mstate.queue = pref.queue;
    mstate.qname = String(pref.qname || "");
    mstate.index = Number.isInteger(pref.index) ? pref.index : -1;
    if (mstate.index >= mstate.queue.length) mstate.index = -1;
    refreshBar();
  }
  /* 预拉曲库: ❤ 实心态和收藏 Tab 不等第一次开面板 */
  refreshLibrary().catch(() => {});

  $("btn-music").onclick = () => {
    // 二元开关: 有任何弹窗/播放条开着 → 一键全关; 全关 → 亮出播放条。
    // 旧的三态循环(面板只关自己、播放条永远留着)回不到干净状态。
    // showDock(false) 顺带关面板 + savePref, 重启后不再自动恢复。
    const allClosed = $("music-dock").hidden && $("music-panel").hidden;
    showDock(allClosed);
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
    b.onclick = () => switchTab(b.dataset.tab);
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
