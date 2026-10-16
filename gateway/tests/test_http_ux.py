#!/usr/bin/env python3
"""单元测试: HTTP 边界行为(401 文案 / 请求体校验 / 终态错误分类 + Retry-After)。

无网络: 直接构造 Handler 实例喂假 rfile/wfile, 桩掉 fetch_upstream 与节点池。
覆盖的是界面上会被客户端观测到、且改动过容易回归的行为。

用法: python3 gateway/tests/test_http_ux.py
"""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from email.message import Message

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_env():
    tmp = tempfile.mkdtemp(prefix="warp-pool-http-test-")
    os.environ["WARP_POOL_STATE"] = tmp
    os.environ["WARP_GATEWAY_CONFIG"] = os.path.join(tmp, "cfg.json")
    cfg = {
        "listen": "127.0.0.1:18199",
        "node_host": "127.0.0.1",
        "node_ports": [10800, 10801, 10802, 10803, 10804],
        "upstream_base": "/inference/v1",
        "upstream_host": "upstream.example",
        "max_concurrent": 8, "max_attempts": 3,
        "connect_timeout": 1, "read_timeout": 300, "ttft_timeout": 5,
        "queue_timeout": 15, "default_quota": 20, "quota_ratio": 0.85,
        "sticky_ttl": 1800, "free_only": True, "spoof_headers": True,
        "opencode_version": "latest/2.0.29/cli",
        "max_body_bytes": 1024,
        "model_blacklist": ["jev-1.13-free"],
        "keys_file": os.path.join(tmp, "keys.txt"),
        "nodes_state_file": os.path.join(tmp, "nodes.json"),
        "quota_state_file": os.path.join(tmp, "quota.json"),
        "conv_table_size": 4,
    }
    with open(os.path.join(tmp, "cfg.json"), "w") as f:
        json.dump(cfg, f)
    with open(cfg["nodes_state_file"], "w") as f:
        json.dump({str(p): {"ok": True} for p in cfg["node_ports"]}, f)
    with open(cfg["keys_file"], "w") as f:
        f.write("test-key\n")
    return tmp, cfg


class StubUsage:
    def record(self, key, model, usage, estimated=False):
        pass

    def today(self):
        return {}


class StubPool:
    """假节点池: pick 返回 None 模拟池子枯竭。"""

    def __init__(self, pick=None):
        self._pick = pick

    def pick(self, key=None, fp=None, exclude=None):
        if self._pick is None or (exclude and self._pick in exclude):
            return None
        return self._pick

    def borrow(self, port):
        return None

    def keep(self, port, conn):
        pass

    def drop(self, port, conn):
        try:
            conn.close()
        except OSError:
            pass

    def record_success(self, port):
        pass

    def cool_short(self, port, sec=60):
        pass

    def cool_daily(self, port, retry_after=None):
        pass

    def table(self):
        return {}

    def pool_stats(self):
        return {}


class FakeConn:
    def __init__(self):
        self.sock = object()

    def close(self):
        pass


class FakeResp:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}

    def read(self, n=-1):
        if n is None or n < 0:
            return self._body
        return self._body[:n]

    def read1(self, n=-1):
        return self.read(n)


def build_handler(wg, method="POST", path="/v1/chat/completions",
                  headers=None, body=b""):
    h = wg.Handler.__new__(wg.Handler)
    h.command = method
    h.path = path
    h.request_version = "HTTP/1.1"
    h.requestline = "%s %s HTTP/1.1" % (method, path)
    h.close_connection = True
    hdrs = dict(headers or {})
    # 真实客户端会带 Content-Length; 没显式给就按 body 长度补(测边界时才手工指定)
    if body and not any(k.lower() == "content-length" for k in hdrs):
        hdrs["Content-Length"] = str(len(body))
    # 生产环境 self.headers 是 email.message.Message(头名大小写不敏感), 这里保持一致
    m = Message()
    for k, v in hdrs.items():
        m[k] = v
    h.headers = m
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h._headers_buffer = []
    return h


def parse_response(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    hdrs = {}
    for ln in lines[1:]:
        if b":" in ln:
            k, _, v = ln.partition(b":")
            hdrs[k.decode().strip().lower()] = v.decode().strip()
    return status, hdrs, body


REAL_FETCH = None


class HttpUxBase(unittest.TestCase):
    def setUp(self):
        import warp_gateway as wg
        global REAL_FETCH
        REAL_FETCH = wg.fetch_upstream
        self.tmp, self.cfg = make_env()
        wg.CFG = self.cfg
        wg.STATE_DIR = self.tmp
        wg.KEYS = wg.KeyStore(self.cfg["keys_file"])
        wg.USAGE = StubUsage()
        wg.SEM = threading.BoundedSemaphore(self.cfg["max_concurrent"])
        wg.DEBUG = False
        wg._models_cache = {"ts": 0.0, "body": None}
        wg.POOL = wg.NodePool(self.cfg)
        wg.fetch_upstream = REAL_FETCH
        self.wg = wg

    def tearDown(self):
        self.wg.fetch_upstream = REAL_FETCH

    def call(self, handler):
        handler.do_POST()
        return parse_response(handler.wfile.getvalue())

    def call_get(self, handler):
        handler.do_GET()
        return parse_response(handler.wfile.getvalue())

    def body(self, obj):
        return json.dumps(obj).encode()


class TestAuth(HttpUxBase):
    def test_missing_key_says_missing(self):
        h = build_handler(self.wg, headers={}, body=self.body(
            {"model": "m", "messages": []}))
        status, hdrs, body = self.call(h)
        self.assertEqual(status, 401)
        self.assertIn("missing api key", json.loads(body)["error"]["message"])
        self.assertIn("bearer", hdrs["www-authenticate"].lower())

    def test_invalid_key_says_invalid(self):
        h = build_handler(self.wg, headers={"Authorization": "Bearer nope"},
                          body=self.body({"model": "m", "messages": []}))
        status, _, body = self.call(h)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["message"], "invalid api key")

    def test_bearer_scheme_case_insensitive(self):
        # 大小写不同视为已带凭据 → 文案应是 "invalid api key" 而非 "missing"
        h = build_handler(self.wg, headers={"authorization": "bEaReR nope"},
                          body=self.body({"model": "m", "messages": []}))
        _, _, body = self.call(h)
        self.assertEqual(json.loads(body)["error"]["message"], "invalid api key")

    def test_x_api_key_header_accepted(self):
        h = build_handler(self.wg, headers={"x-api-key": "nope"},
                          body=self.body({"model": "m", "messages": []}))
        _, _, body = self.call(h)
        self.assertEqual(json.loads(body)["error"]["message"], "invalid api key")

    def test_healthz_needs_no_auth(self):
        h = build_handler(self.wg, method="GET", path="/healthz", headers={})
        status, _, body = self.call_get(h)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])


