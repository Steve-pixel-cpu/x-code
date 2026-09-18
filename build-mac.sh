#!/usr/bin/env bash
# x-code macOS 打包: PyInstaller 冻结后端 + electron-builder 出 .dmg / .zip
# 产物: dist/x-code-<版本>-<arch>.dmg (安装镜像) 与 .zip (免安装压缩包)
# 依赖: uv, Node.js/npm —— 必须在 macOS 上运行
#       (PyInstaller 与 DMG 均无法跨平台构建, Windows/Linux 上跑不了本脚本)
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/4] Syncing project venv and installing PyInstaller..."
uv sync
uv pip install --python .venv/bin/python pyinstaller

echo "[2/4] Freezing Python backend (static/ bundled, uvicorn hidden imports declared)..."
mkdir -p build/server
.venv/bin/python -m PyInstaller --noconfirm --clean --onefile \
  --name x-code-server \
  --distpath build/server --workpath build/pyinstaller --specpath build/pyinstaller \
  --add-data "$PWD/static:static" \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops \
  --hidden-import uvicorn.loops.asyncio \
  --hidden-import uvicorn.protocols \
  --hidden-import uvicorn.protocols.http \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --hidden-import uvicorn.protocols.websockets \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.protocols.websockets.websockets_impl \
  --hidden-import uvicorn.lifespan \
  --hidden-import uvicorn.lifespan.on \
  server.py

echo "[3/4] Installing npm dependencies (electron-builder)..."
npm install

echo "[4/4] Building mac app (dmg + zip, arm64 + x64, unsigned)..."
npx electron-builder --mac --arm64 --x64

echo "Done. Artifacts in dist/:"
ls -1 dist/*.dmg dist/*.zip 2>/dev/null || true
echo "未签名应用首次打开: 右键 -> 打印; 或终端执行: xattr -cr /Applications/x-code.app"
