// x-code 桌面壳（Tauri 版）, 对齐 electron/main.js 的全部行为:
//   1. 令牌: 与后端共享 ~/.x-code/token, 打开页面时经 ?token= 传入（index.html 里种成 cookie）
//   2. 端口: 开发态读 ~/.x-code/port（缺省 8000, 避让 8010–8019）; 发布态读
//      ~/.x-code/release-port（缺省 18080, 避让 18090–18099, 以 --port/--port-file
//      显式传给后端）——不与开发/测试后端常占的 8000 撞车, 两种形态可同时存活
//   3. 探活: GET /api/ping 须 200 且 body 含 "x-code"（8000 被 C-Lodop 等抢占时不能误判为就绪）
//   4. 复用: 已有 x-code 服务在跑 → 直接连, 不拉进程、退出时不杀
//   5. 拉起: 打包态用冻结后端（resources/server/x-code-server.exe, cwd=~/.x-code）,
//            开发态用 .venv 的 python server.py; 退出整树杀
//   6. 前端: 窗口加载本地服务; 注入 window.xcodeDesktop 标记 + xcodePickFolder 原生选文件夹桥;
//            外部链接转交系统浏览器
//   7. 诊断: 后端输出与壳侧启动步骤追加写 ~/.x-code/boot.log; 后端提前退出立即报错
//            （带退出码）而非干等超时; 首启探活窗 60s（杀软首扫 + onefile 解压很慢）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use tauri::window::Effect;
use tauri::utils::config::WindowEffectsConfig;
use tauri::{AppHandle, Manager, RunEvent, WindowEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

/// 本壳拉起的后端子进程; None = 复用外部已运行的服务（退出时不杀）
static CHILD: LazyLock<Mutex<Option<Child>>> = LazyLock::new(|| Mutex::new(None));

// ---------- ~/.x-code/{token,port} ----------

fn data_dir() -> PathBuf {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .expect("no home dir");
    home.join(".x-code")
}

fn read_trim(path: &PathBuf) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|s| s.trim().to_string())
}

/// 连接门禁令牌: 不存在则生成 64 位 hex（与 Electron 壳同规格, 双壳可共用同一后端）
fn ensure_token() -> String {
    let file = data_dir().join("token");
    if let Some(t) = read_trim(&file) {
        if !t.is_empty() {
            return t;
        }
    }
    let t = random_hex32();
    let _ = std::fs::create_dir_all(data_dir());
    let _ = std::fs::write(&file, &t);
    t
}

fn random_hex32() -> String {
    #[cfg(windows)]
    {
        use std::ffi::c_void;
        #[link(name = "bcrypt")]
        extern "system" {
            fn BCryptGenRandom(
                halgorithm: *mut c_void,
                pbbuffer: *mut u8,
                cbbuffer: u32,
                dwflags: u32,
            ) -> i32;
        }
        let mut buf = [0u8; 32];
        let ok = unsafe { BCryptGenRandom(std::ptr::null_mut(), buf.as_mut_ptr(), 32, 0x2) };
        if ok == 0 {
            return buf.iter().map(|b| format!("{b:02x}")).collect();
        }
    }
    // 兜底: 时间熵 + 进程号（几乎不会走到）
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("{t:032x}{:016x}deadbeefdeadbeef", std::process::id() as u128,)[..64].to_string()
}

/// 发布态判定: 后端 sidecar 随包分发在 <exe>\resources\server\ 下。
/// 发布态与开发态用不同的缺省端口/端口文件（见 port_file），互不抢占。
fn sidecar_exe() -> Option<PathBuf> {
    std::env::current_exe().ok().and_then(|exe| {
        let p = exe
            .parent()?
            .join("resources/server/x-code-server.exe");
        p.exists().then_some(p)
    })
}

fn release_mode() -> bool {
    sidecar_exe().is_some()
}

fn default_port() -> u16 {
    if release_mode() { 18080 } else { 8000 }
}

fn port_file() -> PathBuf {
    data_dir().join(if release_mode() { "release-port" } else { "port" })
}

fn read_port() -> u16 {
    read_trim(&port_file())
        .and_then(|s| s.parse::<u16>().ok())
        .filter(|p| *p > 0)
        .unwrap_or_else(default_port)
}

// ---------- 探活 ----------

/// GET /api/ping: 200 且 body 含 "x-code" 才算就绪。
/// 不能只看连接成功: 8000 可能被 C-Lodop 等服务抢占, 它们对任何路径都回自己的页面。
fn ping_server(port: u16, token: &str, timeout: Duration) -> bool {
    let Ok(stream) = TcpStream::connect(("127.0.0.1", port)) else {
        return false;
    };
    let mut stream = stream;
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    let req = format!(
        "GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nx-xcode-token: {token}\r\n\r\n"
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    let _ = stream.read_to_end(&mut buf);
    if buf.is_empty() {
        return false;
    }
    let text = String::from_utf8_lossy(&buf);
    let Some((head, body)) = text.split_once("\r\n\r\n") else {
        return false;
    };
    head.starts_with("HTTP/") && head.contains(" 200 ") && body.contains("x-code")
}

// ---------- 拉起后端 ----------

/// 启动诊断日志: 打包态没有控制台, 后端 stdout/stderr 此前只进 eprint（= 蒸发）,
/// 用户只能看到"30 秒未就绪"的兜底弹窗, 真实死因（杀软拦截/缺文件/端口占用）无从定位。
/// 壳侧关键步骤与后端输出都追加到 ~/.x-code/boot.log, 追加式以保留最近几次启动记录。
fn boot_log_path() -> PathBuf {
    data_dir().join("boot.log")
}

static BOOT_LOG: LazyLock<Mutex<Option<std::fs::File>>> = LazyLock::new(|| {
    let _ = std::fs::create_dir_all(data_dir());
    Mutex::new(
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(boot_log_path())
            .ok(),
    )
});

/// UTC 时间戳, 专供 boot.log。不引第三方时间库, civil 算法（Howard Hinnant）直接算。
fn utc_now_string() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    fmt_utc(secs)
}

