#!/usr/bin/env bash
# 探测 Sequoia-X 仓库状态 / SSH key / GitHub 连通性
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1

echo "=== git version ==="
git --version
echo "=== user.name / user.email ==="
git config user.name  || echo "(未设置)"
git config user.email || echo "(未设置)"
echo "=== remote -v ==="
git remote -v || echo "(无 remote)"
echo "=== 当前分支 ==="
git branch -vv
echo "=== 未提交改动统计 ==="
git status --short | wc -l
echo "=== 未提交改动明细 ==="
git status --short
echo "=== 最近 5 次提交 ==="
git log --oneline -5 2>/dev/null || echo "(无提交历史)"

echo
echo "=== WSL 里的 SSH key ==="
ls -la "$HOME/.ssh" 2>/dev/null || echo "WSL 无 ~/.ssh"
echo "=== Windows 侧的 SSH key（经 /mnt/c）==="
ls -la /mnt/c/Users/hxb/.ssh 2>/dev/null || echo "Windows 无 .ssh"

echo
echo "=== 连通性: github.com:22 (SSH) ==="
( timeout 8 bash -c 'exec 3<>/dev/tcp/github.com/22' && echo GITHUB_22_OK ) 2>/dev/null || echo GITHUB_22_FAIL
echo "=== 连通性: github.com:443 (HTTPS) ==="
( timeout 8 bash -c 'exec 3<>/dev/tcp/github.com/443' && echo GITHUB_443_OK ) 2>/dev/null || echo GITHUB_443_FAIL
echo "=== 全局 git config (remote/proxy 相关) ==="
git config --global --list 2>/dev/null | grep -Ei 'proxy|url\.|user\.' || echo "(无相关全局配置)"
echo "=== done ==="
