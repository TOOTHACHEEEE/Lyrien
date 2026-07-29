@echo off
REM Lyrien 启动脚本（Windows 版）
REM 用法: 双击运行，或在 CMD/PowerShell 中执行 .\start.bat

setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

set "VENV_DIR=%PROJECT_DIR%\.venv"

if not exist "%VENV_DIR%\nul" (
    echo [lyrien] 虚拟环境不存在，正在创建...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [lyrien] 创建虚拟环境失败，请确认 python 已加入系统 PATH。
        exit /b 1
    )
)

echo [lyrien] 激活虚拟环境...
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo [lyrien] 激活虚拟环境失败。
    exit /b 1
)

echo [lyrien] 安装/检查依赖...
pip install -q -e "%PROJECT_DIR%"
if errorlevel 1 (
    echo [lyrien] 安装依赖失败。
    exit /b 1
)

echo [lyrien] 启动应用...
cd /d "%PROJECT_DIR%"
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app
