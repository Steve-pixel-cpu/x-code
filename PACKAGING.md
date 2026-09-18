# x-code 打包成 exe 指南

## 一键打包

```cmd
build-exe.cmd
```

产物在 `dist\`：

| 文件 | 说明 |
|---|---|
| `x-code Setup <版本>.exe` | NSIS 安装包（可选安装目录、创建桌面快捷方式） |
| `x-code <版本>.exe` | 便携版，单个 exe 双击即用 |

两个包都是 **Electron 壳 + PyInstaller 冻结的 Python 后端** 组合：启动时 Electron 拉起
`resources\server\x-code-server.exe`（监听 127.0.0.1:8000），窗口加载本地服务；如果
8000 端口已有 x-code 在跑则直接复用，不重复拉进程。

## 分发给别人时要带的配置

后端读取 `.env` 的顺序（`server.py` 启动检查）：

1. 项目根 `.env`（开发态）
2. `~/.x-code/.env`（打包态，**必配**）

所以拿到 exe 的人需要在 `C:\Users\<用户名>\.x-code\.env` 里写：

```
API_KEY=sk-...
```

会话记录、多 agent 存档同样存于 `~/.x-code\`，与 exe 安装位置无关，升级覆盖安装不丢数据。

## 各部分职责

| 文件 | 作用 |
|---|---|
| `build-exe.cmd` | 一键打包入口：装 PyInstaller → 冻结后端 → npm install → electron-builder |
| `build/server/x-code-server.exe` | PyInstaller 产物（中间产物），`static/` 已打入 exe 内部 |
| `package.json` → `build` | electron-builder 配置：后端 exe 经 `extraResources` 进安装包，窗口图标 `build/icon.ico` |
| `electron/main.js` | `app.isPackaged` 时自动改拉冻结后端，cwd 指向 `~/.x-code`；开发态仍用 `.venv` 的 `python server.py` |

## 常见问题

- **启动弹「Python 后端在 30 秒内未能就绪」**：多见于没配 `~/.x-code/.env`，后端因缺
  `API_KEY` 自行退出。先补配置。
- **想改端口**：`electron/main.js` 的 `BASE_URL` 与 `server.py` 末尾的 `port=8000` 要一起改。
- **杀毒软件误报**：PyInstaller onefile 常见误报，可换 onedir（去掉 `--onefile`，并把
  `extraResources` 的 filter 放行目录）或对 exe 做签名。
- **改了前端代码要重新打包吗**：要。`static/` 是打进展物内部的，不是运行时从磁盘读的。