fn fmt_utc(secs: u64) -> String {
    let (h, m, s) = ((secs / 3600) % 24, (secs % 3600) / 60, secs % 60);
    let z = (secs / 86400) as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let mut y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mth = if mp < 10 { mp + 3 } else { mp - 9 };
    if mth <= 2 {
        y += 1;
    }
    format!("{y:04}-{mth:02}-{d:02} {h:02}:{m:02}:{s:02} UTC")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_epoch为零() {
        assert_eq!(fmt_utc(0), "1970-01-01 00:00:00 UTC");
    }

    #[test]
    fn utc_已知时刻() {
        // 2026-09-20 00:00:00 UTC = 20716 天 × 86400
        assert_eq!(fmt_utc(20_716 * 86_400), "2026-09-20 00:00:00 UTC");
        // 闰年日: 2024-02-29 12:00:00 UTC
        assert_eq!(fmt_utc(1_709_208_000), "2024-02-29 12:00:00 UTC");
    }
}

fn boot_log(tag: &str, line: &str) {
    if let Ok(mut f) = BOOT_LOG.lock() {
        if let Some(f) = f.as_mut() {
            let _ = writeln!(f, "[{}][{tag}] {}", utc_now_string(), line);
        }
    }
}

/// 后端输出转发到控制台（调试可见, 对齐 Electron 壳的 [server] 前缀行为）+ boot.log
fn drain_output(mut pipe: impl BufRead + Send + 'static) {
    std::thread::spawn(move || {
        let mut line = String::new();
        loop {
            line.clear();
            match pipe.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    eprint!("[server] {line}");
                    boot_log("server", line.trim_end());
                }
            }
        }
    });
}

fn spawn_backend(program: PathBuf, args: Vec<String>, cwd: PathBuf) -> Result<Child, String> {
    let mut cmd = Command::new(&program);
    cmd.args(&args)
        .current_dir(&cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd.spawn().map_err(|e| {
        let msg = format!(
            "启动后端 {} 失败: {e}\n发布版常见原因: 杀毒软件已隔离 resources\\server\\x-code-server.exe, 或安装目录缺少该文件。",
            program.display()
        );
        boot_log("shell", &msg);
        msg
    })?;
    if let Some(out) = child.stdout.take() {
        drain_output(BufReader::new(out));
    }
    if let Some(err) = child.stderr.take() {
        drain_output(BufReader::new(err));
    }
    Ok(child)
}

fn start_server() -> Result<Child, String> {
    // 打包态: 冻结后端随包分发在 <exe>\resources\server\; cwd 指向 ~/.x-code（.env/会话/配置在那里）
    if let Some(exe_path) = sidecar_exe() {
        let _ = std::fs::create_dir_all(data_dir());
        boot_log("shell", &format!("打包态: 拉起冻结后端 {}", exe_path.display()));
        // --parent-pid: 后端内置看门狗, 壳死亡(含崩溃/被强杀)时后端立刻退出,
        // 端口随之释放——ExitRequested 清理只覆盖正常退出路径。
        // --port/--port-file: 发布版固定用 18080 段（写 release-port 文件）,
        // 不与开发/测试后端常占的 8000 撞车, 两种形态可同时存活
        return spawn_backend(
            exe_path,
            vec![
                "--port".to_string(),
                default_port().to_string(),
                "--port-file".to_string(),
                port_file().to_string_lossy().to_string(),
                "--parent-pid".to_string(),
                std::process::id().to_string(),
            ],
            data_dir(),
        );
    }
    // 开发态: 项目 .venv 的 python 跑 server.py。
    // 兼容三种启动: cargo run（cwd=src-tauri）、项目根跑 target 下的 exe、任意目录启动
    let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|e| e.parent().map(|d| d.to_path_buf()));
    let mut root = cwd.join("..");
    let candidates = std::iter::once(cwd.join(".."))
        .chain(std::iter::once(cwd.clone()))
        .chain(exe_dir.map(|d| d.join("../../..")).into_iter());
    for cand in candidates {
        if cand.join("server.py").exists() {
            root = cand;
            break;
        }
    }
    let venv = root.join(".venv/Scripts/python.exe");
    let python = if venv.exists() {
        venv
    } else {
        PathBuf::from("python")
    };
    boot_log(
        "shell",
        &format!(
            "开发态: {} {}（无 .venv 时回落 PATH 里的 python, 找不到会 spawn 失败）",
            python.display(),
            root.join("server.py").display()
        ),
    );
    spawn_backend(
        python,
        vec![
            root.join("server.py").to_string_lossy().to_string(),
            "--parent-pid".to_string(),
            std::process::id().to_string(),
        ],
        root,
    )
}

/// 工具执行（bash 等）会产生 python 的子进程, Windows 上必须整树杀
fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("taskkill")
            .args(["/pid", &pid.to_string(), "/T", "/F"])
            .creation_flags(0x0800_0000) // CREATE_NO_WINDOW
            .status();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("kill").arg(pid.to_string()).status();
    }
}

// ---------- 自动更新（tauri-plugin-updater） ----------

/// 检查到的待安装更新; install_update 消费后置 None（复用 CHILD 的静态模式）。
/// Update 非 Clone, 装进 Option 整体替换; INSTALLED 标记防止双击按钮二连装。
static PENDING_UPDATE: LazyLock<Mutex<Option<tauri_plugin_updater::Update>>> =
    LazyLock::new(|| Mutex::new(None));
static INSTALL_STARTED: LazyLock<Mutex<bool>> = LazyLock::new(|| Mutex::new(false));

/// 下载进度: install_update 的回调线程写入, update_status 由前端轮询读取。
/// phase: 0=空闲 1=下载中 2=下载完成(安装器已拉起) 3=安装失败
static DL_PHASE: AtomicU8 = AtomicU8::new(0);
static DL_RECEIVED: AtomicU64 = AtomicU64::new(0);
static DL_TOTAL: AtomicU64 = AtomicU64::new(0);

