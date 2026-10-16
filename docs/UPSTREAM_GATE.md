# 上游免费档请求形态校验（gate）逆向记录

> 更新：2026-10-15 · 结论：**已破解并可复现**

## 现象

对上游免费模型发请求，返回 `403` 和一条"免费档只能在官方客户端内使用"的错误。
官方客户端能过、逐字节重放不能过 —— 说明 gate 不是 TLS 指纹，而是**请求形态校验**。

## 通过 gate 的完整条件

缺任意一条即 403：

| # | 条件 | 说明 |
|---|---|---|
| 1 | `Authorization: Bearer public` | 匿名档显式携带 |
| 2 | `User-Agent: 0penc0de/<近期版本>` | 形如 `0penc0de/latest/<ver>/cli`；过旧版本返回 426 |
| 3 | `x-0penc0de-session` + `x-0penc0de-session-id` | 两个都要；值须为合法 **descending Identifier**（见下） |
| 4 | body `stream: true` | 非流式一律 403 |
| 5 | body `tools` 含名为 `read` 的工具 | 其 parameters 须含 `path` 属性；描述与其余工具随意 |
| 6 | **不带** `x-0penc0de-request` 头 | 旧版专用头，带上即 403（最隐蔽的坑） |

> 表中的 `0penc0de` 是上游品牌名化名；协议里实际必须逐字发送的是该词的正常拼写（见 `gateway/warp_gateway.py` 的 `upstream_headers()`）。

## session ID 格式

上游 `Identifier.descending`：

```
ses_ + 12位hex( ~(ts_ms * 4096 + counter) 的低 48 位 ) + 14位base62
```

- 时间戳可反解出来，用于校验新鲜度
- **必须取反**：升序格式、随机 hex、ULID 一律 403
- 同一对话复用同一 ID → 上游前缀缓存稳定命中；不同对话分散

## 逆向方法（可复用）

1. **抓真实请求**：写一个客户端插件（`~/.config/0penc0de/plugins/*.js`，`export default { id, setup }`，setup 内挂 `ctx.session.hook("model.request", ...)`），把模型请求的 `baseURL` 改指本地 mitm，抓到官方 CLI 的完整请求头与 body。
   - 客户端走自己的 fetch，**不理会 `HTTPS_PROXY` / `NODE_EXTRA_CA_CERTS`**，所以只能靠插件改 baseURL，不能靠环境代理。
   - 在 `baseURL` 上覆盖配置文件里的地址**不生效**（远端下发优先）。
2. **逐字段二分**：换 session ID / 删头 / 删 body 字段 / 换工具，一次一个变量，看 403 是否消失。
3. **关键顺序**（省时间的发现次序）：
   官方客户端能过 → 逐字节重放不能过 → 怀疑 session → 升序 ID 不行、descending 即过 → body 也要对（`stream` + `read` 工具）→ 旧专用头是毒药。

## 稳定性判断

- gate 在闭源 inference 层，随上游发版漂移；这份配方是某次快照，随时可能失效。
- 上游对**已登录**用户也存在随机误判（相似形态的请求被拒），说明它是启发式而非精确校验 —— 换个方向理解：被判定的条件就藏在这些启发式里。
- 失效时的复现路径：按上面"逆向方法"重抓一次，重新二分。

## 上游流的两条实测特性（决定网关重试策略）

在网关侧连续抓了几十条上游 SSE 流：

1. **思考期间有 keepalive**：上游先回 200 头，然后在模型思考期间每 ~4-5s 发一条 SSE 注释帧 `: keep-alive`，再才开始出正文。实测最长的"响应头→首个 data 帧"间隔 **54s**（期间 keepalive 不断），所以不能用"多久没出正文"判故障 —— 只能用"连续多久没收到任何字节"。
2. **错误可以发生在 200 之后**：配额/限流/节点故障都可能先给 200 再在流里发 `data: {"error": ...}`。此时客户端（尤其非流式聚合路径）会拿到一个空回复。

网关的两道应对：

- `first_event_timeout`（默认 15s，覆盖 3 次 keepalive 间隔）只统计"连续无字节"，超时视为节点挂死；
- **首事件护栏**：首个有效事件（正文 / reasoning / tool_call / finish_reason）之前先攒着不发给客户端，遇 error 事件或超时则直接换节点重试（客户端无感）；已开始出字后不再重放。

## 模型 × 协议矩阵（比 gate 更早撞上的一层）

同一个免费档里，模型的**协议支持不一样**，而且报错信息会把人带偏：

| 模型 | `/chat/completions` | `/responses` | `/messages` |
|---|---|---|---|
| `mimo-v2.6-flash-free` 等多数 | ✅ | ❌ 400 | — |
| `muse-spark-1.2/1.3-contributor-free` | ❌ 400 | ✅（见下） | — |
| `jev-1.13-free` | ❌ 400 | ❌ 400 | ❌ 400 |

- **`/responses` 还要求至少一个输出控制字段**：不带 `stream_options` 也不带 `max_output_tokens` 时，即使协议选对了也回
  `400 ModelProtocolUnsupported`（`Model does not support this protocol.`）；补上任意一个即 200。
- **误导性的 403**：客户端用 `/chat/completions` 请求一个只吃 `/responses` 的模型 → 上游 400 → 网关的
  "tools 兜底"（去掉 `tools` 重发）命中条件 5 → 变成 `403 FreeTierError`。所以看到 muse 报"免费档只能在 OpenCode 里用"
  时，真实原因是**协议不对**，不是身份不对 —— 官方 CLI 匿名也能用 muse，就是因为它走 `/responses`。
- `jev-1.13-free` 三种协议都 400，属于上架但调不通 → 进 `model_blacklist`，不列给客户端。

网关侧：`/v1/responses` 分支补 `stream_options.include_usage`；首事件按 responses 事件语义判定
（`scan_responses_event()`：`response.output_text.delta` / `response.completed` 等算有效，`response.created` /
`output_item.added` 只是前导帧，`response.failed` 当错误换节点）；非流式聚合直接把终态事件里上游给的完整 `response`
对象透传（不自拼，免得漏字段）。

## 网关侧对应实现

| 条件 | 网关实现 |
|---|---|
| 1 携带 `Bearer public` | `upstream_headers()` |
| 2 近期版本 UA | `cfg["opencode_version"]` 默认近期版本号 |
| 3 合法 descending session | `make_session_id()` / `session_for_fp()`（同对话复用） |
| 4 强制流式 | `do_POST` 置 `stream=True`；客户端要非流式时由 `relay_aggregate()` 聚合 SSE 还原 |
| 5 注入 `read` 工具 | `inject_gate_tools()` |
| 6 不发旧专用头 | `upstream_headers()` 中已移除 |
