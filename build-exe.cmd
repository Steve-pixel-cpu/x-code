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
REM                 (auto-update: sign with .tauri\x-code.key -> .sig artifact;
REM                  then make-latest.js writes dist\latest.json for the updater)
REM Electron branch: npm install (if needed) -> electron-builder --win
REM                  (extraResources picks up build\server automatically)
REM Output: dist\x-code_<ver>_x64-setup.exe (+ .sig + latest.json for auto-update)
REM         dist\x-code Setup <ver>.exe + dist\x-code <ver>.exe portable (electron)
REM Publish a release: scripts\publish.cmd <ver> [notes]
REM Requirements: uv, Node.js (tauri-cli / electron-builder via npm);
REM               Rust toolchain (rustup) for the tauri target only
REM NOTE: keep this file ASCII-only. cmd parses it with the ANSI codepage
REM       (GBK on zh-CN systems); UTF-8 Chinese in comments/echo gets
REM       mangled and can even be executed as bogus commands.
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
  --hidden-import uvicorn.protocols.websockets ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.protocols.websockets.websockets_impl ^
  --hidden-import uvicorn.lifespan ^
  --hidden-import uvicorn.lifespan.on ^
  server.py
if errorlevel 1 exit /b 1

REM clear stale artifacts (any version) so dist\ only holds the fresh build
if not exist dist mkdir dist
del /q "dist\x-code*.exe" "dist\x-code*.exe.sig" "dist\latest.json" >nul 2>&1

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
REM Update signing. If TAURI_SIGNING_PRIVATE_KEY is already set (CI), it is
REM used as-is (set the password env var yourself too). Otherwise the local
REM key .tauri\x-code.key + password file .tauri\x-code.key.password are
REM injected. Missing key only warns: the build still works, but the version
REM cannot be shipped as an auto-update target (no .sig).
REM
REM The password MUST be a real (non-empty) value injected explicitly: when
REM the password env var is absent, tauri prompts for it interactively and
REM hangs the build; a Windows env var cannot hold an empty value ("set X="
REM deletes the variable), so an empty-password key cannot be expressed in
REM cmd at all. Both key and password file are gitignored.
if defined TAURI_SIGNING_PRIVATE_KEY (
  echo [sign] Using TAURI_SIGNING_PRIVATE_KEY from environment
  goto :sign_key_ready
)
if not exist ".tauri\x-code.key" (
  echo [warn] No signing key: .tauri\x-code.key not found and TAURI_SIGNING_PRIVATE_KEY not set
  echo [warn] This build gets no .sig - already-installed users will NOT receive it as an update.
  echo [warn] To create a key: npx tauri signer generate -w .tauri/x-code.key
  goto :sign_key_ready
)
if not exist ".tauri\x-code.key.password" (
  echo [warn] Missing .tauri\x-code.key.password - cannot sign non-interactively.
  goto :sign_key_ready
)
REM The bundler only honors TAURI_SIGNING_PRIVATE_KEY (key CONTENT, not a
REM path - TAURI_SIGNING_PRIVATE_KEY_PATH is only read by the `tauri signer`
REM subcommand). The key file is a single-line base64 blob, so set /p reads
REM it verbatim; base64/password are alnum-only, no cmd metacharacters.
set /p TAURI_SIGNING_KEY_CONTENT=<".tauri\x-code.key"
set /p TAURI_SIGNING_KEY_PASSWORD_VALUE=<".tauri\x-code.key.password"
set "TAURI_SIGNING_PRIVATE_KEY=%TAURI_SIGNING_KEY_CONTENT%"
set "TAURI_SIGNING_PRIVATE_KEY_PASSWORD=%TAURI_SIGNING_KEY_PASSWORD_VALUE%"
echo [sign] Signing with %~dp0.tauri\x-code.key ^(password from .password file, no prompts^)
:sign_key_ready
call npx tauri build
if errorlevel 1 exit /b 1

echo.
echo Copying installer into dist\...
copy /y "src-tauri\target\release\bundle\nsis\x-code_*_x64-setup.exe" dist\ >nul
if errorlevel 1 exit /b 1
REM auto-update artifacts: .sig signature + latest.json manifest (upload both
REM together with the installer to the release)
copy /y "src-tauri\target\release\bundle\nsis\x-code_*_x64-setup.exe.sig" dist\ >nul 2>&1
node scripts\make-latest.js
if errorlevel 1 echo [warn] latest.json was NOT generated (see reason above); this version cannot be an auto-update target
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
