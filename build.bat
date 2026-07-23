@echo off
chcp 65001 >nul
echo ========================================
echo   AI Gateway 打包脚本
echo ========================================
echo.

:: 检查 gateway.ico
if not exist gateway.ico (
    echo [错误] 找不到 gateway.ico
    pause
    exit /b 1
)

:: 清理旧文件
echo [1/4] 清理旧构建...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

:: 打包
echo [2/4] 开始 PyInstaller 打包...
pyinstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "AI Gateway" ^
    --icon gateway.ico ^
    --add-data "gateway.ico;." ^
    --add-data "src\gui\index.html;src\gui" ^
    main.py

if %errorlevel% neq 0 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo [3/4] 打包完成
echo [4/4] 输出: dist\AI Gateway.exe
echo.
echo exe 图标 = gateway.ico
echo 窗口图标 = gateway.ico (运行时 ctypes 加载)
echo 任务栏图标 = AppUserModelID 已设置
echo.
pause
