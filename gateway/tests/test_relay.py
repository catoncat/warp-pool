#!/usr/bin/env python3
"""单元测试: 首事件护栏(relay_stream / relay_aggregate) + 配额标记 + 指数冷却。

无网络: 用假 resp 喂 SSE 分片。
动手理由: 护栏的每条分支都会改变"客户端看到什么"(换节点重试 vs 收到空回复),
且它是 200 之后才起作用的路径 —— 这类回归没有别的观测手段。

用法: python3 gateway/tests/test_relay.py
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_env(first_event_timeout=15):
    tmp = tempfile.mkdtemp(prefix="warp-pool-relay-test-")
    os.environ["WARP_POOL_STATE"] = tmp
    os.environ["WARP_GATEWAY_CONFIG"] = os.path.join(tmp, "cfg.json")
    cfg = {
        "listen": "127.0.0.1:18199",
        "node_host": "127.0.0.1",
        "node_ports": [10800, 10801],
        "upstream_base": "/inference/v1",
        "upstream_host": "upstream.example",
        "max_concurrent": 8, "max_attempts": 3,
        "connect_timeout": 1, "read_timeout": 300, "ttft_timeout": 5,
        "first_event_timeout": first_event_timeout,
        "queue_timeout": 15, "default_quota": 20, "quota_ratio": 0.85,
        "sticky_ttl": 1800, "free_only": True, "spoof_headers": True,
        "opencode_version": "latest/2.0.29/cli",
        "max_body_bytes": 1024,
        "keys_file": os.path.join(tmp, "keys.txt"),
        "nodes_state_file": os.path.join(tmp, "nodes.json"),
        "quota_state_file": os.path.join(tmp, "quota.json"),
        "usage_log": os.path.join(tmp, "usage.jsonl"),
        "conv_table_size": 4,
    }
    with open(cfg["keys_file"], "w") as f:
        f.write("test-key\n")
    with open(cfg["nodes_state_file"], "w") as f:
        f.write("{}")
    return tmp, cfg


class StubUsage:
    def record(self, key, model, usage, estimated=False):
        pass


class StubSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, v):
        self.timeouts.append(v)


class FakeConn:
    """带 sock 的假连接: 记录 settimeout 调用, close 可观测。"""

    def __init__(self):
        self.sock = StubSock()
        self.closed = False

    def close(self):
        self.closed = True


class FakeResp:
    """按分片返回的假 SSE 响应。"""

    def __init__(self, chunks, exc=None):
        self.chunks = list(chunks)
        self.exc = exc
        self.reads = 0

    def read1(self, n=-1):
        self.reads += 1
        if self.chunks:
            return self.chunks.pop(0)
        if self.exc is not None:
            exc, self.exc = self.exc, None
            raise exc
        return b""


class FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.headers = []

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers.append((k, v))

    def end_headers(self):
        self.wfile.write(b"--head--\n")

    def body(self):
        return self.wfile.getvalue()


def sse(*objs):
    out = b""
    for o in objs:
        out += b"data: " + (o if isinstance(o, bytes) else json.dumps(o).encode()) + b"\n\n"
    return out


PRELUDE = sse({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
CONTENT = sse({"choices": [{"index": 0, "delta": {"content": "hi"}}]})
FINISH = sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})
ERR = sse({"error": {"type": "FreeUsageLimitError", "message": "daily limit"}})


class RelayBase(unittest.TestCase):
    def setUp(self):
        import socket as _socket
        import warp_gateway as wg
        self.socket = _socket
        self.wg = wg
        self.tmp, self.cfg = make_env()
        wg.CFG = self.cfg
        wg.STATE_DIR = self.tmp
        wg.USAGE = StubUsage()
        wg.POOL = wg.NodePool(self.cfg)

    def run_stream(self, chunks, exc=None, fmt="chat"):
        h, c = FakeHandler(), FakeConn()
        ok = self.wg.relay_stream(h, c, FakeResp(chunks, exc), "k", "m", 10800, fmt=fmt)
        return ok, h, c

    def run_aggregate(self, chunks, exc=None, fmt="chat"):
        h, c = FakeHandler(), FakeConn()
        ok = self.wg.relay_aggregate(h, c, FakeResp(chunks, exc), "k", "m", 10800, fmt=fmt)
        return ok, h, c


class TestStreamGuard(RelayBase):
    def test_commits_on_first_content(self):
        ok, h, c = self.run_stream([PRELUDE + CONTENT, FINISH])
        self.assertTrue(ok)
        self.assertEqual(h.status, 200)
        # 前置帧也必须补发给客户端(不能因为憋着就丢)
        self.assertIn(b'"role"', h.body())
        self.assertIn(b'"content"', h.body())
        self.assertIn(b'"stop"', h.body())

    def test_error_before_content_does_not_commit(self):
        ok, h, c = self.run_stream([ERR])
        self.assertFalse(ok)
        self.assertIsNone(h.status)      # 一个字节都没发给客户端
        self.assertEqual(h.body(), b"")
        self.assertTrue(c.closed)        # 连接作废, 不归还池

    def test_prelude_then_error_does_not_commit(self):
        ok, h, c = self.run_stream([PRELUDE, ERR])
        self.assertFalse(ok)
        self.assertIsNone(h.status)

    def test_first_event_timeout_does_not_commit(self):
        ok, h, c = self.run_stream([], exc=self.socket.timeout("no event"))
        self.assertFalse(ok)
        self.assertIsNone(h.status)

    def test_empty_stream_does_not_commit(self):
        ok, h, c = self.run_stream([])
        self.assertFalse(ok)
        self.assertIsNone(h.status)

    def test_timeout_after_commit_keeps_partial(self):
        ok, h, c = self.run_stream([PRELUDE + CONTENT], exc=self.socket.timeout("stall"))
        self.assertTrue(ok)              # 已提交的流不可重放 → 就地结束
        self.assertEqual(h.status, 200)
        self.assertIn(b'"content"', h.body())

    def test_socket_timeout_switches_to_read_timeout_after_commit(self):
        _, _, c = self.run_stream([PRELUDE + CONTENT, FINISH])
        self.assertEqual(c.sock.timeouts[0], self.cfg["first_event_timeout"])
        self.assertEqual(c.sock.timeouts[-1], self.cfg["read_timeout"])

    def test_guard_disabled_commits_immediately(self):
        self.cfg["first_event_timeout"] = 0
        ok, h, _ = self.run_stream([PRELUDE, CONTENT])
        self.assertTrue(ok)
        self.assertEqual(h.status, 200)


class TestAggregateGuard(RelayBase):
    def test_normal_stream_returns_json(self):
        ok, h, _ = self.run_aggregate([PRELUDE + CONTENT + FINISH])
        self.assertTrue(ok)
        body = json.loads(h.body().split(b"--head--\n", 1)[1])
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["total_tokens"], 4)

    def test_error_before_content_retries(self):
        ok, h, c = self.run_aggregate([ERR])
        self.assertFalse(ok)
        self.assertEqual(h.body(), b"")
        self.assertTrue(c.closed)

    def test_empty_stream_retries(self):
        ok, h, _ = self.run_aggregate([])
        self.assertFalse(ok)
        self.assertEqual(h.body(), b"")

    def test_first_event_timeout_retries(self):
        ok, h, _ = self.run_aggregate([], exc=self.socket.timeout("no event"))
        self.assertFalse(ok)
        self.assertEqual(h.body(), b"")

    def test_error_after_content_keeps_answer(self):
        # 已经有正文了就不再换节点(否则同一请求二次消耗配额)
        ok, h, _ = self.run_aggregate([PRELUDE + CONTENT, ERR, FINISH])
        self.assertTrue(ok)
        body = json.loads(h.body().split(b"--head--\n", 1)[1])
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")

    def test_clean_but_empty_stream_is_returned(self):
        # 上游正常收尾但没出正文: 这是"空回复"而不是节点故障, 不要重试(重试会白烧配额,
        # 且与流式路径行为不一致 —— 流式会把空回复交给客户端)
        empty = sse({}, b"[DONE]")
        ok, h, _ = self.run_aggregate([empty])
        self.assertTrue(ok)
        body = json.loads(h.body().split(b"--head--\n", 1)[1])
        self.assertEqual((body["choices"][0]["message"].get("content") or ""), "")

    def test_line_split_across_chunks_is_joined(self):
        # 回归: 旧实现按 chunk 切行, TCP 分段切开的一行会被整条丢掉
        full = PRELUDE + CONTENT + FINISH
        cut = len(full) // 2
        ok, h, _ = self.run_aggregate([full[:cut], full[cut:]])
        self.assertTrue(ok)
        body = json.loads(h.body().split(b"--head--\n", 1)[1])
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")


def rsse(ev_type, obj=None):
    """responses 协议的 SSE 帧: 事件名与 data.type 同名, 且 data 里是完整对象。"""
    o = {"type": ev_type, "sequence_number": 0}
    o.update(obj or {})
    return b"event: " + ev_type.encode() + b"\ndata: " + json.dumps(o).encode() + b"\n\n"


R_PRELUDE = (rsse("response.created", {"response": {"id": "resp_1", "status": "in_progress"}})
             + rsse("response.in_progress", {"response": {"id": "resp_1"}})
             + rsse("response.output_item.added", {"output_index": 0, "item": {"type": "reasoning"}}))
R_DELTA = rsse("response.output_text.delta", {"output_index": 0, "delta": "hi"})
R_FINAL = rsse("response.completed", {"response": {
    "id": "resp_1", "object": "response", "status": "completed", "model": "muse-x",
    "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}]}],
    "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}}})
R_FAILED = rsse("response.failed", {"response": {"id": "resp_1", "status": "failed",
                                                "error": {"code": "x", "message": "boom"}}})


class TestResponsesProtocol(RelayBase):
    """responses 协议: 只吃 /responses 的模型(如 muse)经网关闭环。

    动手理由: 前导帧(created/in_progress/output_item.added)不带内容, 若当成"有效
    事件"会过早提交 —— 上游随后报错就已发给客户端; 反之若丢掉真正的有效信号
    (delta/终态), 整个 responses 路径会 100% 超时换节点(这正是 muse 报 503 的原因)。
    """

    def test_prelude_frames_are_not_effective(self):
        state, _ = self.wg.scan_prelude(R_PRELUDE, "responses")
        self.assertEqual(state, "prelude")

    def test_delta_and_final_are_effective(self):
        self.assertEqual(self.wg.scan_prelude(R_DELTA, "responses")[0], "effective")
        self.assertEqual(self.wg.scan_prelude(R_FINAL, "responses")[0], "effective")

    def test_failed_event_is_error(self):
        state, detail = self.wg.scan_prelude(R_FAILED, "responses")
        self.assertEqual(state, "error")
        self.assertEqual(detail, "boom")

    def test_stream_commits_on_delta_and_replays_prelude(self):
        ok, h, _ = self.run_stream([R_PRELUDE + R_DELTA, R_FINAL], fmt="responses")
        self.assertTrue(ok)
        self.assertEqual(h.status, 200)
        self.assertIn(b"response.created", h.body())     # 前置帧补发
        self.assertIn(b"output_text.delta", h.body())

    def test_stream_timeout_before_delta_does_not_commit(self):
        ok, h, _ = self.run_stream([R_PRELUDE], exc=self.socket.timeout("stall"), fmt="responses")
        self.assertFalse(ok)
        self.assertIsNone(h.status)                      # 客户端一个字节都没收到

    def test_stream_failed_before_delta_does_not_commit(self):
        ok, h, _ = self.run_stream([R_PRELUDE, R_FAILED], fmt="responses")
        self.assertFalse(ok)
        self.assertIsNone(h.status)

    def test_aggregate_passes_through_upstream_response_object(self):
        ok, h, _ = self.run_aggregate([R_PRELUDE + R_DELTA + R_FINAL], fmt="responses")
        self.assertTrue(ok)
        body = json.loads(h.body().split(b"--head--\n", 1)[1])
        self.assertEqual(body["object"], "response")      # responses 格式, 不是 chat.completion
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["usage"]["total_tokens"], 7)
        self.assertEqual(body["output"][0]["content"][0]["text"], "hi")

    def test_aggregate_without_final_event_retries(self):
        # 流被截断(只有前导帧, 没有终态) → 换节点, 不能让客户端拿到半截对象
        ok, h, _ = self.run_aggregate([R_PRELUDE], fmt="responses")
        self.assertFalse(ok)
        self.assertEqual(h.body(), b"")

    def test_aggregate_failed_retries(self):
        ok, h, _ = self.run_aggregate([R_FAILED], fmt="responses")
        self.assertFalse(ok)
        self.assertEqual(h.body(), b"")


class TestQuotaMarkers(RelayBase):
    def test_known_markers(self):
        wg = self.wg
        for body in (b'{"error":{"type":"FreeUsageLimitError"}}',
                     b'{"error":{"type":"rateLimitExceeded"}}',
                     b'{"error":{"type":"insufficient_quota"}}',
                     b'{"error":{"message":"quota exceeded"}}',
                     b'{"error":{"message":"Insufficient balance"}}',
                     "额度不足".encode(),
                     "配额已用尽".encode()):
            with self.subTest(body=body):
                self.assertTrue(wg.is_quota_error(body))

    def test_plain_rate_limit_is_not_quota(self):
        # 判错方向的代价不对称: 普通限流不该把节点锁到 UTC 零点
        self.assertFalse(self.wg.is_quota_error(b'{"error":{"message":"slow down"}}'))
        self.assertFalse(self.wg.is_quota_error(b'{"error":{"message":"too many requests"}}'))


class TestExponentialCooldown(RelayBase):
    def test_grows_and_caps(self):
        pool = self.wg.POOL
        seen = []
        for _ in range(6):
            pool.cool_short(10800, 30)
            seen.append(pool.table()["10800"]["cooldown_in"])
        self.assertEqual(seen[0], 30)
        self.assertEqual(seen[1], 60)
        self.assertEqual(seen[2], 120)
        self.assertEqual(seen[3], 240)
        self.assertEqual(seen[4], 240)   # 上限 8×
        self.assertEqual(seen[5], 240)

    def test_success_resets_streak(self):
        pool = self.wg.POOL
        pool.cool_short(10800, 30)
        pool.cool_short(10800, 30)
        pool.record_success(10800)
        pool._cooldown.clear()          # 只看计数是否被清, 不看残留冷却
        pool.cool_short(10800, 30)
        self.assertEqual(pool.table()["10800"]["cooldown_in"], 30)

    def test_daily_cooldown_resets_streak(self):
        pool = self.wg.POOL
        pool.cool_short(10800, 30)
        pool.cool_daily(10800, None)
        pool._cooldown.clear()
        pool.cool_short(10800, 30)
        self.assertEqual(pool.table()["10800"]["cooldown_in"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
