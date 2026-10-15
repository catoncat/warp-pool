# 配置与客户端

## 配置说明（`gateway/config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `listen` | `0.0.0.0:10900` | 网关监听地址；只本机消费可改 `127.0.0.1:10900` |
| `node_ports` | `[]` | 节点 socks5 端口列表，部署时自动对齐；手动部署需自行填写 |
| `node_host` | `127.0.0.1` | wireproxy 监听主机 |
| `upstream_host` / `upstream_base` | `opencode.ai` / `/inference/v1` | 上游 OpenAI 兼容端点，可换成任意实现 |
| `keys_file` | `gateway/keys.txt` | 共享 key 文件（每行一个，热加载，支持 `#` 注释） |
| `nodes_state_file` / `usage_log` | `state/nodes.json` / `state/usage.jsonl` | 健康状态 / 用量记账 |
| `max_concurrent` | `8` | 并发「建连槽」数（信号量只锁建连+等响应头阶段，长流不占槽） |
| `queue_timeout` | `15` | 排队等建连槽的最长秒数，超时 503 快速失败（不无限阻塞） |
| `ttft_timeout` | `25` | 上游首字节（TTFT）最长等待，超时换节点 |
| `max_attempts` | `15` | 单请求最多尝试节点数（换节点重试） |
| `connect_timeout` / `read_timeout` | `15` / `300` | 连接 / 流式读超时 |
| `conv_table_size` | `256` | 会话指纹表上限（LRU，超限淘汰最久未用对话的粘性） |
| `default_quota` | `20` | 未知节点日配额假设（从实际 429 学习修正） |
| `quota_ratio` | `0.85` | 成功数达配额该比例视为「将尽」提前轮换 |
| `models_cache_sec` | `300` | `/v1/models` 上游结果缓存秒数 |
| `free_only` | `true` | 只透传 model id 以 `-free` 结尾的模型 |
| `spoof_headers` | `true` | 伪装官方 CLI 请求头（`x-opencode-client` 等；上游若校验该头，网关已默认发送） |
| `opencode_version` | `1.0.152` | 伪装头里的版本号 |

## 客户端使用

```bash
# key 在服务器 gateway/keys.txt（deploy.sh 部署后本地留档 exports/gateway-key.txt）
curl -H "Authorization: Bearer $KEY" http://your-gateway:10900/v1/models
curl -H "Authorization: Bearer $KEY" https://your.domain/v1/chat/completions \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}]}'
```

网关是标准 OpenAI 兼容端点：

- `GET /v1/models`、`POST /v1/chat/completions` 需 `Authorization: Bearer <key>` 或 `x-api-key`；其余路径 404
- `GET /healthz` 无鉴权健康检查
- 流式 SSE 透传（自动注入 `include_usage`）；`/stats` 返回当日用量与节点冷却状态
- 认证：key 来自 `keys_file`，改文件即生效（热加载）；无 key 一律 401