/// 检查更新: 有无更新都正常返回（has_update 区分）, 网络失败返回 Err。
#[tauri::command]
async fn check_update(app: AppHandle) -> Result<serde_json::Value, String> {
    use tauri_plugin_updater::UpdaterExt;
    let current = app.package_info().version.to_string();
    boot_log("updater", &format!("检查更新（当前 {current}）"));
    let update = app
        .updater()
        .map_err(|e| {
            boot_log("updater", &format!("ERROR: updater 未就绪: {e}"));
            format!("更新器未就绪: {e}")
        })?
        .check()
        .await;
    match update {
        Ok(Some(update)) => {
            boot_log("updater", &format!("发现新版本 {}", update.version));
            *PENDING_UPDATE.lock().unwrap() = Some(update);
            Ok(serde_json::json!({
                "hasUpdate": true,
                "currentVersion": current,
                "version": PENDING_UPDATE.lock().unwrap().as_ref().map(|u| u.version.clone()),
                "notes": PENDING_UPDATE.lock().unwrap().as_ref().map(|u| u.body.clone()).flatten(),
            }))
        }
        Ok(None) => {
            boot_log("updater", "已是最新版本");
            *PENDING_UPDATE.lock().unwrap() = None;
            Ok(serde_json::json!({ "hasUpdate": false, "currentVersion": current }))
        }
        Err(e) => {
            boot_log("updater", &format!("ERROR: {e}"));
            Err(format!("检查更新失败: {e}"))
        }
    }
}

/// 开始下载并安装: 下载在后台线程推进（进度写 DL_* 原子量, 前端轮询
/// update_status）; 完成后拉起 NSIS 安装器（passive 模式）, 旧进程退出、
/// 新版本启动。后端子进程由 --parent-pid 看门狗随壳退出, 端口自动释放。
#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    let Some(update) = PENDING_UPDATE.lock().unwrap().take() else {
        return Err("没有待安装的更新（请先检查更新）".into());
    };
    {
        let mut started = INSTALL_STARTED.lock().unwrap();
        if *started {
            return Err("更新已在进行中".into());
        }
        *started = true;
    }
    DL_TOTAL.store(0, Ordering::SeqCst);
    DL_RECEIVED.store(0, Ordering::SeqCst);
    DL_PHASE.store(1, Ordering::SeqCst);
    boot_log("updater", "开始下载更新");
    // download_and_install 是 async 且阻塞到安装完成, 放 tauri 异步运行时
    // 的独立任务里跑, 命令立刻返回——进度靠前端轮询 DL_*
    let app2 = app.clone();
    tauri::async_runtime::spawn(async move {
        let result = update
            .download_and_install(
                |chunk, total| {
                    if let Some(t) = total {
                        DL_TOTAL.store(t as u64, Ordering::SeqCst);
                    }
                    DL_RECEIVED.fetch_add(chunk as u64, Ordering::SeqCst);
                },
                || {
                    DL_PHASE.store(2, Ordering::SeqCst);
                    boot_log("updater", "下载完成, 拉起安装器");
                },
            )
            .await;
        match result {
            Ok(()) => {
                // Windows NSIS: 安装器运行时本进程已被要求退出; 若仍在运行
                // （被动安装的边缘情况）, 主动重启走正常退出路径（杀后端）
                boot_log("updater", "更新安装完成, 重启应用");
                let _ = app2.restart();
            }
            Err(e) => {
                boot_log("updater", &format!("ERROR: 安装失败: {e}"));
                DL_PHASE.store(3, Ordering::SeqCst);
                *INSTALL_STARTED.lock().unwrap() = false;
                *PENDING_UPDATE.lock().unwrap() = None;
            }
        }
    });
    Ok(())
}

/// 前端轮询的进度快照
#[tauri::command]
fn update_status() -> serde_json::Value {
    serde_json::json!({
        "phase": DL_PHASE.load(Ordering::SeqCst),
        "received": DL_RECEIVED.load(Ordering::SeqCst),
        "total": DL_TOTAL.load(Ordering::SeqCst),
    })
}

// ---------- 对话框 ----------

fn error_box(title: &str, text: &str) {
    #[cfg(windows)]
    {
        use std::iter::once;
        use std::os::windows::ffi::OsStrExt;
        fn wide(s: &str) -> Vec<u16> {
            std::ffi::OsStr::new(s)
                .encode_wide()
                .chain(once(0))
                .collect()
        }
        #[link(name = "user32")]
        extern "system" {
            fn MessageBoxW(hwnd: isize, text: *const u16, caption: *const u16, utype: u32) -> i32;
        }
        unsafe {
            MessageBoxW(0, wide(text).as_ptr(), wide(title).as_ptr(), 0x10);
        }
    }
    #[cfg(not(windows))]
    {
        eprintln!("{title}: {text}");
    }
}

/// pick_folder 链路日志 tag（落 ~/.x-code/boot.log, 打包版无控制台时的可见性兜底）
const PICK_LOG: &str = "pick_folder";

/// 原生"选择文件夹"对话框（前端经 window.xcodePickFolder() 调用）
///
/// 必须是 async + spawn_blocking: 同步命令在主线程执行, 阻塞式 rfd 对话框
/// 会在主线程上等窗口消息——而消息循环正是被它自己卡住的 → 对话框永远
/// 弹不出来, 前端 await 悬死。挪进阻塞线程池后主线程照常泵消息。
///
/// set_parent(主窗口): 打包版 windows_subsystem=windows 没有控制台, 无父窗口的
/// 对话框可能落在桌面层/被主窗口挡住——用户看来就是"点击无反应"。
/// 返回 Result<Option<String>, String>: Ok(None)=用户取消, Err=真实失败。
/// 此前所有失败路径（JoinError/窗口缺失/ACL/IPC）都被折叠成 null, 无从排查。
/// 系统原生桌面通知。WebView2 未实现 Web Notification API——前端的
/// new Notification() 在壳里静默失败, 桌面端的通知统一走这里代发。
/// 不直接用插件自己的 JS 命令: 远程上下文(127.0.0.1 页面)调插件命令
/// 会被 ACL 拒(实测过 plugin:app), 包成应用命令走 capabilities 白名单。
#[tauri::command]
fn notify_desktop(app: AppHandle, title: String, body: String) -> Result<(), String> {
    use tauri_plugin_notification::NotificationExt;
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|e| e.to_string())
}

