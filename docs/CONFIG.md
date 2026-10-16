# 配置与客户端

## 配置说明（`gateway/config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `listen` | `0.0.0.0:10900` | 网关监听地址；只本机消费可改 `127.0.0.1:10900` |
| `node_ports` | `[]` | 节点 socks5 端口列表，部署时自动对齐；手动部署需自行填写 |
| `node_host` | `127.0.0.1` | wireproxy 监听主机 |
| `upstream_host` / `upstream_base` | 上游域名 / `/inference/v1` | 上游 OpenAI 兼容端点，可换成任意实现（真实默认值见代码常量） |
| `keys_file` | `gateway/keys.txt` | 共享 key 文件（每行一个，热加载，支持 `#` 注释） |
| `nodes_state_file` / `usage_log` | `state/nodes.json` / `state/usage.jsonl` | 健康状态 / 用量记账 |
| `max_concurrent` | `8` | 并发「建连槽」数（信号量只锁建连+等响应头阶段，长流不占槽） |
| `queue_timeout` | `15` | 排队等建连槽的最长秒数，超时 503 快速失败（不无限阻塞） |
| `ttft_timeout` | `25` | 上游首字节（TTFT）最长等待，超时换节点 |
| `first_event_timeout` | `15` | 流内「连续无字节」上限；超时当节点故障换节点（`0`=关闭）。上游在思考期间每 ~5s 发一次 `: keep-alive`，所以 15s 只打到真挂死的节点 |
| `max_attempts` | `15` | 单请求最多尝试节点数（换节点重试） |
| `connect_timeout` / `read_timeout` | `15` / `300` | 连接 / 流式读超时 |
| `max_body_bytes` | `64MB` | 请求体上限，超过返回 413（防超大 Content-Length 拖住线程） |
| `conv_table_size` | `256` | 会话指纹表上限（LRU，超限淘汰最久未用对话的粘性） |
| `default_quota` | `20` | 未知节点日配额假设（从实际 429 学习修正） |
| `quota_ratio` | `0.85` | 成功数达配额该比例视为「将尽」提前轮换 |
| `models_cache_sec` | `300` | `/v1/models` 上游结果缓存秒数 |
| `free_only` | `true` | 只透传 model id 以 `-free` 结尾的模型 |
| `model_blacklist` | `["jev-1.13-free"]` | 上游已上架但任何协议都调不通的模型，不回给 `/v1/models`（只影响列表，不拦请求） |
| `spoof_headers` | `true` | 伪装官方 CLI 请求头（协议要求的 `x-<品牌>-*` 头名逐字见代码；上游按这些头判定请求来源） |
| `opencode_version` | 见代码默认（需近期版本） | 伪装头里的版本号；过旧会被上游拒（426）。键名与 UA 前缀是协议字面量，见 `UPSTREAM_GATE.md` |

## 客户端使用

```bash
# key 在服务器 gateway/keys.txt（deploy.sh 部署后本地留档 exports/gateway-key.txt）
curl -H "Authorization: Bearer $KEY" http://your-gateway:10900/v1/models
curl -H "Authorization: Bearer $KEY" https://your.domain/v1/chat/completions \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}]}'
```

网关是标准 OpenAI 兼容端点：

- `GET /v1/models`、`POST /v1/chat/completions` / `/v1/responses` 需 `Authorization: Bearer <key>` 或 `x-api-key`（`Bearer` 大小写不敏感）；其余路径 404
- 请求体：缺 `Content-Length` / 空体 / CL 非法 → 400；超过 `max_body_bytes` → 413
- `GET /healthz` 无鉴权健康检查（进程活着即 200；池子详情看 `/stats`）
- 流式 SSE 透传（自动注入 `include_usage`）；`/stats` 返回当日用量与节点冷却状态
- 首事件护栏：上游可能先回 200 再在流里报错（配额/限流/节点坏）。首个有效事件之前不给客户端发任何字节，遇到 error 事件或连续无字节超时就**换节点重试**（客户端完全无感）；一旦开始出字就不再重放（已发出的流不可重放）
- 节点冷却按**连续失败次数指数增长**（30s→60s→120s→240s，上限 8×），成功即清零；只有明确的配额标记才锁到 UTC 零点（普通限流不当配额耗尽）
- 认证：key 来自 `keys_file`，改文件即生效（热加载）；401 区分「没带 key」与「key 无效」并附 `WWW-Authenticate`
- 终态错误如实分类：全节点被限流 → 429 `rate_limit_error`；节点故障 / 排队超时 → 503 `server_error`；两者都带 `Retry-After` 退避秒数
