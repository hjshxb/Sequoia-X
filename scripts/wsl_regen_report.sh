#!/usr/bin/env bash
# 在 WSL sequoia-x 环境中用本地 SQLite 库重新生成报告
set -u
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python
PROJ=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X
cd "$PROJ" || { echo "PROJECT_NOT_FOUND"; exit 1; }
echo "=== db exists? ==="
ls -la data/sequoia_v2.db 2>&1
echo "=== regen report ==="
"$PY" scripts/regen_report.py 2>&1
echo "REGEN_EXIT=$?"
echo "=== done ==="
