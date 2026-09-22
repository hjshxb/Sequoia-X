#!/usr/bin/env bash
# 连续解析 public-api.baostock.com，看 Clash 的接管是否稳定；
# 同时看 WSL 的 DNS 走向（/etc/resolv.conf + 是否走 Windows 主机）。
set -u
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X || exit 1
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python

echo "==== WSL DNS 配置 ===="
/usr/bin/grep -vE "^\s*#" /etc/resolv.conf | /usr/bin/grep -vE "^\s*$"
echo "nameserver 是 172.x 说明经 WSL 虚拟网卡 → 由 Windows 侧解析（Clash 可劫持）"
echo

echo "==== 连续 6 次解析 ===="
"$PY" - <<'PYEOF'
import socket
for i in range(6):
    try:
        ips = sorted({x[4][0] for x in socket.getaddrinfo(
            "public-api.baostock.com", 10030, proto=socket.IPPROTO_TCP)})
    except Exception as e:
        ips = [f"ERR {e!r}"]
    tag = "fake-ip(Clash接管中)" if any(p.startswith(("198.18.", "198.19.")) for p in ips) else "真实IP(未经Clash)"
    print(f"  #{i+1}  {ips}  → {tag}")
PYEOF
echo
echo "==== 出口 IP 连续 3 次（看是否漂移 ⇒ 是否在走节点）===="
"$PY" - <<'PYEOF'
import urllib.request
for i in range(3):
    try:
        with urllib.request.urlopen("https://myip.ipip.net", timeout=8) as r:
            print("  #%d  %s" % (i + 1, r.read().decode("utf-8", "replace").strip()))
    except Exception as e:
        print("  #%d  失败 %s" % (i + 1, type(e).__name__))
PYEOF
