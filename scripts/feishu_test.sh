#!/usr/bin/env bash
# 验证 .env 里的飞书 webhook 是否可用（发一条测试消息）
set -u
PROJ=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X
PY=/home/hxb/miniconda3/envs/sequoia-x/bin/python
cd "$PROJ" || exit 1

"$PY" - <<'PYEOF'
import os, json, sys
from dotenv import load_dotenv
import requests

load_dotenv(".env")  # 显式路径：从 stdin 执行时 find_dotenv() 会因无 __file__ 而报 AssertionError
url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()

if not url:
    print("FAIL: FEISHU_WEBHOOK_URL 未配置")
    sys.exit(1)
if "your-" in url or "your_default" in url:
    print("FAIL: FEISHU_WEBHOOK_URL 仍是占位符")
    sys.exit(1)

# 只打印安全信息，不回显 token
print(f"webhook 已配置：长度 {len(url)}，域名 {url.split('/')[2]}")
print(f"token 形态：{'UUID' if len(url.split('/')[-1]) == 36 else '非 UUID（%d 字符）' % len(url.split('/')[-1])}")

payload = {
    "msg_type": "text",
    "content": {"text": "【Sequoia-X 测试】飞书推送链路正常 ✅\n来自本机 WSL 环境的连通性测试，可忽略。"},
}
try:
    r = requests.post(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, timeout=15)
    print("HTTP", r.status_code)
    body = r.json()
    code = body.get("code", body.get("StatusCode"))
    msg = body.get("msg", body.get("StatusMessage", ""))
    print(f"响应: code={code} msg={msg}")
    print("RESULT:", "OK 推送成功，请查看飞书群" if code == 0 else "FAIL 推送被拒")
except Exception as e:
    print("EXCEPTION:", type(e).__name__, str(e)[:200])
PYEOF
