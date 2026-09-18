// x-code 桌面壳:
//   - 端口 8000 上已有 x-code 服务在跑 → 直接复用, 不拉进程、退出时不杀
//   - 否则拉起 .venv 里的 python server.py 作为子进程, 退出时整树杀掉
//   - 窗口只加载本地服务; 外部链接一律转交系统浏览器, 防止窗口被带跑
const { app, BrowserWindow, shell, dialog, Menu, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

const BASE_URL = "http://127.0.0.1:8000";
const ROOT = path.join(__dirname, "..");

let win = null;
let serverProc = null;   // 本进程拉起的 Python 后端; null = 复用了外部已运行的服务
let quitting = false;

// 探测后端是否就绪（就绪 = /api/settings 返回 200）
function probeServer(timeoutMs) {
  return new Promise((resolve) => {
    const req = http.get(`${BASE_URL}/api/settings`, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

function startServer() {
  if (app.isPackaged) {
    // 打包态: 后端是 PyInstaller 冻结的单文件 exe, 随安装包放在资源目录;
    // cwd 指到用户目录 —— .env 由后端从 ~/.x-code 读取, 会话/配置也在那里
    const exe = path.join(process.resourcesPath, "server", "x-code-server.exe");
    const dataDir = path.join(app.getPath("home"), ".x-code");
    fs.mkdirSync(dataDir, { recursive: true });
    const proc = spawn(exe, [], {
      cwd: dataDir,
      windowsHide: true,
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
  // 开发态: 优先用项目 venv
  const pyExe = path.join(ROOT, ".venv", "Scripts", "python.exe");
  const cmd = fs.existsSync(pyExe) ? pyExe : "python";
  const proc = spawn(cmd, [path.join(ROOT, "server.py")], {
    cwd: ROOT,
    windowsHide: true,
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
    if (await probeServer(800)) return true;
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
  return url === BASE_URL || url.startsWith(BASE_URL + "/");
}

async function createWindow() {
  // 端口已被占用（用户手动起的服务/另一个实例）→ 复用, 不再拉自己的后端
  if (!(await probeServer(1200))) {
    serverProc = startServer();
    if (!(await waitServer(30000))) {
      dialog.showErrorBox(
        "x-code 启动失败",
        "Python 后端在 30 秒内未能就绪（端口 8000）。\n请检查 .venv 环境与 .env 配置后重试。"
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
  win.loadURL(BASE_URL);

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
