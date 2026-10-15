#!/usr/bin/env bash
# 只读快照: 网关 + 节点池 + 用量。不改任何状态。
set -uo pipefail
HOST="${1:?usage: live-status.sh <host>}"
REMOTE=/opt/warp-pool

ssh -o ConnectTimeout=10 "$HOST" bash -s <<'EOF'
echo "=== services ==="
systemctl is-active warp-gateway
echo "nodes: $(systemctl list-units 'wireproxy@*' --state=active --no-legend | wc -l) active"
systemctl list-units 'wireproxy@*' --no-legend | awk '{print $1, $4}' | head -20
echo "=== gateway log (tail) ==="
journalctl -u warp-gateway -n 5 --no-pager 2>/dev/null | tail -5
echo "=== nodes state ==="
cat /opt/warp-pool/state/nodes.json 2>/dev/null || echo "(无, 看门狗尚未跑过)"
EOF

KEY=$(cat "$(cd "$(dirname "$0")/.." && pwd)/exports/gateway-key.txt" 2>/dev/null || true)
if [ -n "$KEY" ]; then
  IP=$(ssh -o ConnectTimeout=10 "$HOST" 'curl -sS -m 5 ifconfig.me 2>/dev/null || hostname -I | awk "{print \$1}"' 2>/dev/null || true)
  [ -n "$IP" ] && echo "=== /stats (public $IP) ===" && \
    curl -sS -m 10 -H "Authorization: Bearer $KEY" "http://$IP:10900/stats" | head -c 800
fi
echo