class TestBodyLimits(HttpUxBase):
    def test_missing_content_length_is_400(self):
        h = build_handler(self.wg, headers={"Authorization": "Bearer test-key"})
        status, _, body = self.call(h)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["type"], "invalid_request")

    def test_empty_body_is_400(self):
        h = build_handler(self.wg, headers={"Authorization": "Bearer test-key",
                                            "Content-Length": "0"})
        status, _, body = self.call(h)
        self.assertEqual(status, 400)
        self.assertIn("empty", json.loads(body)["error"]["message"])

    def test_invalid_content_length_is_400(self):
        h = build_handler(self.wg, headers={"Authorization": "Bearer test-key",
                                            "Content-Length": "abc"})
        status, _, body = self.call(h)
        self.assertEqual(status, 400)
        self.assertIn("invalid Content-Length", json.loads(body)["error"]["message"])

    def test_oversized_body_is_413(self):
        # max_body_bytes=1024 → 声明 2048 就拒, 不需要真读进来
        h = build_handler(self.wg, headers={"Authorization": "Bearer test-key",
                                            "Content-Length": "2048"})
        status, _, body = self.call(h)
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["type"], "invalid_request")


class TestTerminalErrors(HttpUxBase):
    """全池重试耗尽的终态分类: 429=被限流, 503=节点故障/排队超时。"""

    def _run_exhausted(self, fmt: str = "chat"):
        wg = self.wg
        wg.POOL = StubPool(pick=None)  # 池子枯竭 → 一次都没试
        if fmt == "chat":
            h = build_handler(wg, headers={"Authorization": "Bearer test-key"},
                              body=self.body({"model": "m",
                                              "messages": [{"role": "user", "content": "hi"}]}))
            status, hdrs, body = self.call(h)
        else:
            h = build_handler(wg, path="/v1/responses",
                              headers={"Authorization": "Bearer test-key"},
                              body=self.body({"model": "m", "input": "hi"}))
            status, hdrs, body = self.call(h)
        return status, hdrs, json.loads(body)

    def test_no_node_is_503_server_error_with_retry_after(self):
        for fmt in ("chat", "responses"):
            with self.subTest(fmt=fmt):
                status, hdrs, body = self._run_exhausted(fmt)
                self.assertEqual(status, 503)
                self.assertEqual(body["error"]["type"], "server_error")
                self.assertGreaterEqual(int(hdrs["retry-after"]), 1)

    def test_all_nodes_429_is_429_rate_limit_with_retry_after(self):
        wg = self.wg
        wg.POOL = StubPool(pick=10800)

        def fake_fetch(cfg, port, path, payload, **kw):
            return FakeConn(), FakeResp(429, b'{"error":{"type":"FreeUsageLimitError",'
                                             b'"message":"daily limit"}}'), port

        wg.fetch_upstream = fake_fetch
        h = build_handler(wg, headers={"Authorization": "Bearer test-key"},
                          body=self.body({"model": "m",
                                          "messages": [{"role": "user", "content": "hi"}]}))
        status, hdrs, body = self.call(h)
        self.assertEqual(status, 429)  # 被限流 → 429(不是 503)
        self.assertGreaterEqual(int(hdrs["retry-after"]), 1)
        # 上游 error body 原样转发, 客户端能看到真实原因
        self.assertIn("FreeUsageLimitError", json.dumps(json.loads(body)))
        self.assertGreaterEqual(int(hdrs["retry-after"]), 1)


class TestModelFilter(unittest.TestCase):
    """上游改名/上下架模型时, 列表必须仍只给客户端能用的项。"""

    def setUp(self):
        import warp_gateway as wg
        self.wg = wg
        self.cfg = {"free_only": True, "model_blacklist": ["jev-1.13-free"]}

    def test_keeps_free_drops_paid_and_blacklisted(self):
        data = {"data": [{"id": "mimo-v2.6-flash-free"}, {"id": "gpt-6-luna"},
                         {"id": "jev-1.13-free"}, {"id": "muse-spark-1.3-contributor-free"}]}
        out = self.wg.filter_models(data, self.cfg)
        # muse 保留: 它只能走 /responses, 但 responses 客户端需要能发现它
        self.assertEqual([m["id"] for m in out["data"]],
                         ["mimo-v2.6-flash-free", "muse-spark-1.3-contributor-free"])

    def test_missing_keys_do_not_crash(self):
        self.assertEqual(self.wg.filter_models({}, self.cfg), {})
        self.assertEqual(self.wg.filter_models({"data": "nope"}, self.cfg), {"data": "nope"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