/// 读系统剪贴板文本（前端经 window.xcodeReadClipboard() 调用, 右键菜单"粘贴"用）。
/// 不直接用插件自己的 JS 命令: 远程上下文(127.0.0.1 页面)调插件命令会被 ACL 拒
/// （同 notify_desktop 的教训）, 包成应用命令走 capabilities 白名单。
/// 没有这个桥, 前端会回退到 navigator.clipboard.readText()——WebView2 对网页读
/// 剪贴板必弹"是否允许"权限窗, 这就是用户看到的粘贴弹窗。
#[tauri::command]
fn read_clipboard_text(app: AppHandle) -> Result<String, String> {
    use tauri_plugin_clipboard_manager::ClipboardExt;
    app.clipboard().read_text().map_err(|e| e.to_string())
}

#[tauri::command]
async fn pick_folder(app: AppHandle) -> Result<Option<String>, String> {
    boot_log(PICK_LOG, "called");
    eprintln!("[pick_folder] called");
    let Some(win) = app.get_webview_window("main") else {
        boot_log(PICK_LOG, "ERROR: main 窗口不存在");
        eprintln!("[pick_folder] ERROR: main window not found");
        return Err("主窗口不存在，无法打开选择对话框".into());
    };
    // spawn_blocking: rfd 的阻塞式对话框不能跑在主线程——会卡死 UI 消息泵
    let picked = tauri::async_runtime::spawn_blocking(move || {
        rfd::FileDialog::new()
            .set_title("选择文件夹")
            .set_parent(&win)
            .pick_folder()
            .map(|p| p.to_string_lossy().to_string())
    })
    .await
    .map_err(|e| {
        // JoinError（任务 panic/运行时关闭）: 此前被 .ok() 静默折叠成 null
        boot_log(PICK_LOG, &format!("ERROR: 对话框线程失败: {e}"));
        eprintln!("[pick_folder] ERROR: dialog task failed: {e}");
        format!("对话框线程失败: {e}")
    })?;
    match picked {
        Some(p) => {
            boot_log(PICK_LOG, &format!("picked: {p}"));
            eprintln!("[pick_folder] picked: {p}");
            Ok(Some(p))
        }
        None => {
            boot_log(PICK_LOG, "cancelled");
            eprintln!("[pick_folder] cancelled");
            Ok(None)
        }
    }
}

// ---------- 自绘标题栏的窗口控制（decorations: false 后自己实现） ----------

#[tauri::command]
fn minimize_main(app: AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.minimize();
    }
}

#[tauri::command]
fn toggle_maximize_main(app: AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.is_maximized()
            .map(|max| if max { win.unmaximize() } else { win.maximize() });
    }
}

#[tauri::command]
fn close_main(app: AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.close();
    }
}

#[tauri::command]
fn start_drag_main(app: AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.start_dragging();
    }
}

// ---------- 桌宠悬浮窗（pet） ----------

/// 桌宠悬浮窗基准尺寸（逻辑像素, ×"宠物大小"滑杆倍率）。内容自底向上:
/// 精灵格 208 + 状态行/气泡 ~150(权限气泡带命令, 最坏 4-5 行) - 下沉
/// 重叠 22 + 间距 6 ≈ 342, 留余量取 360——窗口必须装得下最坏内容,
/// 否则长气泡/大倍率时贴边被裁(用户观感即"放大到最大会截断")。
const PET_BASE_W: f64 = 280.0;
const PET_BASE_H: f64 = 360.0;

/// 宠物大小滑杆合法区间（pet.js clampScale 同值）
fn clamp_pet_scale(scale: Option<f64>) -> f64 {
    match scale {
        Some(s) if s.is_finite() => s.clamp(0.5, 2.0),
        _ => 1.0,
    }
}

/// 把窗口整体夹回它所在的显示器。桌宠是无边框透明小窗, 用户可拖到
/// 任意位置, 但创建/改尺寸后必须保证整窗在屏——默认位置(1200,600)
/// 在小屏/高缩放下会把一半窗口开出屏幕外。
fn clamp_pet_into_monitor(win: &tauri::WebviewWindow) {
    let Ok(Some(mon)) = win.current_monitor() else {
        return;
    };
    let (Ok(pos), Ok(size)) = (win.outer_position(), win.inner_size()) else {
        return;
    };
    let sf = win.scale_factor().unwrap_or(1.0);
    let p = pos.to_logical::<f64>(sf);
    let s = size.to_logical::<f64>(sf);
    let msf = mon.scale_factor();
    let mo = mon.position().to_logical::<f64>(msf);
    let ms = mon.size().to_logical::<f64>(msf);
    let x = (p.x.max(mo.x)).min(mo.x + ms.width - s.width).max(mo.x);
    let y = (p.y.max(mo.y)).min(mo.y + ms.height - s.height).max(mo.y);
    let _ = win.set_position(tauri::LogicalPosition::new(x, y));
}

/// pet.html 的完整地址: 后端端口 + token。cb 时间戳与主窗同理,
/// 绕开 WebView2 对同 URL 的启发式缓存（否则改版后可能加载旧页面）。
fn pet_url(token: &str) -> String {
    let cb = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!(
        "http://127.0.0.1:{}/pet.html?token={}&cb={}",
        read_port(),
        urlencode(token),
        cb
    )
}

