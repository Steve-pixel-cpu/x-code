@echo off
REM ============================================================
REM x-code 一键打包: PyInstaller 冻结后端 + electron-builder 出桌面安装包
REM 产物: dist\x-code Setup <版本>.exe (安装版) / dist\x-code <版本>.exe (便携版)
REM 依赖: uv (Python 环境管理), Node.js/npm, 项目 .venv 已就绪
REM ============================================================
setlocal
cd /d %~dp0

echo [1/4] 安装 PyInstaller 到项目 venv...
uv pip install pyinstaller
if errorlevel 1 exit /b 1

echo.
echo [2/4] 冻结 Python 后端 (static/ 一并打入, uvicorn 懒加载模块显式声明)...
if not exist build\server mkdir build\server
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile ^
  --name x-code-server ^
  --distpath build\server --workpath build\pyinstaller --specpath build\pyinstaller ^
  --add-data "%~dp0static;static" ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.loops.asyncio ^
  --hidden-import uvicorn.protocols ^
  --hidden-import uvicorn.protocols.http ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.http.h11_impl ^
  --hidden-import uvicorn.protocols.web ^
  --hidden-import uvicorn.protocols.web.auto ^
  --hidden-import uvicorn.protocols.web.wsproto_impl ^
  --hidden-import uvicorn.lifespan ^
  --hidden-import uvicorn.lifespan.on ^
  server.py
if errorlevel 1 exit /b 1

echo.
echo [3/4] 安装 npm 依赖 (electron-builder)...
call npm install
if errorlevel 1 exit /b 1

echo.
echo [4/4] 打包桌面应用 (NSIS 安装包 + 便携版)...
call npx electron-builder --win
if errorlevel 1 exit /b 1

echo.
echo 完成! 产物在 dist\ 目录:
dir /b dist\*.exe
endlocal
