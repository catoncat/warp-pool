# warp-pool

OpenAI 兼容网关 + WARP 出口节点池：把上游 OpenAI 兼容服务包装成带 key 认证的公网端点，用「每请求换一个 WARP 出口 IP」的方式规避上游**按 IP 限流**。默认上游是 opencode Zen 免费模型端点，可配置换成任意 OpenAI 兼容服务。

```
Client ──► 你的网关 :10900 (key 认证, 全局并发 N)
         → round-robin 选节点 → wireproxy@108xx socks5
         → 独立 WARP 设备出口 (每节点唯一 IP) → 上游 /v1
```

## 为什么有效（上游限流模型实测）

- 匿名限流按 **IP 按天** 计数（UTC 零点重置），新 IP 通常有 **2x 日配额**
- 每请求换一个出口 IP = 单 IP 配额难以被打满；单 IP 被限则冷却到 UTC 零点
- 看门狗对连续失败的节点**重拨注册新设备** = 换全新 IP，配额回到新 IP 档
- 全池健康归零连续两轮 → **整池重拨**（换全新一组 IP，应对整池被日配额打满）

> 说明：本项目默认伪装官方 CLI 请求头（`x-opencode-client` 等）。若上游服务端恢复对该头的校验，网关已默认发送，无需改动。

## 目录结构

| 目录 | 内容 |
|---|---|
| `gateway/` | `warp_gateway.py`（纯 Python 标准库，OpenAI 兼容，SSE 透传，usage 记账）+ `config.json` |
| `pool/` | `register-node.sh`（注册 WARP 设备）、`provision.sh`（批量开通 + systemd）、`health.sh`（看门狗） |
| `etc/` | systemd 单元：`wireproxy@.service` 模板、`warp-gateway.service`、`warp-pool-health.{service,timer}` |
| `deploy/deploy.sh` | 一键部署（rsync + systemd），幂等 |
| `ops/live-status.sh` | 只读状态快照 |

服务器侧运行时另有（**不进 git**）：`bin/wireproxy`（v1.0.9 静态二进制）、`nodes/`（每节点配置，600）、`state/`（nodes.json / usage.jsonl / fails/）。

## 前置要求

- 一台 Linux VPS（作者用 OpenCloudOS 验证；Debian/Ubuntu 同理），能访问 `api.cloudflareclient.com` 与上游端点
- 依赖：`python3`（无第三方库）、`curl`、`wireguard-tools`（`wg genkey`）、`systemd`
- 可选：域名 + Caddy/nginx 反代 TLS（网关默认明文 HTTP）
- 可选（推荐）：机器有 IPv6 连通性 —— 网关会把上游解析成 IPv6 字面量，强制走每节点唯一的 IPv6 出口，限流规避效果最好；无 IPv6 时自动回退域名出口

## 快速部署（一键）

```bash
# 把代码拷到服务器、下载 wireproxy、生成共享 key、注册节点、装 systemd 并启动
./deploy/deploy.sh <host> 15 10800
#              host 为 ssh 可达的主机（如 my-vps）；15 个节点，socks5 端口 10800 起
```

`deploy.sh` 是幂等的：已注册的节点不会重复注册，已存在的 key 不会覆盖。它会自动：

1. rsync `gateway/` `pool/` `etc/` 到服务器 `/opt/warp-pool/`（排除 `nodes/`、`state/`、`gateway/keys.txt`）
2. 若缺 `bin/wireproxy`，下载 v1.0.9 静态二进制
3. 若缺共享 key，`openssl rand -hex 16` 生成 `gateway/keys.txt`（600）并回传到本地 `exports/gateway-key.txt`
4. `provision.sh` 注册 N 个 WARP 设备、写 wireproxy 配置、启动 `wireproxy@<port>` 单元
5. 把 `config.json` 的 `node_ports` 对齐实际端口，启动 `warp-gateway.service`

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

## 配置说明（`gateway/config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `listen` | `0.0.0.0:10900` | 网关监听地址；只本机消费可改 `127.0.0.1:10900` |
| `node_ports` | `[]` | 节点 socks5 端口列表，部署时自动对齐；手动部署需自行填写 |
| `node_host` | `127.0.0.1` | wireproxy 监听主机 |
| `upstream_host` / `upstream_base` | `opencode.ai` / `/zen/v1` | 上游 OpenAI 兼容端点，可换成任意实现 |
| `keys_file` | `gateway/keys.txt` | 共享 key 文件（每行一个，热加载，支持 `#` 注释） |
| `nodes_state_file` / `usage_log` | `state/nodes.json` / `state/usage.jsonl` | 健康状态 / 用量记账 |
| `max_concurrent` | `8` | 全局并发上限（信号量） |
| `max_attempts` | `15` | 单请求最多尝试节点数（换节点重试） |
| `connect_timeout` / `read_timeout` | `15` / `300` | 连接 / 流式读超时 |
| `models_cache_sec` | `300` | `/v1/models` 上游结果缓存秒数 |
| `free_only` | `true` | 只透传 model id 以 `-free` 结尾的模型 |
| `spoof_headers` | `true` | 伪装官方 CLI 请求头 |
| `opencode_version` | `1.0.152` | 伪装头里的版本号 |

## 客户端使用

```bash
# key 在服务器 gateway/keys.txt（deploy.sh 部署后本地留档 exports/gateway-key.txt）
curl -H "Authorization: Bearer $KEY" http://your-gateway:10900/v1/models
curl -H "Authorization: Bearer $KEY" https://your.domain/v1/chat/completions \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}]}'
```

网关是标准 OpenAI 兼容端点：

- `GET /v1/chat/completions` 之外的路径返回 404；`GET /healthz` 无鉴权健康检查
- `GET /v1/models`、`POST /v1/chat/completions` 需 `Authorization: Bearer <key>` 或 `x-api-key`
- 流式 SSE 透传（自动注入 `include_usage`）；`/stats` 返回当日用量与节点冷却状态
- 认证：key 来自 `keys_file`，改文件即生效（热加载）；无 key 一律 401

## 运维

```bash
./ops/live-status.sh <host>   # 只读快照：服务、节点健康、用量
```

- 看门狗（`warp-pool-health.timer`，每 5 分钟）：探测失败 → 重启单元；连续 3 次失败 → **重拨注册新设备**（换全新 IP）；全池 0 健康连续两轮 → 整池重拨。**自动换 IP 是设计行为，不是故障**
- 节点池状态实时可查：`curl -H "Authorization: Bearer $KEY" <gateway>/stats`

## 把节点池当通用 SOCKS5 代理池

每个节点是 `127.0.0.1:108xx` 的协议无关 SOCKS5，别的服务可以直接消费。注意出口是 Cloudflare WARP IP 段，目标站对 CF IP 的策略需要实测。**默认只监听 localhost**；要对外暴露必须加认证，禁止开成公网匿名代理。

## 安全与边界

- `nodes/*.conf` 含 WireGuard 私钥、`gateway/keys.txt` 是共享 key：**只留服务器，600 权限，不进 git**（`.gitignore` 已覆盖 `nodes/`、`state/`、`gateway/keys.txt`、`exports/`）
- 免费上游有真实的日配额限制，本项目只是通过换 IP 分摊，不是无限资源；请控制用量，避免给上游造成过大压力
- 注册 WARP 设备的 API 有瞬时限流：`register-node.sh` 已内置 3s 间隔 + 重试；手动批量补注册失败就等 1 分钟再试
- `pool/register-node.sh` 里的 `CF-Client-Version` 头会随 Cloudflare 更新过期：报 403/400 时换最新版本号

## License

MIT，见 [LICENSE](LICENSE)。
