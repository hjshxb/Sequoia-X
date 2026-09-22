#!/usr/bin/env bash
# 步骤3：提交并推送到 fork
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1

MSG=$(mktemp)
cat > "$MSG" <<'EOF'
feat: 股票池精筛 + 本地 HTML 报告

精筛（UniverseFilter，四维度全部可选，留空即该维度不过滤）：
- 估值：流通市值 / PE(TTM) / PB
- 流动性：成交额（取自本地库，零网络开销）
- 技术面：换手率区间
- 行业：白名单 / 黑名单（子串匹配，推荐只用黑名单）

报告（notify/html_report.py）：
- 按策略分块，块内按上市板块分组（主板 → 创业板 → 科创板 → 北交所 → 其他）
- 新增 市值(亿) / 换手率 / PE(TTM) 指标列
- 页内搜索覆盖 代码 / 名称 / 行业 / 板块

配套：
- 新增 data/stock_meta.py（名称 + 行业 + 板块判定，纯代码前缀、零网络开销）
- 各策略统一走 BaseStrategy.apply_universe_filter()
- 修复 pyproject.toml 包发现（新版 setuptools 的 flat-layout 报错），补 build-system
- 测试：test_universe_filter.py + test_html_report.py（100 用例全绿）
EOF

echo "=== 提交 ==="
git commit -F "$MSG"
echo "commit exit=$?"
rm -f "$MSG"

echo
echo "=== 最近 3 次提交 ==="
git log --oneline -3

echo
echo "=== 推送到 fork (origin master) ==="
GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" git push -u origin master 2>&1
echo "push exit=$?"

echo
echo "=== 推送后状态 ==="
git status -sb | head -3
echo
echo "=== 远端确认 ==="
GIT_SSH_COMMAND="ssh -o BatchMode=yes" git ls-remote --heads origin 2>&1 | head -3
echo "=== done ==="
