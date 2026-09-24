@echo off
REM ============================================================
REM x-code one-click packaging:
REM   build-exe.cmd                 -> tauri, keep current version
REM   build-exe.cmd tauri           -> Tauri 2 shell + frozen Python backend -> NSIS installer
REM   build-exe.cmd electron        -> Electron shell + frozen Python backend -> NSIS + portable
REM   build-exe.cmd both            -> run tauri, then electron
REM   build-exe.cmd <ver>           -> set version (e.g. 1.0.6), target = tauri
REM   build-exe.cmd tauri <ver>     -> set version, then build tauri
REM Setting a version syncs tauri.conf.json, Cargo.toml, pyproject.toml
REM (scripts\set-version.js) and package.json / package-lock.json (npm version).
REM Shared steps:
REM   [1/2] install PyInstaller into the project venv
REM   [2/2] freeze server.py into build\server\x-code-server.exe
REM Tauri branch:   copy backend exe into src-tauri\server -> cargo tauri build
REM Electron branch: npm install (if needed) -> electron-builder --win
REM                  (extraResources picks up build\server automatically)
REM Output: dist\x-code_<ver>_x64-setup.exe (tauri) / dist\x-code Setup <ver>.exe
REM         + dist\x-code <ver>.exe portable (electron)
REM Requirements: uv, Node.js (tauri-cli / electron-builder via npm);
REM               Rust toolchain (rustup) for the tauri target only
REM ============================================================
setlocal
cd /d %~dp0

set "TARGET=%~1"
set "VERSION=%~2"
if "%TARGET%"=="" set "TARGET=tauri"

REM allow "build-exe.cmd 1.0.6" (version as 1st arg -> default target tauri)
set "FIRST=%TARGET:~0,1%"
if "%FIRST%" GEQ "0" if "%FIRST%" LEQ "9" set "VERSION=%TARGET%"
if "%FIRST%" GEQ "0" if "%FIRST%" LEQ "9" set "TARGET=tauri"

if /i "%TARGET%"=="tauri" goto :target_ok
if /i "%TARGET%"=="electron" goto :target_ok
if /i "%TARGET%"=="both" goto :target_ok
echo Unknown target: %TARGET%
echo Usage: build-exe.cmd [tauri^|electron^|both] [x.y.z]  (default: tauri)
exit /b 1
:target_ok

if "%VERSION%"=="" goto :version_done
echo.
echo ====== [0/2] Setting version %VERSION% (tauri.conf.json, Cargo.toml, pyproject.toml, package.json) ======
node scripts\set-version.js %VERSION%
if errorlevel 1 exit /b 1
call npm version %VERSION% --no-git-tag-version --allow-same-version
if errorlevel 1 exit /b 1
:version_done

echo.
echo ====== [1/2] Installing PyInstaller into project venv ======
uv pip install --python .venv\Scripts\python.exe pyinstaller
if errorlevel 1 exit /b 1

echo.
echo ====== [2/2] Freezing Python backend (static/ bundled, uvicorn hidden imports) ======
REM --collect-all playwright: the browser tool's driver (node.exe + scripts,
REM ~100MB) is data shipped inside the package; PyInstaller's static analysis
REM cannot see it, so it must be collected explicitly, or the frozen browser_*
REM tools fail with "Executable doesn't exist" / missing driver. The desktop
REM app falls back to a local Edge/Chrome channel (see browser_tools._launch),
REM so no "playwright install" is needed.
if not exist build\server mkdir build\server
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile ^
  --name x-code-server ^
  --distpath build\server --workpath build\pyinstaller --specpath build\pyinstaller ^
  --add-data "%~dp0static;static" --add-data "%~dp0pyproject.toml;." ^
  --collect-all playwright ^
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

REM clear stale artifacts (any version) so dist\ only holds the fresh build
if not exist dist mkdir dist
del /q "dist\x-code*.exe" >nul 2>&1

if /i "%TARGET%"=="electron" goto :do_electron
if /i "%TARGET%"=="both" goto :do_tauri
:do_tauri
echo.
echo ====== Tauri [1/2] Copying backend exe into src-tauri\server ======
if not exist src-tauri\server mkdir src-tauri\server
copy /y build\server\x-code-server.exe src-tauri\server\
if errorlevel 1 exit /b 1

REM Ship desktop-pet assets with the package: pets\ -> src-tauri\pets\
REM (tauri.conf resources then installs them to <install dir>\pets\).
REM robocopy exit codes >= 8 mean failure; 1 just means "files copied" --
REM it must NOT be treated as an error exit.
robocopy pets src-tauri\pets /E /NFL /NDL /NJH /NJS >nul
if errorlevel 8 exit /b 1

echo.
echo ====== Tauri [2/2] Building desktop app (NSIS installer) ======
call npx tauri build
if errorlevel 1 exit /b 1

echo.
echo Copying installer into dist\...
copy /y "src-tauri\target\release\bundle\nsis\x-code_*_x64-setup.exe" dist\ >nul
if errorlevel 1 exit /b 1
if /i not "%TARGET%"=="both" goto :done

:do_electron
echo.
echo ====== Electron [1/2] Installing npm dependencies (skipped if present) ======
if not exist node_modules\electron-builder (
  call npm install
  if errorlevel 1 exit /b 1
)

echo.
echo ====== Electron [2/2] Building desktop app (NSIS installer + portable) ======
call npx electron-builder --win
if errorlevel 1 exit /b 1

:done
echo.
echo Done. Artifacts in dist\:
dir /b dist\*.exe
endlocal
