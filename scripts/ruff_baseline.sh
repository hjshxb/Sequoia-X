#!/usr/bin/env bash
# 对比「HEAD 版本」与「工作区版本」的 ruff 报错统计，用来判定本次改动是否引入新 lint 问题。
# 关键：临时目录必须落在仓库内，否则 ruff 找不到 pyproject.toml（会 fallback 到用户级配置，
# 规则集完全不同，统计结果不可比）。
#
# 用法：bash scripts/ruff_baseline.sh
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python
TMP=.ruff_baseline_tmp
trap 'rm -rf "$TMP"' EXIT
rm -rf "$TMP"; mkdir -p "$TMP"
# 自动取「本次相对 HEAD 改动的已跟踪 .py 文件」。
# 曾经把文件名清单硬编码在这里，结果连漏两轮（刚改的 universe_filter.py / 新测试文件
# 忘了加，统计少算一整块）。`git diff HEAD` 天然只列已跟踪且被改动的文件；
# 新增（未跟踪）文件不在其中 —— 它的基线是空的，不可能「引入回归」。
FILES=$(git diff --name-only HEAD --diff-filter=d -- '*.py')
if [ -z "$FILES" ]; then
  echo "无 .py 改动，跳过"
  exit 0
fi
echo "检查文件：$FILES"
for f in $FILES; do
  mkdir -p "$TMP/$(dirname "$f")"
  # 新增文件在 HEAD 里不存在 —— 跳过（它不可能「引入回归」，因为基线是空的）
  git show "HEAD:$f" > "$TMP/$f" 2>/dev/null || rm -f "$TMP/$f"
done
echo "=== HEAD 基线统计 ==="
"$PY" -m ruff check --no-cache --config pyproject.toml --statistics "$TMP" 2>&1
echo "=== 工作区 统计 ==="
"$PY" -m ruff check --no-cache --config pyproject.toml --statistics $FILES 2>&1
