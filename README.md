# warp-pool

OpenAI 兼容网关 + WARP 出口节点池：把上游 OpenAI 兼容服务包装成带 key 认证的端点，用 WARP 节点池（每节点独立出口 IP）规避上游按 IP 限流，默认对接上游 0penc0de 免费模型（`/inference/v1`，自动适配免费档请求形态校验：合法 session ID、read 工具注入、流式协商，见 [gate 逆向记录](docs/UPSTREAM_GATE.md)）。

## 特性

| 特性 | 说明 |
|---|---|
| 会话指纹粘性 | 同一对话固定同一节点 → 前缀缓存稳定命中（实测 93%）；不同对话自动分散，多对话并发不挤单 IP |
| 配额感知调度 | 按节点记录日用量、从 429 学习配额上限，达 ~85% 提前轮换新 IP，不撞 429 干等 |
| 上游连接池复用 | keep-alive 复用省每请求 socks5+TLS 建连 ~0.4s |
| 并发收窄 | 信号量只锁建连阶段，长流不占槽；TTFT 超时自动换节点 |
| 高并发稳定 | listen backlog 128，40 并发 0 错误 |
| 零第三方依赖 | 纯 Python 标准库，SSE 透传、usage 记账、/stats 观测 |
| 自动运维 | 看门狗失败重拨换新 IP，全池故障整池重建 |

## 文档

| 文档 | 内容 |
|---|---|
| [部署](docs/DEPLOYMENT.md) | 前置要求、一键/手动部署、目录结构 |
| [配置与客户端](docs/CONFIG.md) | `config.json` 完整配置表、API 用法 |
| [测试](docs/TESTING.md) | 单元/集成/基准测试套件 |
| [运维](docs/OPS.md) | 看门狗、诊断日志、安全与边界、SOCKS5 代理池 |

## License

MIT，见 [LICENSE](LICENSE)。
