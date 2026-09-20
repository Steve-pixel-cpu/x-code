// x-code 桌面壳（Tauri 版）, 对齐 electron/main.js 的全部行为:
//   1. 令牌: 与后端共享 ~/.x-code/token, 打开页面时经 ?token= 传入（index.html 里种成 cookie）
//   2. 端口: 读 ~/.x-code/port（缺省 8000）; 后端被占端口自动避让 8010–8019 并回写该文件
//   3. 探活: GET /api/ping 须 200 且 body 含 "x-code"（8000 被 C-Lodop 等抢占时不能误判为就绪）
//   4. 复用: 已有 x-code 服务在跑 → 直接连, 不拉进程、退出时不杀
//   5. 拉起: 打包态用冻结后端（resources/server/x-code-server.exe, cwd=~/.x-code）,
//            开发态用 .venv 的 python server.py; 退出整树杀
//   6. 前端: 窗口加载本地服务; 注入 window.xcodeDesktop 标记 + xcodePickFolder 原生选文件夹桥;
//            外部链接转交系统浏览器
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use tauri::window::Effect;
use tauri::utils::config::WindowEffectsConfig;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
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

fn read_port() -> u16 {
    read_trim(&data_dir().join("port"))
        .and_then(|s| s.parse::<u16>().ok())
        .filter(|p| *p > 0)
        .unwrap_or(8000)
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

/// 后端输出转发到控制台（调试可见, 对齐 Electron 壳的 [server] 前缀行为）
fn drain_output(mut pipe: impl BufRead + Send + 'static) {
    std::thread::spawn(move || {
        let mut line = String::new();
        loop {
            line.clear();
            match pipe.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => eprint!("[server] {line}"),
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
    let mut child = cmd.spawn().map_err(|e| format!("spawn backend: {e}"))?;
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
    let sidecar = std::env::current_exe().ok().and_then(|exe| {
        let p = exe
            .parent()?
            .join("resources/server/x-code-server.exe");
        p.exists().then_some(p)
    });
    if let Some(exe_path) = sidecar {
        let _ = std::fs::create_dir_all(data_dir());
        // --parent-pid: 后端内置看门狗, 壳死亡(含崩溃/被强杀)时后端立刻退出,
        // 端口随之释放——ExitRequested 清理只覆盖正常退出路径
        return spawn_backend(
            exe_path,
            vec!["--parent-pid".to_string(), std::process::id().to_string()],
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

/// 原生"选择文件夹"对话框（前端经 window.xcodePickFolder() 调用）
///
/// 必须是 async + spawn_blocking: 同步命令在主线程执行, 阻塞式 rfd 对话框
/// 会在主线程上等窗口消息——而消息循环正是被它自己卡住的 → 对话框永远
/// 弹不出来, 前端 await 悬死。挪进阻塞线程池后主线程照常泵消息。
#[tauri::command]
async fn pick_folder() -> Option<String> {
    // spawn_blocking: rfd 的阻塞式对话框不能跑在主线程——会卡死 UI 消息泵
    tauri::async_runtime::spawn_blocking(move || {
        rfd::FileDialog::new()
            .set_title("选择文件夹")
            .pick_folder()
            .map(|p| p.to_string_lossy().to_string())
    })
    .await
    .ok()
    .flatten()
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

// ---------- 注入页面的桥（对齐 electron/preload.js） ----------

/// 桌面桥: window.xcodeDesktop 标记（app.js 入口守卫依赖）+
/// window.xcodePickFolder() 原生选文件夹桥 + 掐掉浏览器行为。
/// 幂等（__xcodeBridgeInstalled 哨兵）: initialization_script 之外,
/// on_page_load 还会 eval 重申一次——注入偶发失效时兜底。
const BRIDGE_JS: &str = r#"
(() => {
  if (window.__xcodeBridgeInstalled) return;
  window.__xcodeBridgeInstalled = true;
  Object.defineProperty(window, 'xcodeDesktop', { value: true });
  document.documentElement.classList.add('xcode-desktop');   // 显示自绘标题栏
  // 桌面应用形态, 三层配合:
  // 1) Rust: SetAreDefaultContextMenusEnabled(false) + SetAreBrowserAcceleratorKeysEnabled(false)
  // 2) 这里: contextmenu 捕获阶段 preventDefault（右键菜单由 app.js 自建）
  // 3) 这里: F5/Ctrl+R 兜底拦截——设置应用前的窗口期也不许刷新
  document.addEventListener('contextmenu', e => e.preventDefault(), true);
  document.addEventListener('keydown', e => {
    const isReload = e.key === 'F5' || (e.ctrlKey && e.key.toLowerCase() === 'r');
    if (isReload) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  ['dragover', 'drop'].forEach(t =>
    document.addEventListener(t, e => e.preventDefault()));
  window.xcodePickFolder = async () => {
    try { return await window.__TAURI_INTERNALS__.invoke('pick_folder'); }
    catch (e) { return null; }
  };
})();
"#;

// ---------- 启动流程 ----------

fn main() {
    let token = ensure_token();

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_clipboard_manager::init())
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
            minimize_main,
            toggle_maximize_main,
            close_main,
            start_drag_main
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
            _ => {}
        });
}

fn bootstrap(token: &str) -> Result<(), String> {
    // 端口已有 x-code 在跑 → 复用（另一实例/用户手动起的服务）, 不重复拉
    let mut port = read_port();
    if ping_server(port, token, Duration::from_millis(1200)) {
        return Ok(());
    }
    let child = start_server()?;
    *CHILD.lock().unwrap() = Some(child);
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(30) {
        port = read_port(); // 后端避让后会把实际端口写进 port 文件, 每轮重读
        if ping_server(port, token, Duration::from_millis(800)) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Err("Python 后端在 30 秒内未能就绪。\n若 8000-8019 端口被其他程序（如 C-Lodop 打印服务）占用, 请关闭后重试。".to_string())
}

fn create_main_window(app: &AppHandle) -> Result<(), String> {
    // 首屏 = 内嵌 loading 页（tauri:// 资产协议, 不依赖后端进程）。
    // 此前窗口要等后端就绪才创建——Python 冷启动约 3s, 用户对着空白。
    // 现在窗口秒开, bootstrap 完成后由启动线程导航到真正的应用地址。
    let app_for_nav = app.clone();   // 闭包要求 'static: 捕获克隆而非函数引用
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("loading.html".into()))
        .title("x-code")
        .decorations(false)   // 自绘标题栏: 高度可控, 主题跟随应用深浅色
        // 窗口级亚克力: 窗口与 WebView2 背景透明, 桌面经 DWM ACRYLICBLURBEHIND
        // 模糊透出。材质无条件挂载是安全的——普通深浅主题页面画的是不透明
        // 背景, 材质被盖住不可见; 只有切到亚克力主题(页面变透明)时才透出,
        // 主题切换无需重启壳。Win10 已知取舍: 拖动窗口时材质有轻微滞后,
        // 不能接受可把 Effect::Acrylic 换成 Effect::Blur(无滞后, 少质感)。
        .transparent(true)
        .effects(WindowEffectsConfig {
            effects: vec![Effect::Acrylic],
            state: None,
            radius: None,
            color: None,
        })
        .inner_size(1440.0, 900.0)
        .min_inner_size(960.0, 600.0)
        .visible(false) // 页面就绪后再显示, 避免白屏闪烁
        .initialization_script(BRIDGE_JS)
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
        .on_page_load(|win, payload| {
            if payload.event() == tauri::webview::PageLoadEvent::Finished {
                let _ = win.show();
                let _ = win.set_focus();
                // 桥的重申: initialization_script 偶发不注入时在此兜底
                let _ = win.eval(BRIDGE_JS);
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
///
/// 每次页面加载完成都调用: 一次性 with_webview 在窗口创建期有竞态
/// （实测偶发不执行, debug-nav.txt 里可见）, on_page_load 则必然触发;
/// Set* 幂等, 重复只是重申。设置是 webview 级的, 导航后持续生效。
#[cfg(windows)]
fn apply_desktop_webview_settings(win: &tauri::WebviewWindow) {
    use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Settings3;
    use windows::core::Interface;

    let scheduled = win.with_webview(|wv| {
        let r = unsafe {
            (|| -> windows::core::Result<()> {
                let core = wv.controller().CoreWebView2()?;
                let settings = core.Settings()?;
                settings.SetAreDefaultContextMenusEnabled(false)?;
                let s3 = settings.cast::<ICoreWebView2Settings3>()?;
                s3.SetAreBrowserAcceleratorKeysEnabled(false)?;
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
