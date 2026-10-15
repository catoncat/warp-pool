#!/usr/bin/env bash
# 批量开通 N 个节点并安装/启动 systemd 单元
# 用法: provision.sh <count> [base_port]
set -euo pipefail

POOL_ROOT="${WARP_POOL_ROOT:-/opt/warp-pool}"
COUNT="${1:?usage: provision.sh <count> [base_port]}"
BASE_PORT="${2:-10800}"

install -m 644 "$POOL_ROOT/etc/wireproxy@.service" /etc/systemd/system/wireproxy@.service
systemctl daemon-reload

for ((i=0; i<COUNT; i++)); do
  port=$((BASE_PORT+i))
  if [ ! -f "$POOL_ROOT/nodes/node-$port.conf" ]; then
    echo "[provision] register node $port"
    "$POOL_ROOT/pool/register-node.sh" "$port"
    sleep $((RANDOM % 10 + 6))  # 随机 6-15s: 防 CF 注册限流 + 防密集注册设备落同网段
  fi
  systemctl enable --now "wireproxy@$port" >/dev/null
  echo "[provision] wireproxy@$port $(systemctl is-active wireproxy@$port)"
done

# 看门狗 timer（幂等）
install -m 644 "$POOL_ROOT/etc/warp-pool-health.service" /etc/systemd/system/
install -m 644 "$POOL_ROOT/etc/warp-pool-health.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now warp-pool-health.timer >/dev/null
echo "[provision] health timer $(systemctl is-active warp-pool-health.timer)"
