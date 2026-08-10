#!/usr/bin/env bash
# LoomQ 一键启动（macOS / Linux）
#
#   ./start.sh
#
# 什么都没配也能跑：内置参考模拟器不需要任何依赖和网络，
# 三个现成的例子照样能完整走完一遍。
set -euo pipefail
cd "$(dirname "$0")"

# 解析成绝对路径：下面要 cd 进 starter_kit，相对路径会失效
if [ -x .venv/bin/python ]; then
  PY="$PWD/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo
  echo "没找到 Python。请先装 Python 3.10 或更高版本。"
  exit 1
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  echo "已读入 .env"
else
  echo
  echo "没有找到 .env，将以「只用内置模拟器」的方式启动。"
  echo "三个现成的例子可以完整跑通；想用中文自由提问或连真机，"
  echo "把 env.example.txt 复制成 .env 并填好里面的值。"
fi

echo
cd starter_kit
exec "$PY" -m loomq.web "$@"
