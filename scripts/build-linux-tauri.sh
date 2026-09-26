#!/usr/bin/env bash
# x-code Linux Tauri 打包: PyInstaller 冻结后端 + Tauri AppImage + 更新签名
#
# 用法: ./scripts/build-linux-tauri.sh [x.y.z]
#   传版本号会先同步 package.json / tauri.conf.json / Cargo.toml /
#   pyproject.toml (scripts/set-version.js + npm version); 不传维持当前版本。
# 产物 (dist/):
#   x-code_<ver>_amd64.AppImage     免安装单文件 (+ .AppImage.sig 更新签名)
# 依赖: uv, Node.js, Rust 工具链, WebKitGTK 开发包 (workflow 里 apt 装);
#       必须在 Linux 上运行 (PyInstaller 无法跨平台构建)。
# 与 build-mac-tauri.sh 同款对齐策略: 冻结后端沿用 "x-code-server.exe"
# 文件名 (Linux 上只是名字), tauri.conf.json / main.rs 零改动。
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
cp build/server/x-code-server src-tauri/server/x-code-server.exe   # 文件名与 Windows 对齐
rm -rf src-tauri/pets
cp -R pets src-tauri/pets

echo "[4/4] Building Tauri AppImage (updater artifact signed)..."
if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  echo "[sign] Using TAURI_SIGNING_PRIVATE_KEY from environment"
else
  echo "[warn] No signing key in environment - build succeeds but produces no .sig"
fi
npm install --no-save @tauri-apps/cli@2
npx tauri build --bundles appimage

echo "Staging artifacts into dist/..."
mkdir -p dist
cp src-tauri/target/release/bundle/appimage/*.AppImage dist/
cp src-tauri/target/release/bundle/appimage/*.AppImage.sig dist/ 2>/dev/null || true
ls -1 dist/*.AppImage* 2>/dev/null || true
