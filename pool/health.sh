#!/usr/bin/env bash
# 节点看门狗: 每节点经 socks5(域名模式, hosts 钉 IPv6 → 唯一 v6 出口) 探测 zen /models;
# 失败→重启单元; 连续失败≥3→重新注册设备(换全新 IP, 顺带吃新 IP 2x 配额)
# 全池 0 健康连续两轮→全池重拨(换全新 15 个 IP)
# 状态写 state/nodes.json 供网关跳过坏节点
set -uo pipefail

POOL_ROOT="${WARP_POOL_ROOT:-/opt/warp-pool}"
STATE_DIR="$POOL_ROOT/state"
FAIL_DIR="$STATE_DIR/fails"
mkdir -p "$STATE_DIR" "$FAIL_DIR"

PORTS=$(systemctl list-units 'wireproxy@*' --state=active,failed --no-legend \
  | awk '{print $1}' | sed -E 's/wireproxy@([0-9]+)\.service/\1/')
[ -z "$PORTS" ] && { echo "[health] no nodes"; exit 0; }

out="{}"
ok_count=0
for port in $PORTS; do
  ok=false
  # --socks5-hostname: 域名目标交给 wireproxy 解析(wireproxy 不支持 IPv6 字面量 ATYP=4),
  # hosts 钉了 opencode.ai→AAAA 所以仍走每节点唯一 v6 出口, 与网关主链路同机制
  ip=$(curl -sS -m 15 --socks5-hostname "127.0.0.1:$port" \
       https://opencode.ai/zen/v1/models -o /dev/null -w "%{http_code}" 2>/dev/null || true)
  if [ "$ip" = "200" ]; then
    ok=true; ok_count=$((ok_count+1))
    rm -f "$FAIL_DIR/$port"
  else
    n=$(cat "$FAIL_DIR/$port" 2>/dev/null || echo 0); n=$((n+1))
    echo "$n" > "$FAIL_DIR/$port"
    echo "[health] node $port probe=$ip fail=$n"
    if [ "$n" -ge 3 ]; then
      echo "[health] node $port re-register (new device/IP)"
      systemctl stop "wireproxy@$port" 2>/dev/null || true
      "$POOL_ROOT/pool/register-node.sh" "$port"
      rm -f "$FAIL_DIR/$port"
    fi
    systemctl restart "wireproxy@$port" 2>/dev/null || true
  fi
  out=$(python3 -c "
import json,sys
d=json.loads(sys.argv[1]); d[sys.argv[2]]={'ok':sys.argv[3]=='true','ts':int(__import__('time').time())}
print(json.dumps(d))" "$out" "$port" "$ok")
done
echo "$out" > "$STATE_DIR/nodes.json.tmp" && mv "$STATE_DIR/nodes.json.tmp" "$STATE_DIR/nodes.json"

# 全池自愈: 0 健康连续两轮 → 全部重拨(新 IP 新配额, 解决整池被日配额打满/共享池污染)
if [ "$ok_count" -eq 0 ]; then
  if [ -f "$STATE_DIR/all_down" ]; then
    echo "[health] ALL nodes down for 2 rounds → full re-register"
    for port in $PORTS; do systemctl stop "wireproxy@$port" 2>/dev/null || true; done
    for port in $PORTS; do
      "$POOL_ROOT/pool/register-node.sh" "$port"
      sleep 3
      systemctl start "wireproxy@$port" 2>/dev/null || true
    done
    rm -f "$STATE_DIR/all_down" "$FAIL_DIR"/*
    systemctl restart warp-gateway  # 网关内存冷却表也重置
    echo "[health] full re-register done"
  else
    touch "$STATE_DIR/all_down"
    echo "[health] round 1 all-down (next round triggers full re-register)"
  fi
else
  rm -f "$STATE_DIR/all_down"
fi
echo "[health] done: $ok_count / $(echo "$PORTS" | wc -l) ok"
