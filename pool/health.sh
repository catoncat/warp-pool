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

# --- 资源保护 (2026-08-14 事故后加): 内存/负载过低时推迟重拨, 避免 2C2G 小机上
#     全池重拨风暴 (25×register-node.sh 重试退避) 叠加同机其他服务把整机拖挂 ---
# MemAvailable >= 300MB
mem_ok() { awk '/MemAvailable/ {exit !($2 >= 307200)}' /proc/meminfo; }
# 1min loadavg < 4.0 (双核基准, 2x 核数即视为过载)
load_ok() { awk '$1 < 4.0 {exit 0} {exit 1}' /proc/loadavg; }

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
       https://opencode.ai/inference/v1/models -o /dev/null -w "%{http_code}" 2>/dev/null || true)
  if [ "$ip" = "200" ]; then
    ok=true; ok_count=$((ok_count+1))
    rm -f "$FAIL_DIR/$port"
  else
    n=$(cat "$FAIL_DIR/$port" 2>/dev/null || echo 0); n=$((n+1))
    echo "$n" > "$FAIL_DIR/$port"
    echo "[health] node $port probe=$ip fail=$n"
    if [ "$n" -ge 3 ]; then
      if mem_ok && load_ok; then
        echo "[health] node $port re-register (new device/IP)"
        systemctl stop "wireproxy@$port" 2>/dev/null || true
        if "$POOL_ROOT/pool/register-node.sh" "$port"; then
          echo "$port" >> "$STATE_DIR/recycled"  # 注册成功才通知网关: 新 IP 新配额, 清冷却/统计
          rm -f "$FAIL_DIR/$port"
        else
          echo "[health] node $port register FAILED (old conf kept, not recycled)"
        fi
      else
        echo "[health] node $port re-register DEFERRED (mem/load guard, avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)KB)"
      fi
    fi
    systemctl restart "wireproxy@$port" 2>/dev/null || true
  fi
  out=$(python3 -c "
import json,sys
d=json.loads(sys.argv[1]); d[sys.argv[2]]={'ok':sys.argv[3]=='true','ts':int(__import__('time').time())}
print(json.dumps(d))" "$out" "$port" "$ok")
done
echo "$out" > "$STATE_DIR/nodes.json.tmp" && mv "$STATE_DIR/nodes.json.tmp" "$STATE_DIR/nodes.json"

# 配额感知提前轮换: 网关把「配额将尽/已撞 429 日冷却」的节点写 state/quota_exhausted
# (每行一个端口) → 主动重注册换新 IP(新 IP 有 2x 日配额), 而不是撞 429 后被动
# 冷到 UTC 零点白白浪费新配额。资源保护同上; 处理不完的端口写回文件下轮继续
# (网关只在流量事件时重写该文件, 直接截断会让未处理端口永久滞留)。
# 每轮限量 + 端口间节流: CF 注册 API 连续 ~6 个必 429(error 1015 封 IP), 2026-08-18 事故教训。
QUOTA_MAX_PER_RUN=4
QUOTA_PACE_SEC=50
if [ -s "$STATE_DIR/quota_exhausted" ]; then
  quota_n=0
  remain=""
  while read -r port; do
    [ -z "$port" ] && continue
    if [ "$quota_n" -ge "$QUOTA_MAX_PER_RUN" ]; then
      remain="$remain$port"$'\n'
      continue
    fi
    if mem_ok && load_ok; then
      echo "[health] node $port quota-exhausted → re-register (new IP, fresh 2x quota)"
      systemctl stop "wireproxy@$port" 2>/dev/null || true
      if "$POOL_ROOT/pool/register-node.sh" "$port"; then
        echo "$port" >> "$STATE_DIR/recycled"
        rm -f "$FAIL_DIR/$port"
      else
        echo "[health] node $port register FAILED (old conf kept, not recycled)"
      fi
      systemctl start "wireproxy@$port" 2>/dev/null || true
      quota_n=$((quota_n+1))
      sleep "$QUOTA_PACE_SEC"
    else
      echo "[health] node $port quota re-register DEFERRED (mem/load guard, avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)KB)"
      remain="$remain$port"$'\n'
    fi
  done < "$STATE_DIR/quota_exhausted"
  printf '%s' "$remain" > "$STATE_DIR/quota_exhausted"
fi

# 全池自愈: 0 健康连续两轮 → 全部重拨(新 IP 新配额, 解决整池被日配额打满/共享池污染)
if [ "$ok_count" -eq 0 ]; then
  if [ -f "$STATE_DIR/all_down" ]; then
    if mem_ok && load_ok; then
      echo "[health] ALL nodes down for 2 rounds → full re-register"
      for port in $PORTS; do systemctl stop "wireproxy@$port" 2>/dev/null || true; done
      ok_ports=""
      for port in $PORTS; do
        if "$POOL_ROOT/pool/register-node.sh" "$port"; then ok_ports="$ok_ports $port"; fi
        sleep 45  # 节流: 防 CF 注册限流
        systemctl start "wireproxy@$port" 2>/dev/null || true
      done
      rm -f "$STATE_DIR/all_down" "$FAIL_DIR"/*
      for port in $ok_ports; do echo "$port" >> "$STATE_DIR/recycled"; done  # 只清注册成功的
      systemctl restart warp-gateway  # 网关内存冷却表也重置
      echo "[health] full re-register done"
    else
      echo "[health] full re-register DEFERRED (mem/load guard); all_down marker kept"
    fi
  else
    touch "$STATE_DIR/all_down"
    echo "[health] round 1 all-down (next round triggers full re-register)"
  fi
else
  rm -f "$STATE_DIR/all_down"
fi
echo "[health] done: $ok_count / $(echo "$PORTS" | wc -l) ok"
