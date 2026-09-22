#!/usr/bin/env bash
# 配置 pip 清华镜像并在 sequoia-x 环境安装 Sequoia-X 依赖
set -u
ENV=/home/hxb/miniconda3/envs/sequoia-x
PY=$ENV/bin/python
PROJ=/mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X

mkdir -p "$HOME/.config/pip"
cat > "$HOME/.config/pip/pip.conf" <<'EOF'
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple
trusted-host = pypi.tuna.tsinghua.edu.cn
timeout = 120
EOF
echo "=== pip.conf ==="
cat "$HOME/.config/pip/pip.conf"

cd "$PROJ" || { echo "PROJECT_NOT_FOUND"; exit 1; }
echo "=== pip install -e .[dev] ==="
"$PY" -m pip install -e ".[dev]" 2>&1
echo "INSTALL_EXIT=$?"

echo "=== key packages ==="
"$PY" -m pip list 2>&1 | grep -Ei "baostock|akshare|pandas|pydantic|pytest|hypothesis|rich|requests|dotenv|sequoia" 
echo "=== done ==="