/// 开关桌宠悬浮窗: 没开着则创建——透明 + 无边框 + 置顶 + 不进任务栏,
/// 尺寸只够放下 192x208 的精灵图与其上方的状态行/两行气泡; 已开着则关闭
/// (再点一次桌宠按钮 = 收起)。
/// scale: 宠物大小(0.5–2.0), 主窗设置页滑杆的当前值, 决定开窗尺寸。
/// 必须 async: 同步命令在主线程执行, 而 WebviewWindowBuilder::build()
/// 内部要向主线程派发创建——同步形态自己等自己, 实测整个应用卡死
/// (点了按钮页面无响应)。async 命令跑在异步线程池, 创建正常派发。
#[tauri::command]
async fn open_pet_window(
    app: AppHandle,
    token: String,
    scale: Option<f64>,
) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("pet") {
        let _ = win.close();
        return Ok(());
    }
    let s = clamp_pet_scale(scale);
    let url = pet_url(&token);
    let builder = WebviewWindowBuilder::new(
        &app,
        "pet",
        WebviewUrl::External(url.parse().map_err(|e| format!("桌宠地址非法: {e}"))?),
    )
    .title("x-code 桌宠")
    .decorations(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .resizable(false)
    .shadow(false)
    .inner_size(PET_BASE_W * s, PET_BASE_H * s)   // 基准尺寸 × 宠物大小
    // 右下角附近出生, 用户可拖到任意位置
    .position(1200.0, 600.0)
    .visible(true);
    // 透明窗: transparent 方法在 macOS 的 builder 上不存在 (透明走
    // macOSPrivateApi + 配置式窗口), v1 先接受 mac 桌宠带背景
    #[cfg(not(target_os = "macos"))]
    let builder = builder.transparent(true);
    // 悬浮窗自身也需要桥: startDragPet（拖动）/ petClose（双击收起）/
    // setClickThrough（右键穿透）都经 window.xcodeDesktopPet 走 IPC
    builder
    .initialization_script(BRIDGE_JS)
    .build()
    .map_err(|e| format!("创建桌宠窗口失败: {e}"))?;
    // 默认出生点(1200,600)在大尺寸/小屏组合下可能开出屏幕外, 夹回来
    if let Some(w) = app.get_webview_window("pet") {
        clamp_pet_into_monitor(&w);
    }
    Ok(())
}

#[tauri::command]
fn close_pet(app: AppHandle) {
    if let Some(win) = app.get_webview_window("pet") {
        let _ = win.close();
    }
}

/// 按住宠物拖动 = 移动悬浮窗（复用系统级 start_dragging, 与自绘标题栏同源）
#[tauri::command]
fn start_drag_pet(app: AppHandle) {
    if let Some(win) = app.get_webview_window("pet") {
        let _ = win.start_dragging();
    }
}

/// 鼠标穿透开关: 右键开启后点宠物以外的区域都落到下层窗口;
/// 恢复靠主窗的召唤按钮（open_pet_window 会先关穿透）
#[tauri::command]
fn set_pet_click_through(app: AppHandle, ignore: bool) {
    if let Some(win) = app.get_webview_window("pet") {
        let _ = win.set_ignore_cursor_events(ignore);
    }
}

