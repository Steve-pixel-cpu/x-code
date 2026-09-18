@echo off
REM ============================================================
REM x-code one-click packaging (Tauri):
REM   [1/4] install PyInstaller into the project venv
REM   [2/4] freeze server.py into build\server\x-code-server.exe
REM   [3/4] copy backend exe into src-tauri\server (bundle resource)
REM   [4/4] cargo tauri build -> NSIS installer
REM Output: dist\x-code_<ver>_x64-setup.exe
REM Requirements: uv, Rust toolchain (rustup), Node.js (tauri-cli via npm)
REM ============================================================
setlocal
cd /d %~dp0

echo [1/4] Installing PyInstaller into project venv...
uv pip install --python .venv\Scripts\python.exe pyinstaller
if errorlevel 1 exit /b 1

echo.
echo [2/4] Freezing Python backend (static/ bundled, uvicorn hidden imports declared)...
if not exist build\server mkdir build\server
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile ^
  --name x-code-server ^
  --distpath build\server --workpath build\pyinstaller --specpath build\pyinstaller ^
  --add-data "%~dp0static;static" ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops ^
  --hidden-import uvicorn.loops.asyncio ^
  --hidden-import uvicorn.protocols ^
  --hidden-import uvicorn.protocols.http ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.http.h11_impl ^
  --hidden-import uvicorn.protocols.websockets ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.protocols.websockets.websockets_impl ^
  --hidden-import uvicorn.lifespan ^
  --hidden-import uvicorn.lifespan.on ^
  server.py
if errorlevel 1 exit /b 1

echo.
echo [3/4] Copying backend exe into src-tauri\server ...
if not exist src-tauri\server mkdir src-tauri\server
copy /y build\server\x-code-server.exe src-tauri\server\
if errorlevel 1 exit /b 1

echo.
echo [4/4] Building desktop app (Tauri, NSIS installer)...
call npx tauri build
if errorlevel 1 exit /b 1

echo.
echo Done. Artifacts in dist\:
dir /b dist\*.exe
endlocal
