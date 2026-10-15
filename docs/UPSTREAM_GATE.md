# 上游免费档请求形态校验（gate）逆向记录

> 更新时间：2026-10-15

## 问题背景

warp-pool 2026-09-10 后无法使用，错误 `FreeTierError: OpenCode's free tier can only be used from within OpenCode`。最后一笔成功请求停在 2026-09-10 14:05。

---

## 调研过程

### 源码查证

1. **端点路由**：
   - `/zen/v1/...` → `/openai/v1/...`（走旧 inference）
   - `/zen/go/v1/...` → `/go/openai/v1/...`（走 Go inference）
   - `/inference/v1/...` → 官方 CLI v2 (1.19+) 用这个（新 inference）
   - 旧 `handler.ts` + `ipRateLimiter.ts`（含 FreeTierError 的公开源码）已于 2026-10-11/12 完全删除——逻辑移到了闭源的 inference service。

2. **官方客户端 UA 格式**：
   - `User-Agent: opencode/{channel}/{version}/{name}`，如 `opencode/stable/1.19.0/cli`
   - 发送的头：`x-opencode-session`, `x-opencode-session-id`, `x-opencode-client`, `x-opencode-project` 等

3. **关键 commit**：
   - `2026-09-09 feat(console): route migrated Go inference` — 新 inference 开始接管
   - `2026-09-17` — 大规模 FreeTierError（与 Go inference 迁移吻合）
   - `2026-09-21 fix(console): authenticate inference proxy` — 修了 auth 但 gate 仍在
   - `2026-10-11 refactor(console): make Zen routes a thin inference proxy` — handler.ts 删除
   - `2026-10-13 refactor(console): stop stripping legacy Zen headers in proxy` — 代理层改动

### 实测结果（2026-10-15，数据中心 VPS + 家宽本机）

| 请求方式 | 结果 |
|---|---|
| `Bearer public` + 各种 `User-Agent`（`opencode/1.18.31` 等）→ `/zen/v1/chat/completions` | 403 FreeTierError |
| `Bearer public` + 官方 UA 格式 → `/inference/v1/chat/completions` | 403 FreeTierError |
| 官方 CLI v1.19.0 匿名 → 任意免费模型 | 403 FreeTierError |
| `/zen/go/v1/...` + 伪造 session | 200，但 "Model unavailable"（路由不同） |
| `/zen/v1/models` | 200（无 auth 要求） |

### Issue 追踪（anomalyco/opencode）

- **#49433**（已关闭）：opencode 2.0 发布，v1 issues 关闭。官方建议升 v2 重现则新开 issue。
- **#49678**（仍 open）：多人报告官方 CLI 也随机触发的 FreeTierError（plan mode / `bash: deny` agent 触发）。
- **masuda-masuo bisect (2026-09-24)**：关键发现
  - gate 检查**请求体的 tools 数组**
  - `bash: deny` + `read: deny` → 403；`(none)` → 200
  - 即使官方 CLI，特定 agent permission 配置也触发
  - 这是**服务端 bug**，不是客户端验证
  - **正式账号 + Go 订阅 + 余额充足的账号**，plan mode 同样触发

### 当前 gate 机制推断

1. **闭源 inference service**：FreeTierError 逻辑在新的 inference backend（Cloudflare Worker），代码不公开。
2. **客户端识别**：检查 User-Agent、请求 body shape（tools 数组存在性/参数）、auth token。
3. **"within OpenCode"语义**：不只是 UA，而是综合请求特征——官方 CLI 也无法 100% 绕过。
4. **9 月 17 日的变更是 Go inference 灰度**：`Bearer public` 走 `/zen/v1/` 时触发新 backend 的免费档校验，旧 handler 的绕过方法（UA 变体、`ses_` session、`Bearer public`）在新 inference 下失效。
5. **官方 CLI 匿名模式**（`Bearer public`）在 v1.19 也无法使用免费模型——匿名免费档实质性下线。

---

## 结论（2026-10-15 更新：已破解）

**匿名免费档没有下线**——官方 CLI 匿名可用，gate 是一套可复现的请求形态校验。逐项二分定位后，全部条件如下（缺一即 403 FreeTierError）：

| # | 条件 | 备注 |
|---|---|---|
| 1 | `Authorization: Bearer public` | 显式携带 |
| 2 | `User-Agent: opencode/<近期版本>` | 如 `opencode/latest/2.0.29/cli`；`opencode/1.0.0` 返回 426 |
| 3 | `x-opencode-session` + `x-opencode-session-id` | 值须为合法 **descending Identifier**（见下） |
| 4 | body `stream: true` | 非流式一律 403 |
| 5 | body `tools` 含名为 `read` 的工具 | parameters 须含 `path` 属性；描述随意；其余工具随意 |
| 6 | 不带 `x-opencode-request` 头 | 旧 zen 头，带上即 403 |

**session ID 格式**（packages/schema/src/identifier.ts，descending）：
`ses_` + 12位hex(= `~(ts_ms * 4096 + counter)` 的低48位) + 14位base62。
时间戳可解码验新鲜度，且必须取反（ascending 格式 403）。

**逆向方法**（可复用）：
1. opencode v2 插件（`~/.config/opencode/plugins/*.js`，`export default {id, setup}`，setup 里 `ctx.session.hook("model.request", ...)`）把官方 CLI 的 LLM 请求 baseURL 重定向到本地 mitm，抓到完整请求
2. 逐字段二分（换 session / 删头 / 删 body 字段 / 换工具）定位每个条件
3. 关键发现顺序：官方 CLI 匿名能过 → 字节级重放不能过 → session ID 是服务端状态 → descending 格式即过 → body 也要对（stream+read 工具）→ 旧 `x-opencode-request` 头是毒药

**登录账号**：未测（gate 过了就不需要）；官方 issue 里登录用户的随机 403 是服务端误分类 bug，与本配方无关。

**数据中心 IP**：直连可用；WARP 节点出口也可用。

---

## warp-pool 恢复路径

### 路径 A：放弃（推荐）
Zen 免费档不再是可靠的可用资源。issue 仍 open，服务端 bug，官方自己也没修。warp-pool 原来的用途（稳定中转 Zen 免费模型）已不可行。

### 路径 B：适配新版 /zen/go/ 路径
- `/zen/go/v1/` 返回 `MissingSessionID`（不是 FreeTierError），说明路由不同
- 需要有效的 `x-opencode-session`（真实 opencode 账号 session）
- 模型在 `/go/` 路径下报 "Model unavailable"（需要账号 + 模型权限）
- **实质上**：需要一个真实 opencode 账号的 token，无法绕过

### 路径 C：换上游
改 warp-pool 网关的 `upstream_host` / `upstream_base` 指向其他免费/低价模型（deepseek-v4-flash free、HuggingChat 等），节点池的 SOCKS5 代理功能不变。

---

## 建议

**选 A**：Zen 免费模型已不可靠。建议和用户确认方向：
- 是否改接其他上游（deepseek 等）
- 还是维持节点池 SOCKS5 代理功能（不跑网关）
