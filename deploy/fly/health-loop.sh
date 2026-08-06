#!/usr/bin/env bash
# 容器版节点看门狗(health.sh 的 systemd 版换成进程版):
# 每节点经 socks5 探测 zen /models;
# 失败→重启 wireproxy; 连续失败≥3→重新注册设备(换全新 IP)
# 全池 0 健康连续两轮→全池重拨 + 重启网关(清内存冷却表)
# 状态写 state/nodes.json 供网关跳过坏节点
set -uo pipefail

ROOT="${1:?usage: health-loop.sh <root> [base_port] [count]}"
BASE_PORT="${2:-10800}"
COUNT="${3:-5}"

STATE_DIR="$ROOT/state"
FAIL_DIR="$STATE_DIR/fails"
mkdir -p "$STATE_DIR" "$FAIL_DIR"

PORTS=()
for ((i=0; i<COUNT; i++)); do PORTS+=($((BASE_PORT+i))); done
[ "${#PORTS[@]}" -gt 0 ] || { echo "no nodes"; exit 0; }

out="{}"
ok_count=0
for port in "${PORTS[@]}"; do
  ok=false
  ip=$(curl -sS -m 15 --socks5-hostname "127.0.0.1:$port" \
       https://opencode.ai/zen/v1/models -o /dev/null -w "%{http_code}" 2>/dev/null || true)
  if [ "$ip" = "200" ]; then
    ok=true; ok_count=$((ok_count+1)); rm -f "$FAIL_DIR/$port"
  else
    n=$(cat "$FAIL_DIR/$port" 2>/dev/null || echo 0); n=$((n+1))
    echo "$n" > "$FAIL_DIR/$port"
    echo "node $port probe=$ip fail=$n"
    if [ "$n" -ge 3 ]; then
      echo "node $port re-register (new device/IP)"
      pkill -f "nodes/node-$port.conf" 2>/dev/null || true
      "$ROOT/pool/register-node.sh" "$port" || true
      sleep 2
      "$ROOT/bin/wireproxy" -c "$ROOT/nodes/node-$port.conf" &
      rm -f "$FAIL_DIR/$port"
    elif ! pkill -0 -f "nodes/node-$port.conf" 2>/dev/null; then
      # 端口没监听时 curl 必失败; 进程死了就重启
      echo "node $port restart wireproxy"
      "$ROOT/bin/wireproxy" -c "$ROOT/nodes/node-$port.conf" &
    fi
  fi
  out=$(python3 -c "
import json,sys
d=json.loads(sys.argv[1]); d[sys.argv[2]]={'ok':sys.argv[3]=='true','ts':int(__import__('time').time())}
print(json.dumps(d))" "$out" "$port" "$ok")
done
echo "$out" > "$STATE_DIR/nodes.json.tmp" && mv "$STATE_DIR/nodes.json.tmp" "$STATE_DIR/nodes.json"

# 全池自愈: 0 健康连续两轮 → 全部重拨(新 IP 新配额)
if [ "$ok_count" -eq 0 ]; then
  if [ -f "$STATE_DIR/all_down" ]; then
    echo "ALL nodes down for 2 rounds -> full re-register"
    pkill -f "nodes/node-.*\.conf" 2>/dev/null || true
    for port in "${PORTS[@]}"; do
      "$ROOT/pool/register-node.sh" "$port" || true
      sleep 3
      "$ROOT/bin/wireproxy" -c "$ROOT/nodes/node-$port.conf" &
    done
    rm -f "$STATE_DIR/all_down" "$FAIL_DIR"/*
    pkill -f "warp_gateway.py" 2>/dev/null || true  # entrypoint 有重启兜底
    echo "full re-register done"
  else
    touch "$STATE_DIR/all_down"
    echo "round 1 all-down (next round triggers full re-register)"
  fi
else
  rm -f "$STATE_DIR/all_down"
fi
echo "done: $ok_count / ${#PORTS[@]} ok"
