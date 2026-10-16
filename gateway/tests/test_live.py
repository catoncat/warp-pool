#!/usr/bin/env python3
"""集成测试: 真实 warp-pool 网关端到端行为验证。

默认连真实网关与真实上游。
用法:
    python3 tests/test_live.py                    # 生产(默认)
    python3 tests/test_live.py --gateway http://host:10900 --key KEY
退出码: 0=全过; 1=有失败。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_GATEWAY = "http://127.0.0.1:10900"
# 模型 id 会随上游改名/下架而变(曾因硬编码导致整轮集成测试误报), 所以从 /v1/models 挑;
# 首选列表只影响"优先用哪个", 不在列表里就用可用的第一个
PREFERRED_MODELS = ("deepseek-v4.1-flash-free", "mimo-v2.6-flash-free")
FALLBACK_MODEL = PREFERRED_MODELS[0]

def default_key():
    # key 来源: 环境变量 WARP_TEST_KEY, 或部署时回传的 exports/gateway-key.txt
    try:
        with open(os.path.join(os.path.dirname(__file__), "../../exports/gateway-key.txt")) as f:
            k = f.read().strip()
        if k:
            return k
    except FileNotFoundError:
        pass
    return os.environ.get("WARP_TEST_KEY", "")

class LiveGateway:
    def __init__(self, gw, key):
        self.gw = gw.rstrip("/")
        self.key = key
        self.base = self.gw + "/v1"
        self._model = None

    def _do(self, url, method, body=None, headers=None, timeout=180):
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = "Bearer " + self.key
        if headers:
            h.update(headers)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            resp = urllib.request.urlopen(r, timeout=timeout)
            return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def req(self, method, path, body=None, headers=None, timeout=180):
        return self._do(self.base + path, method, body, headers, timeout)

    def req_raw(self, method, path, body=None, headers=None, timeout=180):
        """网关根路径(healthz/stats 不走 /v1)。"""
        return self._do(self.gw + path, method, body, headers, timeout)

    def model(self):
        """可用模型 id(懒发现一次)。"""
        if self._model is None:
            ids = []
            try:
                status, h, b = self.req_raw("GET", "/v1/models", None, timeout=10)
                if status == 200:
                    ids = [m.get("id", "") for m in json.loads(b).get("data", [])]
            except Exception:
                pass
            self._model = next((m for m in PREFERRED_MODELS if m in ids),
                               ids[0] if ids else FALLBACK_MODEL)
        return self._model

    def chat(self, messages, stream=False, max_tokens=32, timeout=180):
        return self.req("POST", "/chat/completions", {
            "model": self.model(),
            "stream": stream, "max_tokens": max_tokens, "messages": messages,
        }, timeout=timeout)

    def stat(self):
        status, h, b = self.req_raw("GET", "/stats", None, timeout=10)
        return json.loads(b) if status == 200 else {}

def conv_msgs(prefix_sys, first_user, turn):
    """模拟真实对话: 固定前缀 + 追加历史轮次。"""
    msgs = [{"role": "system", "content": prefix_sys},
            {"role": "user", "content": first_user}]
    for i in range(turn):
        msgs += [{"role": "assistant", "content": "第%d轮回复" % i, "reasoning_content": ""},
                 {"role": "user", "content": "第%d轮追问" % i}]
    return msgs

PASS = 0
FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s%s" % (name, ("  [" + detail + "]" if detail else "")))
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  [" + detail + "]" if detail else "")))

def sse_parse(body: bytes):
    """解析 SSE 流: 返回 (chunks, usage)。"""
    chunks, usage = [], None
    for line in body.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data: "):
            continue
        payload = line[6:]
        if payload == b"[DONE]":
            continue
        try:
            d = json.loads(payload)
            delta = (d.get("choices") or [{}])[0].get("delta", {})
            if delta.get("content"):
                chunks.append(delta["content"])
            if d.get("usage"):
                usage = d["usage"]
        except Exception:
            pass
    return "".join(chunks), usage

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default=DEFAULT_GATEWAY, help="网关地址(默认本地隧道→生产)")
    ap.add_argument("--key", default=None, help="API key(默认本地 secret)")
    args = ap.parse_args()
    key = args.key or default_key()
    if not key:
        print("错误: 未找到 API key(传 --key 或设置 WARP_TEST_KEY)"); sys.exit(1)
    g = LiveGateway(args.gateway, key)
    print("== 集成测试 vs 真实网关: %s ==" % args.gateway)

    # 1) healthz
    status, h, b = g.req_raw("GET", "/healthz", None, timeout=10)
    check("healthz 200+ok", status == 200 and b"ok" in b, str(status))

    # 2) 401 无 key(原生裸请求, 不带 Authorization)
    try:
        resp = urllib.request.urlopen(urllib.request.Request(g.gw + "/stats", method="GET"), timeout=10)
        status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    check("无 key → 401", status == 401, str(status))
    # 裸 POST 不带 Authorization(不能借 g.req, 它会自动带上 key)
    try:
        r = urllib.request.Request(g.base + "/chat/completions",
                                   data=json.dumps({"model": g.model()}).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
        resp = urllib.request.urlopen(r, timeout=10)
        status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    check("无 key chat → 401", status == 401, str(status))
    print("  (模型: %s)" % g.model())

    # 3) 非流式
    status, h, b = g.chat([{"role": "user", "content": "用一句话说你好"}], stream=False)
    ok = status == 200 and b"choices" in b
    detail = "node=%s" % h.get("X-Warp-Pool-Node", "?")
    check("非流式 200", ok, detail)

    # 4) 流式 SSE (max_tokens 足够大, thinking 模型先出 reasoning 再出 content)
    status, h, b = g.chat([{"role": "user", "content": "数到五, 用数字列出"}], stream=True, max_tokens=128)
    chunks, usage = sse_parse(b)
    check("流式 SSE 有内容+usage", status == 200 and chunks and usage is not None,
          "chars=%d usage=%s node=%s" % (len(chunks), bool(usage), h.get("X-Warp-Pool-Node", "?")))

    # 5) 会话指纹粘性: 同对话同节点; 不同对话分散
    convs = [("sys-测试对话A", "项目A的需求是什么"),
             ("sys-测试对话B", "帮我看一下配置"),
             ("sys-测试对话C", "写个小脚本")]
    nodes = {}
    for ci, (s, u) in enumerate(convs):
        nodes[ci] = []
        for turn in range(3):
            status, h, b = g.chat(conv_msgs(s, u, turn), stream=False, max_tokens=16)
            n = h.get("X-Warp-Pool-Node", "?")
            nodes[ci].append(n)
    same_conv_stable = all(len(set(v)) == 1 for v in nodes.values())
    spread = len(set(v[0] for v in nodes.values())) >= 2
    check("同对话粘同节点", same_conv_stable, "A=%s B=%s C=%s" % (nodes[0][0], nodes[1][0], nodes[2][0]))
    check("不同对话分散", spread, "节点数=%d" % len(set(v[0] for v in nodes.values())))

    # 6) 缓存命中: 同对话重复前缀 → 命中(核心承诺, 硬断言)
    #    zen 对 <500 tokens 的短请求不返回 cache 字段, 用 ~800 tokens 长文本
    long_text = ("这是一段用于验证前缀缓存的较长文本。服务器应该对重复出现的前缀命中缓存，"
                 "从而显著降低首字延迟。我们需要足够的长度才能观察到缓存字段。" * 25)
    msgs = [{"role": "system", "content": "sys-缓存测试"},
            {"role": "user", "content": long_text}]
    hits = []
    for i in range(3):
        status, h, b = g.chat(msgs, stream=False, max_tokens=8)
        try:
            u = json.loads(b).get("usage", {})
            hit = u.get("prompt_cache_hit_tokens", 0) or 0
            miss = u.get("prompt_cache_miss_tokens", 0) or 0
            if not hit and not miss:  # OpenAI 标准格式兼容
                ptd = u.get("prompt_tokens_details") or {}
                hit = ptd.get("cached_tokens", 0) or 0
                miss = max(0, (u.get("prompt_tokens") or 0) - hit)
            hits.append((hit, miss))
        except Exception:
            hits.append((0, 0))
        if i == 0:
            time.sleep(0.5)
    hit_ok = any(h[0] > 0 and h[0] > h[1] for h in hits[1:])
    check("缓存命中(重复请求)", hit_ok,
          "演进=" + ",".join("hit=%d/miss=%d" % h for h in hits))

    # 7) responses 协议: 只吃该协议的模型(如 muse-spark)经网关必须可用。
    #    实测过: 上游里只有部分模型支持 /responses, 其余走它一律 400; 而这条路径
    #    长期没有 live 覆盖, 曾被静默打坏(某次改动后 muse 全部 503)。
    status, h, b = g.req_raw("GET", "/v1/models", None, timeout=10)
    ids = [m.get("id", "") for m in json.loads(b).get("data", [])] if status == 200 else []
    resp_model = next((m for m in ids if "muse-spark" in m), None)
    ri = [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly: ok"}]}]
    if resp_model is None:
        check("responses 协议(列表无 responses 模型, 跳过)", True, "跳过")
    else:
        status, h, b = g.req("POST", "/responses",
                             {"model": resp_model, "input": ri, "stream": False,
                              "max_output_tokens": 300})
        ok, detail = False, "status=%s %s" % (status, b[:120])
        try:
            o = json.loads(b)
            txt = "".join(c.get("text", "") for it in o.get("output", [])
                          for c in (it.get("content") or []))
            # 必须是 responses 格式(object=response), 不是 chat.completion
            ok = status == 200 and o.get("object") == "response" and "ok" in txt.lower()
            detail = "status=%s body_status=%s 文本=%r" % (status, o.get("status"), txt[:30])
        except Exception:
            pass
        check("responses 非流式(只吃该协议的模型)", ok, detail)

        status, h, b = g.req("POST", "/responses",
                             {"model": resp_model, "input": ri, "stream": True,
                              "max_output_tokens": 300})
        check("responses 流式",
              status == 200 and b"response.completed" in b and b"output_text.delta" in b,
              "status=%s 首帧=%r" % (status,
                                  next((ln for ln in b.split(b"\n") if ln.startswith(b"event: ")), b"")[:40]))

    print()
    print("集成测试结果: %d 过 / %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
