#!/usr/bin/env python3
"""基准测试: 真实 warp-pool 网关性能基准(默认连本地网关)。

测量: 并发 TTFT 分布/错误率/节点分布、流式出字速率、缓存命中、连接复用。
用法:
    python3 tests/benchmark.py                          # 生产(默认)
    python3 tests/benchmark.py --concurrency 1,10,20,40 --tag "上线前"
    python3 tests/benchmark.py --stream-words 200       # 流式长内容字数
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live import LiveGateway, default_key, DEFAULT_GATEWAY, sse_parse

def pct(v, p):
    if not v:
        return 0.0
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=DEFAULT_GATEWAY)
    ap.add_argument("--key", default=None)
    ap.add_argument("--concurrency", default="1,10,20,40")
    ap.add_argument("--tag", default="", help="基准标注(如 git commit)")
    ap.add_argument("--stream-words", type=int, default=150, help="流式出字测试目标字数")
    args = ap.parse_args()
    key = args.key or default_key()
    if not key:
        print("错误: 未找到 API key"); sys.exit(1)
    g = LiveGateway(args.gateway, key)
    tag = args.tag or time.strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 60)
    print("warp-pool 基准报告  %s" % tag)
    print("网关: %s" % args.gateway)

    # ---- 0) 健康 & 状态 ----
    try:
        st = g.stat()
        pool = st.get("pool", {})
        nodes = st.get("nodes", {})
        print("节点: %d 个, 复用率=%s%% (hits=%s/misses=%s)"
              % (len(nodes), pool.get("reuse_rate", "?"),
                 pool.get("reuse_hits", "?"), pool.get("reuse_misses", "?")))
    except Exception as e:
        print("警告: /stats 不可用: %s" % e)
        pool, nodes = {}, {}

    # ---- 1) 流式出字速率 ----
    print("\n-- 流式出字(目标 %d 字) --" % args.stream_words)
    content = ("写一篇关于清晨山间景色的散文，要有画面感和细节描写，大约%d字。" % args.stream_words)
    status, h, b = g.chat([{"role": "user", "content": content}], stream=True, max_tokens=args.stream_words * 2)
    chunks, usage = sse_parse(b)
    ttft = float(h.get("X-Warp-Pool-Ttft", 0) or 0)
    print("首字=%.2fs 内容=%d字 速率=%.1f字/s 节点=%s"
          % (ttft, len(chunks), len(chunks) / max(0.1, ttft), h.get("X-Warp-Pool-Node", "?")))

    # ---- 2) 并发 TTFT ----
    print("\n-- 并发 TTFT(非流式短请求) --")
    def one(i):
        body = {"model": "deepseek-v4-flash-free", "stream": False, "max_tokens": 8,
                "messages": [{"role": "user", "content": "回复: %d" % i}]}
        t0 = time.time()
        try:
            status, h, b = g.chat(body["messages"], stream=False, max_tokens=8, timeout=120)
            return ("ok" if status == 200 else "http%d" % status, time.time() - t0,
                    h.get("X-Warp-Pool-Node", "?"))
        except Exception as e:
            return ("ERR", time.time() - t0, str(e)[:40])
    for c in [int(x) for x in args.concurrency.split(",") if x.strip()]:
        jobs = list(range(max(c, 1)))
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=max(c, 1)) as ex:
            rs = list(ex.map(one, jobs))
        wall = time.time() - t0
        ok = [r for r in rs if r[0] == "ok"]
        errs = [r for r in rs if r[0] != "ok"]
        tt = [r[1] for r in ok]
        nodes = Counter(r[2] for r in ok)
        line = "并发%-3d: ok=%d err=%d wall=%4.1fs TTFT min=%4.2f p50=%4.2f p95=%4.2f max=%4.2f" % (
            c, len(ok), len(errs), wall, pct(tt, 0), pct(tt, 0.5), pct(tt, 0.95), pct(tt, 1))
        if nodes:
            line += " 节点=%d" % len(nodes)
        print(line)
        if errs:
            print("        错误示例:", errs[:3])

    # ---- 3) 缓存命中 ----
    print("\n-- 缓存命中(同对话重复请求, ~960 tokens) --")
    long_text = ("这是一段用于验证前缀缓存的较长文本。服务器应该对重复出现的前缀命中缓存，"
                 "从而显著降低首字延迟。我们需要足够的长度才能观察到缓存字段。" * 25)
    msgs = [{"role": "system", "content": "sys-bench"}, {"role": "user", "content": long_text}]
    hits = []
    for i in range(3):
        status, h, b = g.chat(msgs, stream=False, max_tokens=8)
        u = json.loads(b).get("usage", {}) if status == 200 else {}
        hit = u.get("prompt_cache_hit_tokens", 0) or 0
        miss = u.get("prompt_cache_miss_tokens", 0) or 0
        if not hit and not miss:
            ptd = u.get("prompt_tokens_details") or {}
            hit = ptd.get("cached_tokens", 0) or 0
            miss = max(0, (u.get("prompt_tokens") or 0) - hit)
        hits.append((hit, miss))
        if i == 0:
            time.sleep(0.4)
    print("演进: " + " → ".join("hit=%d/miss=%d" % h for h in hits))
    rate = 0.0
    if hits[1] and (hits[1][0] + hits[1][1]):
        rate = 100.0 * hits[1][0] / (hits[1][0] + hits[1][1])
    print("稳态命中率: %.1f%%" % rate)

    print("=" * 60)

if __name__ == "__main__":
    main()
