@echo off
chcp 65001 >nul
echo ========================================
echo   AI Gateway 打包脚本
echo ========================================
echo.

:: 检查项目虚拟环境和资源
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

:: 清理旧文件
echo [1/4] 清理旧构建...
if exist build rmdir /s /q build
set "PYI_WORK=%TEMP%\AI-Gateway-PyInstaller"
if exist "%PYI_WORK%" rmdir /s /q "%PYI_WORK%"

:: 打包
echo [2/4] 开始 PyInstaller 打包...
".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --workpath "%PYI_WORK%\build" ^
    --specpath "%PYI_WORK%" ^
    --distpath "dist" ^
    --name "AI Gateway v1.1.1" ^
    --icon "%CD%\gateway.ico" ^
    --add-data "%CD%\gateway.ico;." ^
    --add-data "%CD%\src\gui\index.html;src\gui" ^
    --add-data "%CD%\src\gui\style.css;src\gui" ^
    --add-data "%CD%\src\gui\animations.css;src\gui" ^
    --add-data "%CD%\src\gui\vue.prod.js;src\gui" ^
    --add-data "%CD%\src\gui\icons;src\gui\icons" ^
    "%CD%\main.py"

if %errorlevel% neq 0 (
    if exist "%PYI_WORK%" rmdir /s /q "%PYI_WORK%"
    echo [错误] 打包失败
    pause
    exit /b 1
)

if exist "%PYI_WORK%" rmdir /s /q "%PYI_WORK%"
echo [3/4] 打包完成
echo [4/4] 输出: dist\AI Gateway v1.1.1.exe
echo.
echo exe 图标 = gateway.ico
echo 窗口图标 = gateway.ico (运行时 ctypes 加载)
echo 任务栏图标 = AppUserModelID 已设置
echo.
pause
