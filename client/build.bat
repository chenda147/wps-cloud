@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo  139→WPS 传输工具 打包脚本
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

REM 安装依赖（requirements.txt 在项目根目录）
echo [1/3] 安装依赖...
pip install -r "..\requirements.txt" -q
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

REM 打包
echo [2/3] 开始打包...
pyinstaller --noconfirm --clean "139ToWPS.spec"

if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo.
echo [3/3] 打包完成!
echo 生成位置: %~dp0dist\139ToWPS.exe
echo.
pause
