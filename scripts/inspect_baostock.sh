#!/usr/bin/env bash
# 只读排查：baostock 客户端到底怎么连服务器（协议/主机/端口/是否认代理）
set -u
SP=/home/hxb/miniconda3/envs/sequoia-x/lib/python3.11/site-packages/baostock
echo "==== 包内文件 ===="
ls -1 "$SP"
echo
echo "==== 连接相关（socket / connect / port）===="
/usr/bin/grep -rnE "socket\.|connect\(|setdefaulttimeout|HOST|PORT|url|http" "$SP" --include=*.py \
  | /usr/bin/grep -vE "^\s*#" | head -40
echo
echo "==== login 实现 ===="
/usr/bin/grep -rn "def login" -A 30 "$SP" --include=*.py | head -60
