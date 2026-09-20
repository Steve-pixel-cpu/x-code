from pathlib import Path

# ---------- app.js ----------
p = Path("static/app.js")
t = p.read_text(encoding="utf-8")

old = '''/* ---------- 设置 → 外观: 背景图片（亚克力磨砂的"壁纸"） ----------
 * 与应用图标同模式: POST /api/bg 落盘 static/bg-user.png, localStorage 只存
 * 版本号。应用分两半: CSS 令牌（applyBgImg, 启动即调）与 <img> 预加载
 * （syncBgLayers, 加载完再显示, 避免壁纸解码期间白屏闪烁）。 */
const BG_KEY = "xc-bg";
const bgUrl = () => "/static/bg-user.png" + (bgVer ? `?v=${bgVer}` : "");
let bgVer = 0;
function bgPref() { return localStorage.getItem(BG_KEY) === "1"; }

function applyBgImg() {
  const on = bgPref() && bgVer > 0;
  document.body.classList.toggle("has-bg", on);
  document.body.style.backgroundImage = on ? `url("${bgUrl()}")` : "";
  document.body.style.backgroundSize = "cover";
  document.body.style.backgroundPosition = "center";
}
/* 预加载壁纸: 加载成功才显示 bg-layer / 更新 body 背景。属性挂 body 上,
 * #bg-layer 用 CSS var(bg-image) 引用, 换图只改一处。 */
function syncBgLayers() {
  const on = bgPref() && bgVer > 0;
  document.body.classList.toggle("has-bg", on);
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
syncBgLayers();   // 启动装载: 上次会话的壁纸（有本地标记才发请求）
'''
new = '''/* ---------- 设置 → 外观: 背景图片（亚克力磨砂的"壁纸"） ----------
 * 与应用图标同模式: POST /api/bg 落盘 static/bg-user.png, localStorage 只存
 * 启用标记（xc-bg=1）。应用方式: <html data-bg="1"> 让遮罩/半透明令牌生效
 * （预绘制脚本抢在首帧前设置, 避免闪烁）; 壁纸本体由 syncBgLayers 预加载
 * 成功后再写到 body 内联背景上, 避免解码期间半成品闪烁。 */
const BG_KEY = "xc-bg";
const bgUrl = () => "/static/bg-user.png" + (bgVer ? `?v=${bgVer}` : "");
let bgVer = 0;
function bgPref() { return localStorage.getItem(BG_KEY) === "1"; }

function syncBgLayers() {
  const on = bgPref() && bgVer > 0;
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
'''
assert t.count(old) == 1, "bg js block"
t = t.replace(old, new, 1)

# loadSettings 接 bg_ver: 服务端有壁纸且本地标记开启才装载
old = '''    if (s.icon_ver) { iconVer = s.icon_ver; applyIconEverywhere(iconUrl()); }'''
new = '''    if (s.icon_ver) { iconVer = s.icon_ver; applyIconEverywhere(iconUrl()); }
    if (s.bg_ver) { bgVer = s.bg_ver; syncBgLayers(); }
    else syncBgLayers();   // 服务端无壁纸: 走一遍以清掉本地残留标记的效果'''
assert t.count(old) == 1, "loadSettings bg_ver"
t = t.replace(old, new, 1)

# 上传/清除改用 data-bg 标记（原 classList）
old = '''      bgVer = (await r.json()).ver;
      localStorage.setItem(BG_KEY, "1");   // 上传即启用
      syncBgLayers();'''
assert t.count(old) == 1   # syncBgLayers 内部已切 data-bg, 无需额外改动

# 应用图标行后面补一句: 上传背景后 openSettings 无需处理（syncBgLayers 即时生效）
p.write_text(t, encoding="utf-8")
print("js fixed")

# ---------- index.html ----------
p = Path("static/index.html")
t = p.read_text(encoding="utf-8")
old = '''<script>/* 主题/材质/壁纸在绘制前应用，避免闪白 (dark | light | system | acrylic | acrylic-light) */ (function(){var p=localStorage.getItem("xc-theme")||"system";var acrylic=(p==="acrylic"||p==="acrylic-light");var d=p==="dark"||p==="acrylic"||(p==="system"&&window.matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.dataset.theme=acrylic?(p==="acrylic"?"dark":"light"):(d?"dark":"light");if(acrylic)document.documentElement.dataset.fx="acrylic";var a=localStorage.getItem("xc-accent");if(a&&a!=="gray")document.documentElement.dataset.accent=a;if(localStorage.getItem("xc-bg")==="1")document.body.classList.add("has-bg");})();</script>'''
new = '''<script>/* 主题/材质/壁纸在绘制前应用，避免闪白 (dark | light | system | acrylic | acrylic-light) */ (function(){var de=document.documentElement,p=localStorage.getItem("xc-theme")||"system",acrylic=(p==="acrylic"||p==="acrylic-light");de.dataset.theme=acrylic?(p==="acrylic"?"dark":"light"):(p==="system"&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");if(acrylic)de.dataset.fx="acrylic";var a=localStorage.getItem("xc-accent");if(a&&a!=="gray")de.dataset.accent=a;if(localStorage.getItem("xc-bg")==="1")de.dataset.bg="1";})();</script>'''
assert t.count(old) == 1, "preload script v2"
t = t.replace(old, new, 1)
p.write_text(t, encoding="utf-8")
print("html fixed")