/// 把桌宠窗带到前台并聚焦: 悬浮输入框获得焦点时调用。透明置顶小窗
/// 默认不抢前台——原生窗口不在前台时, IME 上下文挂不到 WebView2 上,
/// 实测候选框飘到屏幕左上角、Ctrl+Space/Wn+Space 也切不动输入法;
/// set_focus 后输入法正常跟随输入框, 切换也归本窗。
/// async 形态理由同 move_pet_window: set_focus 要向主线程派发。
#[tauri::command]
async fn focus_pet(app: AppHandle) {
    if let Some(win) = app.get_webview_window("pet") {
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// 手动拖动: pet 页按指针位移调这里挪窗（逻辑坐标, 与 JS 的
/// screenX/screenY 同参照）。必须 async——同步命令在主线程执行,
/// set_position 又要向主线程派发, 会与 open_pet_window 同款互等卡死。
#[tauri::command]
async fn move_pet_window(app: AppHandle, x: f64, y: f64) {
    if let Some(win) = app.get_webview_window("pet") {
        let _ = win.set_position(tauri::LogicalPosition::new(x, y));
    }
}

/// 宠物大小热调: 主窗滑杆 / pet 页启动时把缩放同步到窗口尺寸。
/// async 形态理由同 move_pet_window——set_size 要向主线程派发。
/// 等值跳过: 窗口创建时已按目标尺寸建好, 无谓的 set_size 会触发
/// WebView2 重排, 有把透明底打回白色"框"的风险。
#[tauri::command]
async fn resize_pet_window(app: AppHandle, scale: f64) {
    let s = clamp_pet_scale(Some(scale));
    if let Some(win) = app.get_webview_window("pet") {
        let target = tauri::LogicalSize::new(PET_BASE_W * s, PET_BASE_H * s);
        let sf = win.scale_factor().unwrap_or(1.0);
        let cur = win.inner_size().ok().map(|p| p.to_logical::<f64>(sf));
        if let Some(cur) = &cur {
            if (cur.width - target.width).abs() < 0.5
                && (cur.height - target.height).abs() < 0.5
            {
                return;
            }
        }
        // 底边锚定: 宠物站在窗口底部, 高度变化必须朝上伸缩——保持左上角
        // 不动的话, 变大后底锚内容整体沉进屏幕外, 只剩头顶露在旧窗口里。
        let old = win.outer_position().ok().map(|p| p.to_logical::<f64>(sf));
        let mut new_x = old.as_ref().map(|p| p.x).unwrap_or(0.0);
        let mut new_y = old.as_ref().map(|p| p.y).unwrap_or(0.0)
            + cur.map(|c| c.height - target.height).unwrap_or(0.0);
        // 再夹回所在显示器, 别缩放完一半在屏幕外
        if let Ok(Some(mon)) = win.current_monitor() {
            let msf = mon.scale_factor();
            let mo = mon.position().to_logical::<f64>(msf);
            let ms = mon.size().to_logical::<f64>(msf);
            new_x = (new_x.max(mo.x)).min(mo.x + ms.width - target.width);
            new_y = (new_y.max(mo.y)).min(mo.y + ms.height - target.height);
        }
        let _ = win.set_size(target);
        let _ = win.set_position(tauri::LogicalPosition::new(new_x, new_y));
    }
}

// ---------- 注入页面的桥（对齐 electron/preload.js） ----------

/// 桌面桥: window.xcodeDesktop 标记（app.js 入口守卫依赖）+
/// window.xcodePickFolder() 原生选文件夹桥 + 掐掉浏览器行为。
/// 幂等（__xcodeBridgeInstalled 哨兵）: initialization_script 之外,
/// on_page_load 还会 eval 重申一次——注入偶发失效时兜底。
const BRIDGE_JS: &str = r#"
(() => {
  // 结构即健壮性: 哨兵必须在全部安装完成后才置位。此前哨兵在最前, 脚本中途
  // 抛错(注入脚本跑在文档解析前, documentElement 可能为 null → classList
  // 抛 TypeError)会留下"哨兵已装、桥没装"的半安装态, on_page_load 的重申
  // 也被哨兵拦截 → xcodePickFolder 永远缺失, 页面静默走进浏览器兜底
  // (#dir-pop), 用户看到的就是"弹不出原生文件夹对话框"。
  // 1) 桥最优先: 后面任何一步失败都不影响 xcodePickFolder 存在
  if (!window.xcodePickFolder) {
    window.xcodePickFolder = async () => {
      // 失败必须可见: Promise reject = 真实失败（ACL/IPC/对话框崩溃）,
      // 页面 catch 据此 toast 报错; 用户取消由 Rust 返回 null 表达, 不走 reject。
      if (!window.__TAURI_INTERNALS__) {
        console.error('[xcode] __TAURI_INTERNALS__ 缺失: pick_folder 无法调用');
        throw new Error('桌面桥未就绪（__TAURI_INTERNALS__ 缺失）');
      }
      try {
        return await window.__TAURI_INTERNALS__.invoke('pick_folder');
      } catch (e) {
        console.error('[xcode] pick_folder IPC 失败:', e);
        throw e;
      }
    };
  }
  if (!window.xcodeDesktop) {
    Object.defineProperty(window, 'xcodeDesktop', { value: true });
  }
  // 剪贴板读取桥（右键菜单"粘贴"用）: 必须走 Rust 侧 clipboard API。
  // 缺桥时前端回退 navigator.clipboard.readText(), WebView2 对网页读剪贴板
  // 会弹"是否允许"权限窗——用户看到的粘贴弹窗就是它。
  if (!window.xcodeReadClipboard) {
    window.xcodeReadClipboard = async () => {
      if (!window.__TAURI_INTERNALS__) return null;   // 页面在浏览器里预览: 无壳
      try {
        return await window.__TAURI_INTERNALS__.invoke('read_clipboard_text');
      } catch (e) {
        console.error('[xcode] read_clipboard_text IPC 失败:', e);
        throw e;
      }
    };
  }
  // 应用版本号桥: 标题栏徽标用。打包后的 Python 后端不带 pyproject.toml,
  // 服务端读不到版本 → 壳内一律问壳自己。版本号由 Rust 在注入脚本头部
  // 烤成 window.__XCODE_VERSION__（create_main_window 处拼接）, 这里直接读;
  // invoke('plugin:app|version') 做兜底（remote 上下文的 ACL 曾实测拒掉该命令,
  // 故不作为主路径）。
  if (!window.xcodeAppVersion) {
    window.xcodeAppVersion = async () => {
      if (window.__XCODE_VERSION__) return window.__XCODE_VERSION__;
      if (!window.__TAURI_INTERNALS__) return null;   // 页面在浏览器里预览: 无壳
      try {
        return await window.__TAURI_INTERNALS__.invoke('plugin:app|version');
      } catch (e) {
        console.error('[xcode] get app version 失败:', e);
        return null;
      }
    };
  }
  // 桌宠悬浮窗桥: petFloat 打开/聚焦悬浮窗（token 从本页 cookie 取——
  // 后端门禁认它）; petClose 关窗; startDragPet 把拖动交给系统;
  // setClickThrough 右键鼠标穿透。全部走"失败必须可见": reject 而非静默 null。
  if (!window.xcodeDesktopPet) {
    const petInvoke = async (cmd, payload) => {
      if (!window.__TAURI_INTERNALS__) {
        throw new Error('桌面桥未就绪（__TAURI_INTERNALS__ 缺失）');
      }
      try {
        return await window.__TAURI_INTERNALS__.invoke(cmd, payload);
      } catch (e) {
        console.error('[xcode] ' + cmd + ' IPC 失败:', e);
        throw e;
      }
    };
    window.xcodeDesktopPet = {
      petFloat: (scale) => petInvoke('open_pet_window', {
        token: (document.cookie.match(/(?:^|;\s*)xcode_token=([^;]*)/) || [])[1]
          ? decodeURIComponent((document.cookie.match(/(?:^|;\s*)xcode_token=([^;]*)/) || [])[1])
          : '',
        scale: Number(scale) || 1
      }),
      petClose: () => petInvoke('close_pet'),
      startDragPet: () => petInvoke('start_drag_pet'),
      setClickThrough: (ignore) => petInvoke('set_pet_click_through', { ignore: !!ignore }),
      focusPet: () => petInvoke('focus_pet'),
      movePet: (x, y) => petInvoke('move_pet_window', { x: Number(x), y: Number(y) }),
      resizePet: (scale) => petInvoke('resize_pet_window', { scale: Number(scale) || 1 }),
    };
  }
  // 自动更新桥: 检查/安装/进度查询。安装进度走轮询而非事件监听——
  // 远端上下文的事件 ACL 曾实测拒过插件命令, invoke 应用命令是已验证的路子。
  if (!window.xcodeDesktopUpdater) {
    const updInvoke = async (cmd) => {
      if (!window.__TAURI_INTERNALS__) {
        throw new Error('桌面桥未就绪（__TAURI_INTERNALS__ 缺失）');
      }
      try {
        return await window.__TAURI_INTERNALS__.invoke(cmd);
      } catch (e) {
        console.error('[xcode] ' + cmd + ' IPC 失败:', e);
        throw e;
      }
    };
    window.xcodeDesktopUpdater = {
      check: () => updInvoke('check_update'),
      install: () => updInvoke('install_update'),
      status: () => updInvoke('update_status'),
    };
  }
  // 2) DOM 相关: 注入时机 documentElement 可能尚未创建 → 空值安全 + 就绪后补挂
  const installDom = () => {
    if (document.documentElement.dataset.xcodeDomInstalled) return;  // 重申幂等
    document.documentElement.dataset.xcodeDomInstalled = '1';
    // 桌面应用形态, 三层配合:
    // 1) Rust: SetAreDefaultContextMenusEnabled(false) + SetAreBrowserAcceleratorKeysEnabled(false)
    // 2) 这里: contextmenu 捕获阶段 preventDefault（右键菜单由 app.js 自建）
    // 3) 这里: F5/Ctrl+R 兜底拦截——设置应用前的窗口期也不许刷新
    document.documentElement.classList.add('xcode-desktop');   // 显示自绘标题栏
    document.addEventListener('contextmenu', e => e.preventDefault(), true);
    document.addEventListener('keydown', e => {
      const isReload = e.key === 'F5' || (e.ctrlKey && e.key.toLowerCase() === 'r');
      if (isReload) { e.preventDefault(); e.stopPropagation(); }
    }, true);
    ['dragover', 'drop'].forEach(t =>
      document.addEventListener(t, e => e.preventDefault()));
  };
  if (document.documentElement) installDom();
  else document.addEventListener('DOMContentLoaded', installDom, { once: true });
  // 3) 哨兵最后: 只有全部装完才标记——半安装态不再拦截重申, 重申反而能自愈
  window.__xcodeBridgeInstalled = true;
})();
"#;

// ---------- 启动流程 ----------

fn main() {
    let token = ensure_token();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 单实例: 二次启动只把已有窗口带到前台
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.unminimize();
                let _ = win.set_focus();
            }
        }))
        .manage(())
        .invoke_handler(tauri::generate_handler![
            pick_folder,
            read_clipboard_text,
            notify_desktop,
            check_update,
            install_update,
            update_status,
            minimize_main,
            toggle_maximize_main,
            close_main,
            start_drag_main,
            open_pet_window,
            close_pet,
            start_drag_pet,
            set_pet_click_through,
            focus_pet,
            move_pet_window,
            resize_pet_window
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |app, event| match event {
            RunEvent::Ready => {
                let app = app.clone();
                let token = token.clone();
                // 窗口先开（秒级, 显示 loading 页）, 后端在后台线程拉起
                if let Err(e) = create_main_window(&app) {
                    error_box("x-code 启动失败", &e);
                    app.exit(1);
                    return;
                }
                std::thread::spawn(move || {
                    if let Err(e) = bootstrap(&token) {
                        error_box("x-code 启动失败", &e);
                        app.exit(1);
                        return;
                    }
                    // 就绪后把窗口从 loading 页导航到真正的应用地址
                    if let Some(win) = app.get_webview_window("main") {
                        let _ = win.eval(&format!(
                            "location.replace('{}')",
                            app_url(&token)
                        ));
                    }
                });
            }
            RunEvent::ExitRequested { .. } => {
                // 退出: 杀掉自己拉起的后端（复用的外部服务不动）
                if let Some(child) = CHILD.lock().unwrap().take() {
                    kill_tree(child.id());
                }
            }
            RunEvent::WindowEvent { label, event: WindowEvent::CloseRequested { .. }, .. }
                if label == "main" =>
            {
                // 主窗关闭 = 整个应用退出: 桌宠窗若还开着, "所有窗口已关"
                // 永远不成立, ExitRequested(杀后端清理)就不会来——壳和后端
                // 双双残留。主窗关时把桌宠一并带上, 走正常退出路径。
                if let Some(pet) = app.get_webview_window("pet") {
                    let _ = pet.close();
                }
            }
            _ => {}
        });
}

