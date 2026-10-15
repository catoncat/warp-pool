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

## 网关侧对应实现

| 条件 | 网关实现 |
|---|---|
| 1 携带 `Bearer public` | `upstream_headers()` |
| 2 近期版本 UA | `cfg["0penc0de_version"]` 默认近期版本号 |
| 3 合法 descending session | `make_session_id()` / `session_for_fp()`（同对话复用） |
| 4 强制流式 | `do_POST` 置 `stream=True`；客户端要非流式时由 `relay_aggregate()` 聚合 SSE 还原 |
| 5 注入 `read` 工具 | `inject_gate_tools()` |
| 6 不发旧专用头 | `upstream_headers()` 中已移除 |
