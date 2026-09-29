#!/usr/bin/env bash
# 单条本地启动命令：重置演示库 -> 播种初检/复检/退修/放行样例 -> 启动 Web 服务
# 用法: ./run.sh [端口]   默认端口 8000
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"
DATA_DIR="${MILL_DATA_DIR:-$(pwd)/data}"

exec python3 -m millinspect.app --host 127.0.0.1 --port "$PORT" \
    --data-dir "$DATA_DIR" --reset --seed
