// x-code 桌面壳:
//   - 端口 8000 上已有 x-code 服务在跑 → 直接复用, 不拉进程、退出时不杀
//   - 否则拉起 .venv 里的 python server.py 作为子进程, 退出时整树杀掉
//   - 窗口只加载本地服务; 外部链接一律转交系统浏览器, 防止窗口被带跑
const { app, BrowserWindow, shell, dialog, Menu, ipcMain, session } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const http = require("http");
const crypto = require("crypto");


const ROOT = path.join(__dirname, "..");

// 连接门禁令牌: 与后端共享 ~/.x-code/token, 桌面壳的所有请求自动携带,
// 浏览器直接访问 127.0.0.1:8000 会因缺少令牌被后端 403 拒绝
const TOKEN_FILE = path.join(os.homedir(), ".x-code", "token");
function ensureToken() {
  fs.mkdirSync(path.dirname(TOKEN_FILE), { recursive: true });
  try {
    const t = fs.readFileSync(TOKEN_FILE, "utf-8").trim();
    if (t) return t;
  } catch (e) { /* 不存在: 生成 */ }
  const t = crypto.randomBytes(32).toString("hex");
  fs.writeFileSync(TOKEN_FILE, t);
  return t;
}
const API_TOKEN = ensureToken();

// 后端端口: 默认 8000, 被 C-Lodop 等程序占用时后端自动避让（8010–8019）,
// 实际端口写在 ~/.x-code/port, 这里动态读取
const PORT_FILE = path.join(os.homedir(), ".x-code", "port");
function readPort() {
  try {
    const p = parseInt(fs.readFileSync(PORT_FILE, "utf-8").trim(), 10);
    if (p > 0 && p < 65536) return p;
  } catch (e) { /* 文件不存在: 默认 8000 */ }
  return 8000;
}
let basePort = readPort();
const baseUrl = () => `http://127.0.0.1:${basePort}`;

let win = null;
let serverProc = null;   // 本进程拉起的 Python 后端; null = 复用了外部已运行的服务
let quitting = false;

