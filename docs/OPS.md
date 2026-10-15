# 运维

## 状态快照

```bash
./ops/live-status.sh <host>   # 只读快照：服务、节点健康、用量
```

节点池状态实时可查：`curl -H "Authorization: Bearer $KEY" <gateway>/stats`（当日用量、节点冷却、连接池复用统计）。

## 看门狗（自动换 IP）

`warp-pool-health.timer` 每 5 分钟运行：探测失败 → 重启单元；连续 3 次失败 → **重拨注册新设备**（换全新 IP）；全池 0 健康连续两轮 → 整池重拨。

> **自动换 IP 是设计行为，不是故障。** 网关读到 `state/recycled`（重拨后写入）即清该端口冷却与统计，新 IP 新配额立即可用。

## 诊断日志

`systemctl edit warp-gateway` 加 `Environment=WARP_DEBUG=1` 可开启请求形态日志（只打结构不打内容：model / thinking / 历史消息里 reasoning_content 长度），排查 400 thinking 校验等上游兼容问题时用；默认关闭。

DEBUG 下另打观测行：`[usage]`（每请求缓存命中率）、`[ttft]`（首字耗时）、`[hdr]`（会话结构）。

## 把节点池当通用 SOCKS5 代理池

每个节点是 `127.0.0.1:108xx` 的协议无关 SOCKS5，别的服务可以直接消费。注意出口是 Cloudflare WARP IP 段，目标站对 CF IP 的策略需要实测。**默认只监听 localhost**；要对外暴露必须加认证，禁止开成公网匿名代理。

## 安全与边界

- `nodes/*.conf` 含 WireGuard 私钥、`gateway/keys.txt` 是共享 key：**只留服务器，600 权限，不进 git**（`.gitignore` 已覆盖 `nodes/`、`state/`、`gateway/keys.txt`、`exports/`）
- 免费上游有真实的日配额限制，本项目只是通过换 IP 分摊，不是无限资源；请控制用量，避免给上游造成过大压力
- 注册 WARP 设备的 API 有瞬时限流：`register-node.sh` 已内置 3s 间隔 + 重试；手动批量补注册失败就等 1 分钟再试
- `pool/register-node.sh` 里的 `CF-Client-Version` 头会随 Cloudflare 更新过期：报 403/400 时换最新版本号
