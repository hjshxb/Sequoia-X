#!/usr/bin/env bash
# 在 WSL sequoia-x 环境中跑关键测试
set -u
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python
PROJ=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X
cd "$PROJ" || { echo "PROJECT_NOT_FOUND"; exit 1; }
echo "=== python: $("$PY" --version 2>&1) ==="
echo "=== pytest: universe_filter + html_report ==="
"$PY" -m pytest tests/test_universe_filter.py tests/test_html_report.py -q 2>&1 | tail -25
echo "PYTEST_EXIT=${PIPESTATUS[0]}"
echo "=== done ==="
