@echo off
chcp 65001 >nul
echo ==========================================
echo  aftersale-exporter Windows 打包脚本
echo ==========================================

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.11+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] 安装依赖...
python -m pip install -e . pyinstaller

echo [2/4] 开始打包...
pyinstaller ^
    --name aftersale-exporter ^
    --onefile ^
    --console ^
    --add-data "seed.curl;." ^
    cli.py

echo [3/4] 复制 seed.curl 到 dist 目录...
if exist seed.curl (
    copy seed.curl dist\seed.curl
)

echo [4/4] 打包完成！
echo.
echo 可执行文件位于: dist\aftersale-exporter.exe
echo.
echo 使用方法:
echo   dist\aftersale-exporter.exe --start "2026-04-29" --end "2026-04-30" --out-dir out
pause
