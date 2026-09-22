#!/usr/bin/env bash
# 测试 .git/hooks/pre-commit 的拦截行为
set -u
export PATH="/usr/bin:/bin:/c/Windows/System32:/c/Windows:/usr/local/bin:${PATH:-}"
cd /c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1

echo "=== hook 文件 ==="
ls -l .git/hooks/pre-commit

echo
echo "=== 用例 A：暂存区干净（期望 exit 0）==="
bash .git/hooks/pre-commit; echo "exit=$?"

echo
echo "=== 用例 B：暂存区含真实飞书密钥（期望 exit 1）==="
# 这个「假密钥」刻意拆成两段拼接：写完整体就会被 .git/hooks/pre-commit
# 当真实 webhook 拦下 —— 提交这个测试脚本时会自锁（踩过）。
FAKE_HOOK="https://open.feishu.cn/open-apis/bot/v2/hook/"\
"abcdef01-2345-6789-abcd-ef0123456789"
cat > _hooktest_secret.txt <<EOF
FEISHU=$FAKE_HOOK
EOF
git add _hooktest_secret.txt
bash .git/hooks/pre-commit; echo "exit=$?"
git reset -q HEAD _hooktest_secret.txt
rm -f _hooktest_secret.txt

echo
echo "=== 用例 C：暂存区含 .env（期望 exit 1）==="
git add -f .env
bash .git/hooks/pre-commit; echo "exit=$?"
git reset -q HEAD .env

echo
echo "=== 清理后 git 状态（应无 _hooktest / .env 暂存）==="
git status --short | head -8
echo "done"
