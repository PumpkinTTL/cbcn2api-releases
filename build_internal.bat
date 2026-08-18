@echo off
chcp 65001 >nul
echo ========================================
echo   AI Gateway 内部豁免版打包脚本
echo ========================================
echo.
echo [警告] 产物跳过全部授权校验，仅限内部使用，严禁外发！
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

echo [1/3] 临时开启内部豁免（build_flags.py INTERNAL_BUILD=True）...
powershell -NoProfile -Command "(Get-Content src\build_flags.py -Raw) -replace 'INTERNAL_BUILD = False','INTERNAL_BUILD = True' | Set-Content src\build_flags.py -NoNewline"
if %errorlevel% neq 0 goto :restore

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
    --include-package=clr_loader ^
    --include-package=pythonnet ^
    --include-package=websocket ^
    --include-module=pythoncom ^
    --include-module=pywintypes ^
    --include-module=win32com.client ^
    --enable-plugin=pywebview ^
    --enable-plugin=multiprocessing ^
    --include-data-dir=src\gui=src\gui ^
    --include-data-files=gateway.ico=gateway.ico ^
    --include-data-files=.venv\Lib\site-packages\pywin32_system32\pythoncom312.dll=pywin32_system32\pythoncom312.dll ^
    --include-data-files=.venv\Lib\site-packages\pywin32_system32\pywintypes312.dll=pywin32_system32\pywintypes312.dll ^
    --mingw64 ^
    --assume-yes-for-downloads ^
    --output-dir=dist ^
    --output-filename="AI Gateway v1.1.2-internal.exe" ^
    main.py

if %errorlevel% neq 0 (
    echo [错误] Nuitka 打包失败
    goto :restore
)

echo [3/3] 还原豁免标志（INTERNAL_BUILD=False）...
:restore
git checkout -- src\build_flags.py 2>nul
echo 完成。产物: dist\AI Gateway v1.1.2-internal.exe（仅限内部使用，勿外发）
pause
