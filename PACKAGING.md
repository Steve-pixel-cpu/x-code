# x-code 打包成 exe 指南

## 一键打包

```cmd
build-exe.cmd            REM 默认: Tauri 壳
build-exe.cmd tauri      REM 同上, 显式指定
build-exe.cmd electron   REM Electron 壳 (NSIS 安装包 + portable 免安装版)
build-exe.cmd both       REM 两种都打
```

两种壳共用同一条后端流水线：装 PyInstaller → 把 `server.py` 冻结成
`build\server\x-code-server.exe`（`static/` 已打入），之后按目标分别走 Tauri 或
electron-builder。产物统一在 `dist\`。

### Tauri（默认）

| 文件 | 说明 |
|---|---|
| `x-code_0.1.0_x64-setup.exe` | NSIS 安装包（当前用户安装, 可选安装目录） |

技术栈：**Tauri 2 壳（Rust + 系统 WebView2）+ PyInstaller 冻结的 Python 后端**。
启动时 Tauri 拉起 `resources\server\x-code-server.exe`（默认监听 127.0.0.1:8000），
WebView2 窗口加载本地服务；如果 8000 端口已有 x-code 在跑则直接复用，不重复拉进程。

### Electron（`electron` / `both` 参数）

| 文件 | 说明 |
|---|---|
| `x-code Setup 0.1.0.exe` | NSIS 安装包 |
| `x-code 0.1.0.exe` | portable 免安装版，双击即用 |

技术栈：**Electron 壳（自带 Chromium）+ 同一个冻结后端**（electron-builder 经
`extraResources` 自动把 `build\server` 打进安装包）。运行行为与 Tauri 版一致：
端口探测/复用、令牌门禁、单实例都由 `electron/main.js` 提供。
需要 Rust 工具链的只有 Tauri 目标；Electron 目标只依赖 Node.js。
体积较大（~130MB+），但不需要系统 WebView2。

> 项目已从 Electron 迁移到 Tauri（安装包从 ~133MB 降到 ~28MB），Tauri 仍是默认目标。
> Electron 壳保留为可选项：`build-exe.cmd electron` 即可启用，无需清理相关文件。

## 端口自动避让

8000 端口常被其他程序抢占（**C-Lodop 云打印服务默认占 8000/18000**，装了它的电脑
最容易撞上）。因此后端绑定失败会自动尝试 8010–8019，并把最终端口写入
`~/.x-code/port`；桌面壳读取该文件动态访问，无需人工干预。想固定端口可给后端传
`--port` 参数或设置 `XCODE_PORT` 环境变量。

## 桌面端的浏览器测试工具

桌面版自带 `browser_navigate` / `browser_snapshot` / `browser_click` /
`browser_type` / `browser_console` 等浏览器工具（Playwright 驱动 headless
Chromium），Agent 可实际操作 Web 系统做功能测试。冻结后端已把 Playwright
driver 打进 exe（`--collect-all playwright`），无需 `playwright install`：
启动渠道自动回退 chromium → 本机 Chrome → 本机 Edge（Win10/11 必有 Edge，
等效零额外下载）；`XCODE_BROWSER_CHANNEL` 环境变量可强制指定渠道。

## 分发给别人时要带的配置

不需要带任何配置文件。拿到 exe 的人首次打开会进入**初始化页**，填入 API Key
（和可选的接口地址）即可；配置保存在 `C:\Users\<用户名>\.x-code\settings.json`。

会话记录、多 agent 存档同样存于 `~/.x-code\`，与 exe 安装位置无关，升级覆盖安装不丢数据。

**运行前提**：Windows 10/11 自带 WebView2 运行时（Tauri 渲染层）。极少数精简系统
可能没有，安装包会引导安装，或从微软官网下载 WebView2 Runtime。

## 运行前提：Git for Windows（工具执行器 / 中文编码）

**启动时强校验**：CLI 与后端服务启动都会检测 Git Bash，检测不到就拒绝启动
（CLI 打印说明后退出；桌面端后端退出时把原因写入 `~/.x-code/startup-error.log`，
启动成功会自动删除该文件）。安装 [Git for Windows](https://git-scm.com/download/win)
后重启即可，默认选项、无需配置。

为什么强依赖 Git Bash：MSYS2 的 coreutils（cat/grep/ls）对文件内容字节直通，
UTF-8 不经转码；PowerShell 5.1 的 cmdlet 会按 ANSI(GBK) 转码，读 UTF-8 源码
必乱。选壳顺序（`tools._git_bash_candidates`）：

1. `XCODE_BASH_HOME` 环境变量指向的目录（预留的打包/定制入口）
2. `~/.x-code/git-bash/bin/bash.exe`（预留的内置副本位置）
3. 系统安装的 Git for Windows（从 `git.exe` 推导根目录）← 绝大多数机器走这里
4. PATH 里的 bash（排除 System32 的 WSL 启动器）
5. 都没有才退回 PowerShell——但正常情况下启动门禁已经把这条路挡住了

## 各部分职责

| 文件 | 作用 |
|---|---|
| `build-exe.cmd` | 一键打包入口：装 PyInstaller → 冻结后端 → 按目标走 Tauri 或 Electron 打包 |
| `build/server/x-code-server.exe` | PyInstaller 产物（中间产物），`static/` 已打入 exe 内部 |
| `src-tauri/tauri.conf.json` | Tauri 配置：窗口、NSIS 打包、后端 exe 经 `bundle.resources` 进安装包 |
| `src-tauri/src/main.rs` | 桌面壳主体：探活/拉后端/整树杀、令牌门禁、单实例、外部链接转系统浏览器 |
| `src-tauri/icons/icon.ico` | 应用图标（由 `static/icon.png` 转制，256px PNG 直嵌 ICO） |
| `src-tauri/server/x-code-server.exe` | 打包前从 `build/server/` 复制来的后端（打包脚本自动做） |

## 常见问题

- **启动弹「Python 后端在 30 秒内未能就绪」**：一般是 8000–8019 端口全被占用，或后端
  进程启动即崩（查看 `~/.x-code/` 下的日志）。按提示关闭占用端口的程序后重试。
  机器没装 Git 时后端也会拒绝启动，具体原因看 `~/.x-code/startup-error.log`；
  装 [Git for Windows](https://git-scm.com/download/win) 后重启即可。
- **端口**：默认 8000，被占自动避让 8010–8019（实际端口见 `~/.x-code/port`）；
  可用 `--port` 参数或 `XCODE_PORT` 环境变量固定。
- **杀毒软件误报**：PyInstaller onefile 常见误报，可换 onedir（去掉 `--onefile`，并把
  tauri.conf.json 的 resources 放行目录）或对 exe 做签名。
- **改了前端代码要重新打包吗**：要。`static/` 是打进展物内部的，不是运行时从磁盘读的。
