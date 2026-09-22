#!/usr/bin/env bash
# 步骤1：验证 SSH 认证、设置提交身份、把 origin 指向 fork
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1

echo "=== SSH 认证测试 (GitHub) ==="
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new git@github.com 2>&1 | head -3

echo
echo "=== 设置本仓库提交身份 ==="
git config user.name "hjshxb"
git config user.email "hjshxb@users.noreply.github.com"
printf 'name  = %s\n' "$(git config user.name)"
printf 'email = %s\n' "$(git config user.email)"

echo
echo "=== 把 origin 指向 fork ==="
git remote set-url origin git@github.com:hjshxb/Sequoia-X.git
git remote -v

echo
echo "=== 远端分支情况 ==="
GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" git ls-remote --heads origin 2>&1 | head -5
echo "=== done ==="
