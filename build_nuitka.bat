@echo off
chcp 65001 >nul
echo ========================================
echo   AI Gateway Nuitka 打包脚本
echo ========================================
echo.

:: 下载/缓存全部放 E 盘，不占 C 盘
set "NUITKA_CACHE_DIR=E:\Nuitka\cache"
set "PIP_CACHE_DIR=E:\Nuitka\pip-cache"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 找不到 .venv\Scripts\python.exe
    pause
    exit /b 1
)
if not exist gateway.ico (
    echo [错误] 找不到 gateway.ico
    pause
    exit /b 1
)

echo [1/3] 检查 MinGW 工具链（首次自动下载到 E:\Nuitka\cache，约 500MB）...
echo [2/3] 开始 Nuitka 编译（首次 5~15 分钟）...
".venv\Scripts\python.exe" -m nuitka ^
    --onefile ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico=gateway.ico ^
    --include-package=src ^
    --include-package=fastapi ^
    --include-package=uvicorn ^
    --include-package=httpx ^
    --include-package=anyio ^
    --include-package=starlette ^
    --include-package=pydantic ^
    --include-package=requests ^
    --include-package=webview ^
    --include-package=clr_loader ^
    --include-package=pythonnet ^
    --enable-plugin=pywebview ^
    --enable-plugin=multiprocessing ^
    --include-data-dir=src\gui=src\gui ^
    --include-data-files=gateway.ico=gateway.ico ^
    --mingw64 ^
    --assume-yes-for-downloads ^
    --lto=yes ^
    --work-dir=E:\Nuitka\work ^
    --output-dir=dist ^
    --output-filename="AI Gateway v1.0.8.exe" ^
    main.py

if %errorlevel% neq 0 (
    echo [错误] Nuitka 打包失败
    pause
    exit /b 1
)

echo [3/3] 完成: dist\AI Gateway v1.0.8.exe
pause
