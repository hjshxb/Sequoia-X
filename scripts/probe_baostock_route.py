"""探针：baostock 的出网路径（DNS / 出口 IP / TCP 10030 可达性 / 可选 login）。

**为什么需要它**：baostock 是**裸 TCP**（`socket.SOCK_STREAM` → `public-api.baostock.com:10030`），
既不是 HTTP 也不读 `HTTP_PROXY`/`ALL_PROXY` 环境变量（全库搜 `proxy|environ|getenv` 零命中）。
所以「挂 Clash」的默认姿势（系统代理 / HTTP 代理端口）**根本拦不到它的流量**，
出口 IP 不会变。只有 TUN 模式或透明代理才可能改变源 IP。

用法：挂代理**前后各跑一次**，对比「出口 IP」那一行是否真的变了。
    wsl.exe -d Ubuntu --cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X \\
        -- /home/hxb/miniconda3/envs/sequoia-x/bin/python scripts/probe_baostock_route.py

加 `--login` 会额外做一次真实 login —— 会占掉一次登录配额，黑名单期间别频繁跑。
"""

import socket
import sys
import time
import urllib.request

HOST = "public-api.baostock.com"
PORT = 10030

# 出口 IP 回显服务，按顺序尝试（境内优先，境外作对照）
IP_ECHO = [
    "https://myip.ipip.net",
    "https://ifconfig.me/ip",
    "https://api.ipify.org",
]


def section(title: str) -> None:
    print(f"\n── {title} ──")


def show_dns() -> tuple[bool, list[str]]:
    """返回 (是否 fake-ip, 解析到的 IP 列表)。"""
    section(f"DNS 解析 {HOST}")
    try:
        infos = socket.getaddrinfo(HOST, PORT, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        print(f"  解析失败: {exc!r}")
        return False, []
    ips = sorted({i[4][0] for i in infos})
    fake = False
    for ip in ips:
        is_fake = ip.startswith(("198.18.", "198.19."))
        fake = fake or is_fake
        print(f"  {ip}{'（Clash fake-ip 段）' if is_fake else ''}")
    if fake:
        print("  ↳ fake-ip ⇒ TUN/透明代理正在接管该域名，流量已经进了 Clash")
        print("  ↳ 也意味着：baostock 报的黑名单针对的是**当前代理出口**，不是本机直连 IP")
    return fake, ips


def show_proxy_env() -> None:
    section("代理环境变量（顺带确认：对 baostock 没用）")
    import os

    found = {
        k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
    }
    if found:
        for k, v in found.items():
            print(f"  {k}={v}")
    else:
        print("  （未设置）")
    print("  ↳ 无论设不设置，baostock 都用裸 socket 直连、不读这些变量；")
    print("    真正让流量进 Clash 的是 TUN/透明代理，不是系统代理端口")


def show_egress_ip() -> None:
    section("出口 IP（判断代理是否真的生效）")
    for url in IP_ECHO:
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                body = resp.read().decode("utf-8", "replace").strip()
            print(f"  {url} → {body[:120]}")
            return
        except Exception as exc:
            print(f"  {url} 失败: {type(exc).__name__}")
    print("  全部失败 —— 网络本身可能就不通")


def show_tcp(fake_ip: bool) -> None:
    section(f"TCP 可达性 {HOST}:{PORT}")
    t0 = time.time()
    try:
        with socket.create_connection((HOST, PORT), timeout=10) as sock:
            peer = sock.getpeername()
        elapsed = time.time() - t0
        print(f"  握手成功 → {peer}  elapsed={elapsed:.2f}s")
        if fake_ip:
            print("  ↳ ⚠️ 这是 Clash **本地代答**（fake-ip 段），握手成功≠上游可达，")
            print("     也不代表节点放行了 10030；上游是否通只能看 login 的业务响应。")
        else:
            print("  ↳ 直连握手成功 ⇒ 端口可达，若仍报黑名单则是服务端按 IP 拒绝")
    except Exception as exc:
        print(f"  握手失败: {type(exc).__name__}: {exc}  elapsed={time.time() - t0:.2f}s")
        print("  ↳ 端口不通 ⇒ 代理/节点很可能没放行 10030 这种非标准高端口")


def show_login() -> None:
    section("真实 login（占用一次登录配额）")
    import baostock as bs

    t0 = time.time()
    lg = bs.login()
    print(f"  {lg.error_code} {lg.error_msg}  elapsed={time.time() - t0:.2f}s")
    if lg.error_code == "0":
        print("  ✓ 登录成功 —— 传输链路与出口 IP 均已可用")
        bs.logout()
    else:
        print("  ↳ 能拿到业务响应码 ⇒ 说明 10030 已被成功转发、服务器能回话，")
        print("     剩下的问题只是「当前出口 IP 在黑名单里」⇒ 换节点即换 IP")


def main() -> None:
    print(f"===== baostock 出网路径探针 {time.strftime('%F %T')} =====")
    fake_ip, _ = show_dns()
    show_proxy_env()
    show_egress_ip()
    show_tcp(fake_ip)
    if "--login" in sys.argv:
        show_login()
    else:
        print("\n（未做 login；要测真实登录请加 --login）")


if __name__ == "__main__":
    main()
