#!/bin/bash
# 私人英语教练 —— 双击启动器
# 双击启动本地服务 + 自动打开浏览器；关掉浏览器后服务自动停止，无需打开 Qwen Code

cd "$(dirname "$0")" || exit 1

PY="$HOME/Applications/anaconda3/envs/qwen-data-video/bin/python"
PORT=8766
URL="http://127.0.0.1:${PORT}/"

echo "=========================================="
echo "  私人英语教练 · Coach Alex"
echo "=========================================="

if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✅ 服务已在运行，直接打开浏览器"
else
  echo "🚀 正在启动服务…"
  nohup "$PY" server.py > "运行日志.log" 2>&1 &
  sleep 3
  if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    echo "✅ 服务启动成功"
  else
    echo "⚠️ 服务可能启动失败，请查看同目录下的 运行日志.log"
  fi
fi

open "$URL"
echo "已打开浏览器：${URL}"
echo ""
echo "练完直接关掉浏览器即可，服务会在约 60 秒后自动停止"
echo "=========================================="
sleep 2
