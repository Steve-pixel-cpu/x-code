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
        return spawn_backend(exe_path, vec![], data_dir());
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
        vec![root.join("server.py").to_string_lossy().to_string()],
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
#[tauri::command]
fn pick_folder() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择文件夹")
        .pick_folder()
        .map(|p| p.to_string_lossy().to_string())
}

// ---------- 注入页面的桥（对齐 electron/preload.js） ----------

/// 对齐 preload.js: window.xcodeDesktop 标记（app.js 入口守卫依赖）+
/// window.xcodePickFolder() 原生选文件夹桥
const BRIDGE_JS: &str = r#"
(() => {
  Object.defineProperty(window, 'xcodeDesktop', { value: true });
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
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 单实例: 二次启动只把已有窗口带到前台
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.unminimize();
                let _ = win.set_focus();
            }
        }))
        .manage(())
        .invoke_handler(tauri::generate_handler![pick_folder])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |app, event| match event {
            RunEvent::Ready => {
                let app = app.clone();
                let token = token.clone();
                std::thread::spawn(move || {
                    if let Err(e) = bootstrap(&token) {
                        error_box("x-code 启动失败", &e);
                        app.exit(1);
                        return;
                    }
                    if let Err(e) = create_main_window(&app, &token) {
                        error_box("x-code 启动失败", &e);
                        app.exit(1);
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

fn create_main_window(app: &AppHandle, token: &str) -> Result<(), String> {
    let port = read_port();
    let base = format!("http://127.0.0.1:{port}");
    let url: tauri::Url = format!("{base}/?token={}", urlencode(token))
        .parse()
        .map_err(|e| format!("bad url: {e}"))?;

    // 窗口加载本地 FastAPI 服务（页面/静态/API/WS 同源）
    let base_for_nav = base.clone();
    let app_for_nav = app.clone();
    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
        .title("x-code")
        .inner_size(1440.0, 900.0)
        .min_inner_size(960.0, 600.0)
        .visible(false) // 页面就绪后再显示, 避免白屏闪烁
        .initialization_script(BRIDGE_JS)
        .on_navigation(move |url| {
            // 本地链接放行; 外部链接（markdown 链接等）转交系统浏览器
            let s = url.as_str();
            if s.starts_with(&base_for_nav) || s.starts_with("http://127.0.0.1") {
                true
            } else {
                let _ = app_for_nav.opener().open_url(s, None::<&str>);
                false
            }
        })
        .on_page_load(|win, payload| {
            if payload.event() == tauri::webview::PageLoadEvent::Finished {
                let _ = win.show();
                let _ = win.set_focus();
            }
        })
        .build()
        .map_err(|e| e.to_string())?;

    Ok(())
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
