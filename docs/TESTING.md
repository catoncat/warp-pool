# 测试

三层测试套件，全部 Python 标准库（无第三方依赖）。**集成与基准默认连生产网关**（`--gateway` 指定地址，默认 `http://127.0.0.1:10900`）。

## 一键运行

```bash
python3 gateway/tests/run_tests.py             # 单元 63 + 集成 8（默认）
python3 gateway/tests/run_tests.py --bench     # 单元 + 集成 + 基准
python3 gateway/tests/run_tests.py --no-live   # 只跑单元（无网络环境）
python3 gateway/tests/run_tests.py --bench-only
```

退出码 0 = 全过；1 = 有失败。

## 单元测试（`test_nodepool.py` + `test_http_ux.py` + `test_relay.py`，63 项，无网络）

| 组 | 覆盖 |
|---|---|
| 会话指纹 | 同对话指纹稳定（历史追加不影响）、不同对话区分、多模态 content |
| NodePool | pick/空池、同对话粘性、多对话分散、配额将尽轮换、429 学习配额、冷却跳过、recycled 清态、LRU 有界、池枯竭兜底 |
| 连接池 | keep/borrow、重复 keep 关旧连接、drop、cool/recycled 清 idle |
| HTTP 边界 | 401 区分缺 key/无效 key + `WWW-Authenticate`、Bearer 大小写、`x-api-key`、空体/缺 CL/非法 CL/超大请求体、终态 429/503 分类 + `Retry-After` |
| 首事件护栏 | 首个正文前提交、流内 error 前置→不提交、前置帧后 error→不提交、空流→不提交、连续无字节超时→不提交、提交后中断→保留已发内容、提交后超时预算切回 `read_timeout`、关闭护栏即时提交；非流式聚合：正常流还原 JSON、error/空流/超时重试、出字后 error 保留答案、上游正常收尾但空回复→不重试、**TCP 分段切断的一行能拼回来** |
| 配额标记 / 冷却 | `FreeUsageLimitError`/`rateLimitExceeded`/`insufficient_quota`/中英文配额文案→真配额；普通 `slow down`/`too many requests`→不算；冷却 30→60→120→240 封顶 8×，成功与日冷却都清零连续失败计数 |
| Responses 协议 | 前导帧（`response.created`/`in_progress`/`output_item.added`）不算有效事件、`output_text.delta`/终态算、`response.failed`→换节点；流式提交时补发前导帧；非流式透传上游完整 `response` 对象；只有前导帧或失败→重试 |
| 模型列表过滤 | `-free` 过滤、`model_blacklist` 拉黑、缺 `data` 字段不炸 |

每个测试独立 NodePool（`setUp` 重建，环境变量 `WARP_POOL_STATE`/`WARP_GATEWAY_CONFIG` 指向临时目录），杜绝跨测试状态污染。

## 集成测试（`test_live.py`，8 项，真实网关）

默认 `http://127.0.0.1:10900`（生产隧道）；`--gateway`/`--key` 可覆盖。
模型 id 从 `GET /v1/models` 挑（首选列表见 `test_live.py`），不硬编码 —— 上游改过名，硬编码会让整轮集成测试假失败。

1. `GET /healthz` → 200 ok
2. 无 key `/stats`、`/chat/completions` → 401
3. 非流式 chat → 200 + choices
4. 流式 SSE → 有 content + usage
5. 同对话（固定前缀 + 轮次追加）→ `X-Warp-Pool-Node` 全部相同（硬断言）
6. 不同对话 → 节点分散 ≥ 2（硬断言）
7. 缓存命中：同 prompt ~960 tokens 重复 3 次 → 第 2/3 次 `cached_tokens` > miss（核心承诺硬断言）

> 注意：对真实上游会消耗少量日配额（约 15 请求/轮），25 节点日配额 1000+，可放心跑。

## 基准测试（`benchmark.py`）

```bash
python3 gateway/tests/benchmark.py --concurrency 1,10,20,40 --tag "2026-08-15"
```

输出：节点/复用率快照、流式出字速率、各并发档 TTFT（min/p50/p95/max）+ 错误率 + 节点分散数、缓存稳态命中率。

**参考基线（2026-08-15 生产）**：
- 缓存稳态命中率 **93%**（同对话重复请求）
- 连接复用率 **83%**（hits 55 / misses 11）
- 40 并发：0 错误，TTFT p50 4.2s / p95 7.1s，分散到 18 个节点
- 10 并发自动分散到 10 个节点（会话指纹生效）

## 已知边界

- 集成/基准依赖生产网关在线；离线时用 `--no-live` 只跑单元
- 真实上游有波动：缓存/出字速率软性报告，粘性/鉴权/流式结构硬断言
- usage 缓存字段 zen 混用两种格式（`prompt_cache_hit_tokens` 与 `prompt_tokens_details.cached_tokens`），网关与测试均已兼容
