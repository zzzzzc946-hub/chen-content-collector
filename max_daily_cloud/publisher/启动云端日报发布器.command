#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR/.."

if [[ ! -f "$SCRIPT_DIR/config.json" ]]; then
  echo "缺少配置文件：$SCRIPT_DIR/config.json"
  echo "请先复制 config.example.json 为 config.json，并填写 cloud_api_base。"
  exit 1
fi

if [[ -z "${1:-}" ]]; then
  DAILY_DATE="$(date +%F)"
else
  DAILY_DATE="$1"
fi

python3 "$SCRIPT_DIR/max_daily_publisher.py" "$DAILY_DATE" --config "$SCRIPT_DIR/config.json"
