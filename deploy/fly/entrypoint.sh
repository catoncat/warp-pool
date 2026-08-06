#!/usr/bin/env bash
# warp-pool 容器入口(替代 systemd)
# 1) 把镜像代码同步到卷(保留 nodes/ state/ gateway/keys.txt)
# 2) 幂等注册节点(卷上有 conf 不重复注册 → 重启不换 IP)
# 3) 起 wireproxy 节点池 + 网关(带重启兜底)
# 4) 循环跑容器版看门狗 health-loop.sh
set -uo pipefail

SRC=/app
ROOT="${WARP_POOL_ROOT:-/opt/warp-pool}"
COUNT="${NODE_COUNT:-5}"
BASE_PORT="${BASE_PORT:-10800}"
HEALTH_EVERY="${HEALTH_EVERY:-300}"

log() { echo "[entrypoint] $*"; }

# ---------- 1. 代码同步到卷(只覆盖代码, 不碰持久数据) ----------
mkdir -p "$ROOT"
cp -a "$SRC/gateway" "$SRC/pool" "$SRC/etc" "$SRC/bin" "$ROOT/"
chmod +x "$ROOT/bin/wireproxy" "$ROOT/pool/"*.sh
mkdir -p "$ROOT/nodes" "$ROOT/state"

# ---------- 2. 共享 key: 优先用 fly secret, 否则生成 ----------
if [ -n "${WARP_POOL_KEY:-}" ] && [ ! -s "$ROOT/gateway/keys.txt" ]; then
  printf '%s\n' "$WARP_POOL_KEY" > "$ROOT/gateway/keys.txt"
  log "using WARP_POOL_KEY secret"
fi
if [ ! -s "$ROOT/gateway/keys.txt" ]; then
  openssl rand -hex 16 > "$ROOT/gateway/keys.txt"
  log "generated gateway key"
fi
chmod 600 "$ROOT/gateway/keys.txt"

# ---------- 3. 注册缺失节点(幂等) ----------
for ((i=0; i<COUNT; i++)); do
  port=$((BASE_PORT+i))
  if [ ! -f "$ROOT/nodes/node-$port.conf" ]; then
    log "register node $port"
    if ! "$ROOT/pool/register-node.sh" "$port"; then
      log "register node $port failed, continuing"
    fi
    sleep 3
  fi
done

# ---------- 4. 起 wireproxy 节点 ----------
NODE_PIDS=()
for ((i=0; i<COUNT; i++)); do
  port=$((BASE_PORT+i))
  conf="$ROOT/nodes/node-$port.conf"
  if [ ! -f "$conf" ]; then
    log "skip node $port (no conf)"
    continue
  fi
  "$ROOT/bin/wireproxy" -c "$conf" &
  NODE_PIDS+=($!)
done

# ---------- 5. 网关 config: node_ports 对齐实际端口 ----------
export WARP_POOL_STATE="$ROOT/state"
export WARP_GATEWAY_CONFIG="$ROOT/gateway/config.json"
python3 - "$ROOT/gateway/config.json" "$BASE_PORT" "$COUNT" <<'PY'
import json, sys
p, base, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
c = json.load(open(p))
c["node_ports"] = [base + i for i in range(count)]
c["node_host"] = "127.0.0.1"
json.dump(c, open(p, "w"), indent=2)
PY

# ---------- 6. 网关(挂掉自动重启) ----------
(
  while :; do
    python3 "$ROOT/gateway/warp_gateway.py"
    log "gateway exited ($?), restart in 3s"
    sleep 3
  done
) &
GATEWAY_SUPER=$!

cleanup() {
  log "shutting down"
  pkill -f "warp_gateway.py" 2>/dev/null || true
  kill "${NODE_PIDS[@]}" 2>/dev/null || true
  exit 0
}
trap cleanup TERM INT

log "pool up: ${#NODE_PIDS[@]} nodes (${BASE_PORT}-$((BASE_PORT+COUNT-1)))"
while :; do
  sleep "$HEALTH_EVERY"
  "$SRC/health-loop.sh" "$ROOT" "$BASE_PORT" "$COUNT" \
    | sed 's/^/[health] /' || true
done