fn bootstrap(token: &str) -> Result<(), String> {
    let log = boot_log_path();
    boot_log("shell", "=== 壳启动, 开始引导后端 ===");
    // 端口已有 x-code 在跑 → 复用（另一实例/用户手动起的服务）, 不重复拉
    let mut port = read_port();
    if ping_server(port, token, Duration::from_millis(1200)) {
        boot_log("shell", &format!("复用已运行的后端 port={port}"));
        return Ok(());
    }
    let child = start_server()?;
    *CHILD.lock().unwrap() = Some(child);
    boot_log("shell", "后端进程已拉起, 开始探活");
    let t0 = Instant::now();
    // 60s 而非 30s: 杀软首次深度扫描 + onefile 解压在低端机/冷盘上会超 30s。
    // 期间每轮先看后端是否已退出——死了就不干等, 退出码 + boot.log 才是答案。
    while t0.elapsed() < Duration::from_secs(60) {
        {
            let mut guard = CHILD.lock().unwrap();
            if let Some(child) = guard.as_mut() {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        let msg = format!(
                            "后端进程启动后即退出（{status}）。\n常见原因: 杀毒软件拦截/隔离了 resources\\server\\x-code-server.exe（请在杀软隔离区找回并加入白名单）, 或安装目录缺少该文件。\n后端完整输出已写入 {}",
                            log.display()
                        );
                        boot_log("shell", &format!("后端提前退出: {status}"));
                        return Err(msg);
                    }
                    Ok(None) => {}
                    Err(e) => boot_log("shell", &format!("try_wait 失败: {e}")),
                }
            }
        }
        port = read_port(); // 后端避让后会把实际端口写进 port 文件, 每轮重读
        if ping_server(port, token, Duration::from_millis(800)) {
            boot_log("shell", &format!("后端就绪 port={port}"));
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Err(format!(
        "Python 后端在 60 秒内未能就绪。\n可能原因: ① {}-{} 端口被其他程序占用; ② 杀毒软件拦截后端进程（请查杀软隔离区并加白名单）。\n后端完整输出已写入 {}",
        default_port(),
        default_port() + 19,
        log.display()
    ))
}

