#!/usr/bin/env bash
# 收尾：启用 conda 交互激活 + 清理一次性探测脚本
set -u
CONDA=/home/hxb/miniconda3/bin/conda

echo "=== conda init bash ==="
"$CONDA" init bash 2>&1 | tail -5
echo "=== 关闭 base 自动激活（避免每个新 shell 都进 base）==="
"$CONDA" config --set auto_activate_base false 2>&1

echo "=== .bashrc 中的 conda 块 ==="
grep -n -A2 "conda initialize" "$HOME/.bashrc" 2>/dev/null | head -8

echo "=== 清理一次性脚本 ==="
cd /mnt/c/Users/hxb/WorkBuddy/stock/Sequoia-X/scripts || exit 1
rm -f wsl_probe.sh wsl_probe.out wsl_find_conda.sh wsl_envs.sh wsl_create_env.sh \
      wsl_install_deps.out wsl_create_env.out
echo "保留的 WSL 脚本："
ls -1 wsl_*.sh 2>/dev/null
echo "=== done ==="
