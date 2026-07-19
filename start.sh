#!/usr/bin/env bash
# Lyrien 启动脚本
# 用法: ./start.sh

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "[lyrien] 虚拟环境不存在，正在创建..."
    python3 -m venv "$VENV_DIR"
fi

echo "[lyrien] 激活虚拟环境..."
source "$VENV_DIR/bin/activate"

if ! python -c "import fastapi" 2>/dev/null; then
    echo "[lyrien] 依赖未安装，正在安装..."
    pip install -q -e "$PROJECT_DIR"
fi

echo "[lyrien] 启动应用..."
cd "$PROJECT_DIR"
exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app
