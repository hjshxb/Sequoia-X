#!/usr/bin/env bash
# 步骤2：暂存全部改动并做提交前自查
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1

echo "=== git add -A ==="
git add -A
echo "add exit=$?"

echo
echo "=== 暂存区文件数 ==="
git diff --cached --name-only | wc -l

echo "=== .env 是否被误暂存（应为空）==="
git diff --cached --name-only | grep -x '\.env' && echo "!! 警告：.env 进了暂存区" || echo "OK: .env 未暂存"

echo "=== 暂存区敏感文件名自查 ==="
git diff --cached --name-only | grep -Eic '\.env$|\.pem$|\.key$|id_rsa|credentials' || echo "OK: 无敏感文件名"

echo "=== 暂存区真实密钥模式自查 ==="
git diff --cached --no-color \
  | grep -En 'open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}|gh[pousr]_[A-Za-z0-9]{36,}|AKIA[0-9A-Z]{16}' \
  | head -5 || echo "OK: 未发现真实密钥"

echo
echo "=== 改动统计 ==="
git diff --cached --stat | tail -12

echo "=== 新增文件清单 ==="
git diff --cached --name-status --diff-filter=A | head -20
echo "=== done ==="
