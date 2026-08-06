# warp-pool on fly.io

把 warp-pool(网关 + WARP 节点池)跑在 fly.io 上的容器化版本,替代 VPS + systemd 方案。

## 为什么能白嫖

你的 fly.io 账号带 legacy 免费配额(新号已不享受):

- **3 × shared-cpu-1x 256MB** —— 只跑 1 台,绰绰有余
- **3GB Volume** —— 挂到 `/opt/warp-pool`,持久化节点私钥,重启不换 IP
- **100GB NA/EU 出口** —— 模型流量走 WARP 隧道,不占 fly 出口,实际用量极小

## 部署

```bash
cd <warp-pool 仓库根>

fly auth login

# 1. 建 app(名字决定 baseURL,见 fly.toml `app`)
fly apps create warp-pool

# 2. 建持久卷(3GB 免费额度内,region 对齐 fly.toml 的 primary_region)
fly volumes create warp_pool_data --size 3 --region nrt

# 3. (可选)用 secret 指定网关 key; 不设的话 entrypoint 会自己生成一个
fly secrets set WARP_POOL_KEY=$(openssl rand -hex 16)

# 4. 部署
fly deploy
```

## 验证

```bash
# 健康检查(无鉴权)
curl https://warp-pool.fly.dev/healthz

# 取网关 key(若没设 secret)
fly ssh console -C 'cat /opt/warp-pool/gateway/keys.txt'

# 模型列表 & 一次对话
export WARP_POOL_KEY=上面拿到的key
curl -H "Authorization: Bearer $WARP_POOL_KEY" https://warp-pool.fly.dev/v1/models
curl -H "Authorization: Bearer $WARP_POOL_KEY" https://warp-pool.fly.dev/v1/chat/completions \
  -d '{"model":"deepseek-v4-flash-free","messages":[{"role":"user","content":"hi"}]}'

# 当日用量 / 节点冷却状态
curl -H "Authorization: Bearer $WARP_POOL_KEY" https://warp-pool.fly.dev/stats
```

## 与 VPS 版的差异

| VPS(systemd) | fly.io(容器) |
|---|---|
| `deploy/deploy.sh` + `etc/*.service` | `deploy/fly/{Dockerfile,entrypoint.sh,health-loop.sh}` |
| systemd 管理 wireproxy@N 单元 | entrypoint 后台起 N 个 wireproxy 进程,看门狗循环兜底 |
| `/etc/hosts` 钉 AAAA 强制 IPv6 出口 | 不需要:网关 `upstream_ipv6()` 自己解析 AAAA 并把 IPv6 字面量塞给 wireproxy,同样每节点唯一 IPv6 出口 |
| `systemctl restart` 手动运维 | `fly logs` / `fly ssh console` / `fly deploy` |

## 调参

- **节点数**:`fly secrets set NODE_COUNT=8`(或 fly.toml `[env]`)。256MB 内存建议 ≤6,单节点 wireproxy 空闲 ~20MB。
- **区域**:`fly.toml` 的 `primary_region`,就近选 `nrt`(东京)/`sin`/`lax`。
- **看门狗间隔**:`HEALTH_EVERY=300`(秒),探测失败会自动重启节点,连续 3 次失败重拨注册(换 IP),全池 0 健康两轮整池重拨。和 VPS 版行为一致。
- **重启不换 IP**:节点私钥在卷上;要整池换新 IP 就 `fly volumes destroy warp_pool_data` 重建(或删 `/opt/warp-pool/nodes` 后重启)。

## 安全

- 网关监听 10900 只经 fly 公网入口暴露,必须带 `Authorization: Bearer <key>`(keys.txt / secret)才能用,401 兜底。
- 节点 WireGuard 私钥在卷上,勿把卷公开/删除前备份。
- 免费上游有真实日配额,换 IP 只是分摊,别滥用。