// 探测后端是否就绪（就绪 = /api/ping 返回 200 且 app 标识为 x-code; 携带门禁令牌）。
// 不能只看 200: 8000 可能被 C-Lodop 打印服务等程序抢占, 它们对任何路径都回自己的页面
function pingServer(port, timeoutMs) {
  return new Promise((resolve) => {
    const req = http.get(`${baseUrl(port)}/api/ping`, {
      timeout: timeoutMs,
      headers: { "x-xcode-token": API_TOKEN },
    }, (res) => {
      let body = "";
      res.on("data", (d) => { body += d; });
      res.on("end", () => {
        try {
          resolve(res.statusCode === 200 && JSON.parse(body).app === "x-code");
        } catch (e) { resolve(false); }
      });
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

function startServer() {
  if (app.isPackaged) {
    // 打包态: 后端是 PyInstaller 冻结的单文件二进制, 随安装包放在资源目录
    // （Windows: x-code-server.exe / macOS·Linux: x-code-server）;
    // cwd 指到用户目录 —— .env 由后端从 ~/.x-code 读取, 会话/配置也在那里
    const exe = path.join(process.resourcesPath, "server",
      process.platform === "win32" ? "x-code-server.exe" : "x-code-server");
    const dataDir = path.join(app.getPath("home"), ".x-code");
    fs.mkdirSync(dataDir, { recursive: true });
    const proc = spawn(exe, ["--parent-pid", String(process.pid)], {
      cwd: dataDir,
      windowsHide: true,
      detached: process.platform !== "win32",   // posix: 独立进程组, 退出时整组杀
      stdio: ["ignore", "pipe", "pipe"],
    });
    proc.stdout.on("data", (d) => process.stdout.write(`[server] ${d}`));
    proc.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));
    proc.on("exit", (code) => {
      if (serverProc === proc) serverProc = null;
      if (!quitting && win && !win.isDestroyed()) {
        dialog.showErrorBox("x-code 后端已退出", `后端进程退出（code=${code}）。`);
      }
    });
    return proc;
  }
  // 开发态: 优先用项目 venv（Windows: Scripts/python.exe, posix: bin/python）
  const pyExe = process.platform === "win32"
    ? path.join(ROOT, ".venv", "Scripts", "python.exe")
    : path.join(ROOT, ".venv", "bin", "python");
  const cmd = fs.existsSync(pyExe) ? pyExe : "python";
  const proc = spawn(cmd, [path.join(ROOT, "server.py"), "--parent-pid", String(process.pid)], {
    cwd: ROOT,
    windowsHide: true,
    detached: process.platform !== "win32",
    stdio: ["ignore", "pipe", "pipe"],
  });
  proc.stdout.on("data", (d) => process.stdout.write(`[server] ${d}`));
  proc.stderr.on("data", (d) => process.stderr.write(`[server] ${d}`));
  proc.on("exit", (code) => {
    if (serverProc === proc) serverProc = null;
    // 非退出阶段后端自己挂了: 弹窗告知, 不静默
    if (!quitting && win && !win.isDestroyed()) {
      dialog.showErrorBox("x-code 后端已退出", `server.py 进程退出（code=${code}）。`);
    }
  });
  return proc;
}

async function waitServer(timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    basePort = readPort();   // 后端避让后会把实际端口写进 port 文件, 每轮重读
    if (await pingServer(basePort, 800)) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

function killServer() {
  if (!serverProc) return;
  const pid = serverProc.pid;
  serverProc = null;
  // 工具执行（bash 等）会产生 python 的子进程, Windows 上必须整树杀
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(pid), "/T", "/F"], { windowsHide: true });
  } else {
    try { process.kill(-pid); } catch { try { process.kill(pid); } catch {} }
  }
}

function isLocal(url) {
  return url === baseUrl() || url.startsWith(baseUrl() + "/");
}

async function createWindow() {
  // 端口已被占用（用户手动起的服务/另一个实例）→ 复用, 不再拉自己的后端
  basePort = readPort();   // 恢复上次会话的实际端口（可能已避让到 8010 等）
  if (!(await pingServer(basePort, 1200))) {
    serverProc = startServer();
    if (!(await waitServer(30000))) {
      dialog.showErrorBox(
        "x-code 启动失败",
        "Python 后端在 30 秒内未能就绪。\n若 8000–8019 端口被其他程序（如 C-Lodop 打印服务）占用, 请关闭后重试。"
      );
      app.quit();
      return;
    }
  }

  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    backgroundColor: "#101014",   // 与前端深色主题一致, 避免启动闪白
    autoHideMenuBar: true,
    title: "x-code",
    icon: path.join(ROOT, "static", "icon.png"),   // 窗口/任务栏图标（打包成 exe 需另配 .ico）
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  win.once("ready-to-show", () => win.show());
  // 门禁: 本会话的所有请求（页面/静态/API/WS 握手）自动携带令牌
  win.webContents.session.webRequest.onBeforeSendHeaders((details, cb) => {
    details.requestHeaders["x-xcode-token"] = API_TOKEN;
    cb({ requestHeaders: details.requestHeaders });
  });
  win.loadURL(baseUrl() + "/?token=" + encodeURIComponent(API_TOKEN));

  // 右键菜单: Electron 默认没有, 手动提供（选中即可复制; 输入框里可全选）
  win.webContents.on("context-menu", (ev, params) => {
    const menu = Menu.buildFromTemplate([
      { label: "复制", role: "copy", enabled: params.editFlags.canCopy },
      { type: "separator" },
      { label: "全选", role: "selectAll" },
    ]);
    menu.popup({ window: win });
  });

  // 外部链接（markdown 链接等）交给系统浏览器打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!isLocal(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (ev, url) => {
    if (!isLocal(url)) {
      ev.preventDefault();
      shell.openExternal(url);
    }
  });

  win.on("closed", () => { win = null; });
}

// 单实例: 二次启动只把已有窗口带到前台
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  // 系统原生"选择文件夹"对话框（渲染层经 preload 桥调用）
  ipcMain.handle("pick-folder", async () => {
    const opts = { title: "选择文件夹", properties: ["openDirectory"] };
    const res = win ? await dialog.showOpenDialog(win, opts)
                    : await dialog.showOpenDialog(opts);
    return res.canceled ? null : res.filePaths[0];
  });
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
  app.whenReady().then(createWindow);
  app.on("window-all-closed", () => {
    quitting = true;
    app.quit();
  });
  app.on("before-quit", () => {
    quitting = true;
    killServer();
  });
}