# ---------- app.css: has-bg 选择器统一到 :root[data-bg] ----------
p = Path("static/app.css")
t = p.read_text(encoding="utf-8")
old = '''  /* 普通主题 + 壁纸（无磨砂）: 大面积区域转半透明让图片可见, 卡片保持
   * 实底保证可读性; 遮罩比亚克力下重一档（没有模糊帮文字让路）。
   * 注意 data-theme / data-bg 都挂在 <html> 上, body 级规则须带 :root 前缀;
   * 变量写在 body 上, #bg-scrim 作为 body 子元素继承之 */
  body.has-bg #sidebar { background: color-mix(in srgb, var(--bg-side) 88%, transparent); }
  body.has-bg #pane { background: color-mix(in srgb, var(--bg) 84%, transparent); }
  body.has-bg #titlebar { background: color-mix(in srgb, var(--bg-side) 88%, transparent); }
  body.has-bg #doc-header { background: color-mix(in srgb, var(--bg) 86%, transparent); }
  body.has-bg .composer-shell { background: transparent; }
  body.has-bg { --bg-scrim: rgba(10, 10, 14, .52); }              /* 普通深色: 无模糊, 遮罩重 */
  :root[data-theme="light"] body.has-bg { --bg-scrim: rgba(255, 255, 255, .46); }
  :root[data-fx="acrylic"] body.has-bg { --bg-scrim: rgba(10, 10, 14, .34); }          /* 亚克力: 磨砂已柔化 */
  :root[data-theme="light"][data-fx="acrylic"] body.has-bg { --bg-scrim: rgba(255, 255, 255, .30); }

  /* 壁纸遮罩与磨砂层: z-index -1 = 壁纸(body 绘制)之上、一切 UI 之下 */
  #bg-scrim, #bg-frost {
    position: fixed; inset: 0; z-index: -1; display: none; pointer-events: none;
  }
  body.has-bg #bg-scrim { display: block; background: var(--bg-scrim); }
  #bg-frost { backdrop-filter: blur(46px) saturate(1.4); }
  :root[data-fx="acrylic"] #bg-frost { display: block; }'''
new = '''  /* 壁纸启用标记 data-bg="1" 挂在 <html> 上（head 预绘制脚本可设）。
   * 普通主题 + 壁纸: 大面积区域转半透明让图片可见, 卡片保持实底保证
   * 可读性; 用 :not([data-fx=acrylic]) 与亚克力令牌互斥, 避免特异性打架 */
  :root[data-bg="1"]:not([data-fx="acrylic"]) #sidebar { background: color-mix(in srgb, var(--bg-side) 88%, transparent); }
  :root[data-bg="1"]:not([data-fx="acrylic"]) #pane { background: color-mix(in srgb, var(--bg) 84%, transparent); }
  :root[data-bg="1"]:not([data-fx="acrylic"]) #titlebar { background: color-mix(in srgb, var(--bg-side) 88%, transparent); }
  :root[data-bg="1"]:not([data-fx="acrylic"]) #doc-header { background: color-mix(in srgb, var(--bg) 86%, transparent); }
  :root[data-bg="1"] .composer-shell { background: transparent; }
  /* 遮罩强度: 亚克力下轻一档（磨砂已柔化壁纸）; 无模糊时遮罩重一点帮文字让路 */
  :root[data-bg="1"] { --bg-scrim: rgba(10, 10, 14, .52); }
  :root[data-theme="light"][data-bg="1"] { --bg-scrim: rgba(255, 255, 255, .46); }
  :root[data-fx="acrylic"][data-bg="1"] { --bg-scrim: rgba(10, 10, 14, .34); }
  :root[data-theme="light"][data-fx="acrylic"][data-bg="1"] { --bg-scrim: rgba(255, 255, 255, .30); }

  /* 壁纸遮罩与磨砂层: z-index -1 = 壁纸(body 绘制)之上、一切 UI 之下;
   * 变量从 <html> 继承, 元素本体只管显隐与着色 */
  #bg-scrim, #bg-frost {
    position: fixed; inset: 0; z-index: -1; display: none; pointer-events: none;
  }
  :root[data-bg="1"] #bg-scrim { display: block; background: var(--bg-scrim); }
  #bg-frost { backdrop-filter: blur(46px) saturate(1.4); }
  :root[data-fx="acrylic"] #bg-frost { display: block; }'''
assert t.count(old) == 1, "css has-bg block"
t = t.replace(old, new, 1)
p.write_text(t, encoding="utf-8")
print("css fixed")
