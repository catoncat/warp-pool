#!/usr/bin/env bash
# 注册一个 WARP 设备并生成 wireproxy 配置
# 用法: register-node.sh <port>
# 输出: $POOL_ROOT/nodes/node-<port>.conf + .json (含 id/token, 供换绑参考)
set -euo pipefail

POOL_ROOT="${WARP_POOL_ROOT:-/opt/warp-pool}"
PORT="${1:?usage: register-node.sh <port>}"
NODES_DIR="$POOL_ROOT/nodes"
mkdir -p "$NODES_DIR"

PRIV=$(wg genkey)
PUB=$(echo "$PRIV" | wg pubkey)
SN=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)
RESP=""
CODE=""
# CF-Client-Version 是 WARP 注册 API 的客户端版本头，Cloudflare 更新后旧版本会失效；
# 失效时报 403/400 时，换成最新版 app 的版本号（可用抓包或社区公布的当前值）。
for attempt in 1 2 3 4; do
  # -sS 对 HTTP 错误退出码仍为 0，必须用 -w 取状态码判断，否则 429 零退避连打 (2026-08-18 事故)
  OUT=$(curl -sS -m 20 -w $'\n%{http_code}' -X POST https://api.cloudflareclient.com/v0a2483/reg \
    -H "CF-Client-Version: a-6.81-2410032352.1" \
    -H "Content-Type: application/json" \
    -d "{\"install_id\":\"\",\"tos\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)\",\"key\":\"$PUB\",\"fcm_token\":\"\",\"serial_number\":\"$SN\",\"type\":\"Android\",\"model\":\"PC\",\"locale\":\"zh_CN\"}") || true
  CODE="${OUT##*$'\n'}"
  RESP="${OUT%$'\n'*}"
  if [ "$CODE" = "200" ]; then
    break
  elif [ "$CODE" = "429" ] || [ "$CODE" = "403" ]; then
    # CF 限流(error 1015)/版本被拒: 短退避只会续封禁，60-120s 长退避
    echo "[register] attempt $attempt HTTP $CODE (rate-limited/rejected), long backoff" >&2
    sleep $((RANDOM % 60 + 60))
  else
    echo "[register] attempt $attempt failed (HTTP ${CODE:-000}), backoff" >&2
    sleep $((RANDOM % 15 + attempt * 12))  # 网络层失败: 短随机退避
  fi
done
[ "$CODE" = "200" ] && [ -n "$RESP" ] || { echo "register failed: HTTP ${CODE:-000}"; exit 1; }

RESP="$RESP" PRIV="$PRIV" PORT="$PORT" CONF="$NODES_DIR/node-$PORT.conf" \
  JSON_OUT="$NODES_DIR/node-$PORT.json" python3 - <<'EOF'
import json, os, sys

d = json.loads(os.environ["RESP"])
if "config" not in d:
    print(f"register failed: {json.dumps(d)[:300]}", file=sys.stderr)
    sys.exit(1)
cfg, peer = d["config"], d["config"]["peers"][0]
v4 = cfg["interface"]["addresses"]["v4"]
v6 = cfg["interface"]["addresses"]["v6"]
with open(os.environ["CONF"], "w") as f:
    f.write(f"""[Interface]
PrivateKey = {os.environ["PRIV"]}
Address = {v4}/32
Address = {v6}/128
DNS = 1.1.1.1
MTU = 1280

[Peer]
PublicKey = {peer["public_key"]}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {peer["endpoint"]["host"]}
PersistentKeepalive = 25

[Socks5]
BindAddress = 127.0.0.1:{os.environ["PORT"]}
""")
with open(os.environ["JSON_OUT"], "w") as f:
    json.dump({"id": d.get("id"), "token": d.get("token"),
               "v6": v6, "port": int(os.environ["PORT"])}, f, indent=2)
print(f"node-{os.environ['PORT']}: v6={v6} id={d.get('id', '?')[:8]}")
EOF
chmod 600 "$NODES_DIR/node-$PORT.conf" "$NODES_DIR/node-$PORT.json"