fn create_main_window(app: &AppHandle) -> Result<(), String> {
    // 首屏 = 内嵌 loading 页（tauri:// 资产协议, 不依赖后端进程）。
    // 此前窗口要等后端就绪才创建——Python 冷启动约 3s, 用户对着空白。
    // 现在窗口秒开, bootstrap 完成后由启动线程导航到真正的应用地址。
    let app_for_nav = app.clone();   // 闭包要求 'static: 捕获克隆而非函数引用
    // 版本号烤进注入脚本头部: 编译期常量（tauri.conf.json 的 version）,
    // 前端 window.__XCODE_VERSION__ 直接读, 不经 IPC——remote 页面对
    // plugin:app 命令的 ACL 曾实测不放行, invoke 路线不可靠。
    let version = &app.package_info().version;
    let bridge_js = format!("window.__XCODE_VERSION__ = '{version}';\n{BRIDGE_JS}");
    let builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("loading.html".into()))
        .title("x-code")
        .decorations(false)   // 自绘标题栏: 高度可控, 主题跟随应用深浅色
        .inner_size(1440.0, 900.0)
        .min_inner_size(960.0, 600.0)
        .visible(false) // 页面就绪后再显示, 避免白屏闪烁
        .initialization_script(&bridge_js);
    // 窗口级亚克力 (仅 Windows): 窗口与 WebView2 背景透明, 桌面经 DWM
    // ACRYLICBLURBEHIND 模糊透出。材质无条件挂载是安全的——普通深浅主题
    // 页面画的是不透明背景, 材质被盖住不可见; 只有切到亚克力主题(页面变
    // 透明)时才透出, 主题切换无需重启壳。Win10 已知取舍: 拖动窗口时材质
    // 有轻微滞后, 不能接受可把 Effect::Acrylic 换成 Effect::Blur(无滞后,
    // 少质感)。transparent 方法在 macOS builder 上不存在 (transparent +
    // effects 一起门控); Linux/mac 页面自绘背景, 观感不变。
    #[cfg(windows)]
    let builder = builder
        .transparent(true)
        .effects(WindowEffectsConfig {
            effects: vec![Effect::Acrylic],
            state: None,
            radius: None,
            color: None,
        });
    builder
        .on_navigation(move |url| {
            let s = url.as_str();
            // 内部导航放行: 后端地址（任意端口）+ tauri 内嵌资产 + 浏览器内部页。
            // 内嵌资产在 Windows WebView2 上是 http(s)://tauri.localhost,
            // 在 macOS/Linux 上是 tauri://localhost——漏了前者会把启动页
            // 误判为外部链接, 每次启动都用系统浏览器开一遍 loading.html
            if s.starts_with("http://127.0.0.1")
                || s.starts_with("http://tauri.localhost")
                || s.starts_with("https://tauri.localhost")
                || s.starts_with("tauri://localhost")
                || s.starts_with("about:")
            {
                true
            } else {
                // 外部链接（markdown 链接等）转交系统浏览器
                let open = app_for_nav.opener().open_url(s, None::<&str>).is_ok();
                open
            }
        })
        .on_page_load(move |win, payload| {
            if payload.event() == tauri::webview::PageLoadEvent::Finished {
                let _ = win.show();
                let _ = win.set_focus();
                // 桥的重申: initialization_script 偶发不注入时在此兜底
                let _ = win.eval(&bridge_js);
                apply_desktop_webview_settings(&win);
            }
        })
        .build()
        .map_err(|e| e.to_string())?;

    Ok(())
}

/// 把 WebView2 的浏览器行为关成桌面应用形态:
/// - 默认右键菜单（"刷新/返回/打印"等浏览器项）→ 前端自建菜单替代
/// - 浏览器加速键（F5/Ctrl+R 刷新、Ctrl+P 打印、Ctrl+F 查找等）
/// - 表单自动填充（搜索框上"保存的信息"弹层）
///
/// 每次页面加载完成都调用: 一次性 with_webview 在窗口创建期有竞态
/// （实测偶发不执行, debug-nav.txt 里可见）, on_page_load 则必然触发;
/// Set* 幂等, 重复只是重申。设置是 webview 级的, 导航后持续生效。
#[cfg(windows)]
fn apply_desktop_webview_settings(win: &tauri::WebviewWindow) {
    use webview2_com::Microsoft::Web::WebView2::Win32::{ICoreWebView2Settings3, ICoreWebView2Settings4};
    use windows::core::Interface;

    let scheduled = win.with_webview(|wv| {
        let r = unsafe {
            (|| -> windows::core::Result<()> {
                let core = wv.controller().CoreWebView2()?;
                let settings = core.Settings()?;
                settings.SetAreDefaultContextMenusEnabled(false)?;
                let s3 = settings.cast::<ICoreWebView2Settings3>()?;
                s3.SetAreBrowserAcceleratorKeysEnabled(false)?;
                let s4 = settings.cast::<ICoreWebView2Settings4>()?;
                s4.SetIsGeneralAutofillEnabled(false)?;
                s4.SetIsPasswordAutosaveEnabled(false)?;
                Ok(())
            })()
        };
        if let Some(err) = r.err() {
            diag_log(&format!("webview-settings error: {err}"));
        }
    });
    if let Err(e) = scheduled {
        diag_log(&format!("webview-settings schedule error: {e}"));
    }
}

#[cfg(not(windows))]
fn apply_desktop_webview_settings(_win: &tauri::WebviewWindow) {}

/// 壳的诊断日志: ~/.x-code/debug-nav.txt（排查原生菜单/刷新复发用）
fn diag_log(msg: &str) {
    use std::io::Write as _;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(data_dir().join("debug-nav.txt"))
    {
        let _ = writeln!(f, "{msg}");
    }
}

/// 应用真实入口: 本地 FastAPI 服务（页面/静态/API/WS 同源）。
/// 必须在 bootstrap 之后再调——端口以后端避让后写出的 port 文件为准。
fn app_url(token: &str) -> String {
    // cb 时间戳: 每次 launches URL 唯一, 绕开 WebView2 对主文档的启发式
    // 缓存——后端虽发 no-cache, 实测同 URL 导航仍可能吃旧缓存
    let cb = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!(
        "http://127.0.0.1:{}/?token={}&desktop=1&cb={}",
        read_port(),
        urlencode(token),
        cb
    )
}

fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
