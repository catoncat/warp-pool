# 部署

## 前置要求

- 一台 Linux VPS（OpenCloudOS 验证；Debian/Ubuntu 同理），能访问 `api.cloudflareclient.com` 与上游端点
- 依赖：`python3`（无第三方库）、`curl`、`wireguard-tools`（`wg genkey`）、`systemd`
- 可选：域名 + Caddy/nginx 反代 TLS（网关默认明文 HTTP）
- 可选（推荐）：机器有 IPv6 连通性。把上游主机名在 `/etc/hosts` 钉到一个 IPv6 地址（AAAA），`wireproxy` 解析域名时命中 hosts，使每个节点走**每节点唯一的 IPv6 出口**，限流规避效果最好；不钉则走节点本机默认出口族（IPv4 多节点共享 NAT 池，容易撞限流）

## 快速部署（一键）

```bash
./deploy/deploy.sh <host> 15 10800
#              host 为 ssh 可达的主机（如 my-vps）；15 个节点，socks5 端口 10800 起
```

`deploy.sh` 幂等：已注册节点不重复注册、已存在 key 不覆盖。自动完成：

1. rsync `gateway/` `pool/` `etc/` 到服务器 `/opt/warp-pool/`（排除 `nodes/`、`state/`、`gateway/keys.txt`）
2. 若缺 `bin/wireproxy`，下载 v1.0.9 静态二进制
3. 若缺共享 key，`openssl rand -hex 16` 生成 `gateway/keys.txt`（600）并回传本地 `exports/gateway-key.txt`
4. `provision.sh` 注册 N 个 WARP 设备、写 wireproxy 配置、启动 `wireproxy@<port>` 单元
5. 对齐 `config.json` 的 `node_ports`，启动 `warp-gateway.service`

## 手动部署（不用 deploy.sh 时）

```bash
# 1. 装依赖（Debian/Ubuntu 示例）
apt install -y python3 curl wireguard-tools

# 2. 上服务器，把 gateway/ pool/ etc/ 放到 /opt/warp-pool
mkdir -p /opt/warp-pool/bin
curl -sSL -o /tmp/wireproxy.tgz \
  https://github.com/octeep/wireproxy/releases/download/v1.0.9/wireproxy_linux_amd64.tar.gz
tar xzf /tmp/wireproxy.tgz -C /opt/warp-pool/bin && rm /tmp/wireproxy.tgz

# 3. 共享 key（网关认证用）
openssl rand -hex 16 > /opt/warp-pool/gateway/keys.txt
chmod 600 /opt/warp-pool/gateway/keys.txt

# 4. 注册节点 + 启动 wireproxy 单元 + 看门狗 timer
WARP_POOL_ROOT=/opt/warp-pool /opt/warp-pool/pool/provision.sh 15 10800

# 5. 网关配置 node_ports 对齐实际端口，启动网关
sed -i 's/"node_ports": \[\]/"node_ports": [10800,10801,...]/' /opt/warp-pool/gateway/config.json
install -m 644 /opt/warp-pool/etc/warp-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now warp-gateway
```

> 想自建 Caddy 反代（推荐）：`your.domain -> http://127.0.0.1:10900` 即可拿到 TLS 入口。

## 目录结构

| 目录 | 内容 |
|---|---|
| `gateway/` | `warp_gateway.py`（纯 Python 标准库，OpenAI 兼容，SSE 透传，usage 记账）+ `config.json` |
| `gateway/tests/` | 测试套件：`test_nodepool.py`（单元 18 项）、`test_live.py`（集成 8 项，默认打生产网关）、`benchmark.py`（性能基准）、`run_tests.py`（一键运行） |
| `pool/` | `register-node.sh`（注册 WARP 设备）、`provision.sh`（批量开通 + systemd）、`health.sh`（看门狗） |
| `etc/` | systemd 单元：`wireproxy@.service` 模板、`warp-gateway.service`、`warp-pool-health.{service,timer}` |
| `deploy/deploy.sh` | 一键部署（rsync + systemd），幂等 |
| `ops/live-status.sh` | 只读状态快照 |

服务器侧运行时另有（**不进 git**）：`bin/wireproxy`（v1.0.9 静态二进制）、`nodes/`（每节点配置，600）、`state/`（nodes.json / usage.jsonl / fails/）。
