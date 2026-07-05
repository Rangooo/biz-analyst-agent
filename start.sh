#!/usr/bin/env bash
# 商业分析 Agent —— 一键启动前后端
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

# ========== Python 环境 ==========
# 优先 .venv，其次环境变量 PYBIN，最后系统 python3
if [ -f "$ROOT/.venv/bin/python" ]; then
  PYBIN="$ROOT/.venv/bin/python"
elif [ -n "$PYBIN" ]; then
  :
else
  PYBIN="python3"
fi

# 检查 Python 版本 ≥ 3.10
PY_VER=$("$PYBIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "⚠ Python $PY_VER 版本过低，需要 Python 3.10+。"
  echo "  建议创建虚拟环境：python3.13 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

# 检查核心依赖是否已安装，未安装则提示
if ! "$PYBIN" -c "import fastapi, uvicorn, httpx, pydantic, yaml, sse_starlette" 2>/dev/null; then
  echo "⚠ Python 依赖未安装。正在自动安装..."
  if [ ! -f "$ROOT/.venv/bin/python" ]; then
    echo "  建议先创建虚拟环境：python3 -m venv .venv"
    echo "  然后运行：.venv/bin/pip install -r backend/requirements.txt"
  fi
  "$PYBIN" -m pip install -q -r "$ROOT/backend/requirements.txt" 2>&1 || {
    echo "⚠ 依赖安装失败，请手动运行："
    echo "  $PYBIN -m pip install -r backend/requirements.txt"
    exit 1
  }
  echo "  ✅ 依赖安装完成"
fi

# ========== Node.js 环境 ==========
if [ -z "$NODEBIN" ]; then
  if command -v node &>/dev/null; then
    NODEBIN="$(dirname "$(command -v node)")"
  elif [ -f "$HOME/.nvm/versions/node" ] && [ -d "$HOME/.nvm/versions/node" ]; then
    NODEBIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin | head -1)
  else
    echo "⚠ 未找到 Node.js，请安装 Node.js 18+ 或设置 NODEBIN 环境变量"
    echo "  https://nodejs.org/"
    exit 1
  fi
fi

# 检查 Node.js 版本 ≥ 18
NODE_VER=$("$NODEBIN/node" -v 2>/dev/null | sed 's/v//' | cut -d. -f1)
if [ -z "$NODE_VER" ] || [ "$NODE_VER" -lt 18 ]; then
  echo "⚠ Node.js 版本过低，需要 18+。当前: $("$NODEBIN/node" -v 2>/dev/null || echo '未知')"
  exit 1
fi

# ========== .env 配置 ==========
if [ ! -f "$ROOT/.env" ]; then
  echo "ℹ 未找到 .env，从 .env.example 创建（可跳过 Key 配置，进入演示模式）..."
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "  ✅ 已创建 .env（所有 Key 为空 = 演示模式，用内置数据演示完整流程）"
  echo "  💡 如需真实分析，编辑 .env 填入 DEEPSEEK_API_KEY 等密钥后重启。"
fi

# ========== 启动 ==========
echo "▶ 启动后端 (FastAPI :8000) ..."
cd "$ROOT/backend"
"$PYBIN" -m uvicorn main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

echo "▶ 启动前端 (Vite :5173) ..."
cd "$ROOT/frontend"
if [ ! -d node_modules ]; then
  "$NODEBIN/npm" install
fi
"$NODEBIN/npm" run dev &
FRONTEND_PID=$!

echo ""
echo "✅ 已启动：前端 http://localhost:5173  后端 http://127.0.0.1:8000"
echo "   按 Ctrl+C 停止。"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null" EXIT
wait
