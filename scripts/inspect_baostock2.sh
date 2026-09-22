#!/usr/bin/env bash
# 只读排查：服务器地址、登录请求体、是否支持代理
set -u
SP=/home/hxb/miniconda3/envs/sequoia-x/lib/python3.11/site-packages/baostock
echo "==== 服务器地址/端口 ===="
/usr/bin/grep -nE "BAOSTOCK_SERVER" "$SP/common/contants.py"
echo
echo "==== socketutil.py 全文（看有无代理支持）===="
/usr/bin/grep -nvE "^\s*$" "$SP/util/socketutil.py" | head -45
echo
echo "==== login 请求体组织（第 46-75 行）===="
/usr/bin/sed -n '46,75p' "$SP/login/loginout.py"
echo
echo "==== 是否出现 proxy / environ 字样 ===="
/usr/bin/grep -rnE "proxy|environ|getenv" "$SP" --include=*.py || echo "（无：完全不认代理环境变量）"
