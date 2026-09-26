#!/usr/bin/env bash
# x-code macOS Tauri 打包: PyInstaller 冻结后端 + Tauri .app/.dmg + 更新签名
#
# 用法: ./scripts/build-mac-tauri.sh [x.y.z]
#   传版本号会先同步 package.json / tauri.conf.json / Cargo.toml /
#   pyproject.toml (scripts/set-version.js + npm version); 不传维持当前版本。
# 产物 (dist/):
#   x-code_<ver>_<arch>.dmg            安装镜像 (手动安装, 未过 Gatekeeper 公证)
#   x-code_<ver>_<arch>.app.tar.gz     自动更新包 (+ .sig, 上传 Release 后可被更新器消费)
#   latest.json                        更新清单 (make-latest-multi.js 生成)
# 依赖: uv, Node.js, Rust 工具链 —— 必须在 macOS 上运行
#       (PyInstaller 无法跨平台构建, arch 跟随构建机: macos-14=arm64 / macos-13=x64)
#
# 与 Windows (build-exe.cmd tauri) 的两个刻意对齐点:
#   - 冻结后端沿用 "x-code-server.exe" 文件名 —— tauri.conf.json 的
#     resources 映射与 main.rs 的 sidecar 路径全部零改动; mac 上这只是
#     一个普通文件名, 不影响执行。
#   - 签名同样走 TAURI_SIGNING_PRIVATE_KEY / _PASSWORD 环境变量
#     (CI 从仓库 Secrets 注入); 缺失时构建照常成功, 只是没有 .sig。
# 未签名 .app 首次打开: 右键 -> 打开; 或 xattr -cr /Applications/x-code.app
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:-}"
if [ -n "$VERSION" ]; then
  echo "[0/4] Setting version $VERSION (package.json, tauri.conf.json, Cargo.toml, pyproject.toml)..."
  npm version "$VERSION" --no-git-tag-version --allow-same-version
  node scripts/set-version.js "$VERSION"
fi

echo "[1/4] Syncing project venv and installing PyInstaller..."
uv sync
uv pip install --python .venv/bin/python pyinstaller

echo "[2/4] Freezing Python backend (static/ bundled, playwright driver collected)..."
mkdir -p build/server
# --collect-all playwright: 浏览器工具的驱动 (node.exe + 脚本) 是包内数据,
# PyInstaller 静态分析看不到, 必须显式收集 —— 与 build-exe.cmd 同口径。
.venv/bin/python -m PyInstaller --noconfirm --clean --onefile \
  --name x-code-server \
  --distpath build/server --workpath build/pyinstaller --specpath build/pyinstaller \
  --add-data "$PWD/static:static" \
  --add-data "$PWD/pyproject.toml:." \
  --collect-all playwright \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols \
  --hidden-import uvicorn.protocols.http \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.protocols.websockets.websockets_impl \
  --hidden-import uvicorn.lifespan \
  --hidden-import uvicorn.lifespan.on \
  server.py

echo "[3/4] Staging backend + pets into src-tauri (Tauri resources)..."
mkdir -p src-tauri/server
cp build/server/x-code-server src-tauri/server/x-code-server.exe   # 文件名与 Windows 对齐, conf/main.rs 零改动
rm -rf src-tauri/pets
cp -R pets src-tauri/pets

echo "[4/4] Building Tauri .app/.dmg (updater artifacts signed)..."
# --bundles 覆盖 conf 的 ["nsis"]: 资源映射/图标在基础配置里已平台兼容
# (后端沿用 x-code-server.exe 文件名; WebView2Loader.dll 在 git 中; icon 含 icns)
if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "[sign] Using TAURI_SIGNING_PRIVATE_KEY from environment"
else
  echo "[warn] No signing key in environment - build succeeds but produces no .sig"
  echo "[warn] (this version cannot be an auto-update target)"
fi
# 本地安装 tauri-cli (npx 优先解析 node_modules/.bin): 全局装在 mac 的
# npm 上 bin 解析失败 —— "could not determine executable to run" (CI 实测)
npm install --no-save @tauri-apps/cli@2
npx tauri build --bundles dmg,app

echo "Staging artifacts into dist/..."
mkdir -p dist
# 带版本/架构重命名: make-latest-multi.js 按此模式归位 darwin 条目
# (macos-latest=arm64→aarch64; Intel runner=x86_64→x64)
if [ -z "$VERSION" ]; then
  VERSION=$(node -p "require('./src-tauri/tauri.conf.json').version")
fi
ARCH_RAW=$(uname -m)
case "$ARCH_RAW" in
  arm64) ARCH=aarch64 ;;
  x86_64) ARCH=x64 ;;
  *) ARCH="$ARCH_RAW" ;;
esac
cp src-tauri/target/release/bundle/dmg/*.dmg "dist/x-code_${VERSION}_${ARCH}.dmg"
cp src-tauri/target/release/bundle/macos/*.app.tar.gz "dist/x-code_${VERSION}_${ARCH}.app.tar.gz"
cp src-tauri/target/release/bundle/macos/*.app.tar.gz.sig "dist/x-code_${VERSION}_${ARCH}.app.tar.gz.sig"
ls -1 dist/*.dmg dist/*.app.tar.gz* 2>/dev/null || true
