#!/usr/bin/env bash
# 部署 warp-pool 到 VPS 并幂等拉起
# 用法: deploy.sh [host] [node_count] [base_port]
set -euo pipefail

HOST="${1:?usage: deploy.sh <host> [node_count] [base_port]}"
COUNT="${2:-15}"
BASE_PORT="${3:-10800}"
# 国内机器访问 GitHub 不通时设置镜像前缀，如:
#   GH_MIRROR=https://gh-proxy.com/https://github.com ./deploy/deploy.sh <host>
GH_BASE="${GH_MIRROR:-https://github.com}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE=/opt/warp-pool

# 非 root 用户先提权建目录（幂等），否则 rsync 无法写入 /opt
ssh "$HOST" "sudo mkdir -p $REMOTE && sudo chown \$(id -un) $REMOTE"

rsync -a --delete \
  --exclude state --exclude nodes --exclude 'gateway/keys.txt' \
  "$ROOT/gateway" "$ROOT/pool" "$ROOT/etc" "$HOST:$REMOTE/"

ssh "$HOST" "sudo -n COUNT=$COUNT BASE_PORT=$BASE_PORT GH_BASE=$GH_BASE bash -s" <<'EOF'
set -euo pipefail
: "${COUNT:=15}" "${BASE_PORT:=10800}" "${GH_BASE:=https://github.com}"
cd /opt/warp-pool

# wireproxy 二进制（GitHub release, 静态）
if [ ! -x bin/wireproxy ]; then
  mkdir -p bin
  curl -sSL -m 120 -o /tmp/wireproxy.tgz \
    "$GH_BASE/octeep/wireproxy/releases/download/v1.0.9/wireproxy_linux_amd64.tar.gz"
  tar xzf /tmp/wireproxy.tgz -C bin && rm -f /tmp/wireproxy.tgz
fi
chmod +x bin/wireproxy pool/*.sh

# 共享 key（不存在才生成）
[ -s gateway/keys.txt ] || openssl rand -hex 16 > gateway/keys.txt
chmod 600 gateway/keys.txt

# 开通节点池 + 看门狗
WARP_POOL_ROOT=/opt/warp-pool pool/provision.sh "$COUNT" "$BASE_PORT"

# 网关配置 node_ports 对齐实际节点
python3 - <<PY
import json
p = "/opt/warp-pool/gateway/config.json"
c = json.load(open(p))
c["node_ports"] = [$BASE_PORT + i for i in range($COUNT)]
json.dump(c, open(p, "w"), indent=2)
PY

install -m 644 etc/warp-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now warp-gateway >/dev/null
systemctl restart warp-gateway
sleep 2
echo "[deploy] gateway $(systemctl is-active warp-gateway)"
EOF

# key 落到本地 exports（不打印到终端）；非 root 用户用 sudo 读取
mkdir -p "$ROOT/exports"
ssh "$HOST" "sudo -n cat $REMOTE/gateway/keys.txt" > "$ROOT/exports/gateway-key.txt"
chmod 600 "$ROOT/exports/gateway-key.txt"
echo "[deploy] key saved: exports/gateway-key.txt"
