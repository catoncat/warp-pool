#!/usr/bin/env python3
"""单元测试: NodePool / 连接池 / 会话指纹(无网络, 隔离环境)。

用法: python3 tests/test_nodepool.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def make_env():
    tmp = tempfile.mkdtemp(prefix="warp-pool-test-")
    os.environ["WARP_POOL_STATE"] = tmp
    os.environ["WARP_GATEWAY_CONFIG"] = os.path.join(tmp, "cfg.json")
    cfg = {
        "listen": "127.0.0.1:18199",
        "node_host": "127.0.0.1",
        "node_ports": [10800, 10801, 10802, 10803, 10804],
        "upstream_base": "/inference/v1",
        "upstream_host": "upstream.example",
        "max_concurrent": 8, "max_attempts": 3,
        "connect_timeout": 10, "read_timeout": 300, "ttft_timeout": 25,
        "queue_timeout": 15, "default_quota": 20, "quota_ratio": 0.85,
        "sticky_ttl": 1800, "free_only": True, "spoof_headers": True,
        "opencode_version": "latest/2.0.29/cli",
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

def reload_gateway(cfg, tmp):
    import warp_gateway as wg
    import importlib
    wg.CFG = cfg
    wg.STATE_DIR = tmp
    wg.POOL = wg.NodePool(cfg)
    return wg

class FakeConn:
    def __init__(self):
        self.closed = False
        self.sock = object()
    def close(self):
        self.closed = True

class TestFingerprint(unittest.TestCase):
    def test_same_conversation_stable(self):
        import warp_gateway as wg
        f1 = wg.fingerprint([{"role": "system", "content": "A"},
                             {"role": "user", "content": "你好"}])
        f2 = wg.fingerprint([{"role": "system", "content": "A"},
                             {"role": "user", "content": "你好"},
                             {"role": "assistant", "content": "回"},
                             {"role": "user", "content": "继续"}])
        self.assertEqual(f1, f2)  # 历史追加不影响前 2 条指纹
    def test_different_conversation(self):
        import warp_gateway as wg
        f1 = wg.fingerprint([{"role": "system", "content": "A"}, {"role": "user", "content": "你好"}])
        f2 = wg.fingerprint([{"role": "system", "content": "B"}, {"role": "user", "content": "另一个"}])
        self.assertNotEqual(f1, f2)
    def test_multimodal_content(self):
        import warp_gateway as wg
        f1 = wg.fingerprint([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        f2 = wg.fingerprint([{"role": "user", "content": [{"type": "text", "text": "yo"}]}])
        self.assertNotEqual(f1, f2)

class TestNodePool(unittest.TestCase):
    """每个测试独立 NodePool(不共享状态, 避免跨测试污染)。"""
    def setUp(self):
        self.tmp, self.cfg = make_env()
        self.wg = reload_gateway(self.cfg, self.tmp)
        self.POOL = self.wg.POOL
    def test_pick_returns_node(self):
        self.assertIn(self.POOL.pick("k", fp="f1"), self.cfg["node_ports"])
    def test_empty_pool_none(self):
        import warp_gateway as wg
        p = wg.NodePool({**self.cfg, "node_ports": []})
        self.assertIsNone(p.pick("k", fp="f1"))
    def test_conversation_sticky(self):
        p1 = self.POOL.pick("k", fp="same")
        self.assertEqual(self.POOL.pick("k", fp="same"), p1)
    def test_multi_conversation_spread(self):
        ports = set()
        for i in range(4):
            ports.add(self.POOL.pick("k", fp="conv%d" % i))
        self.assertGreater(len(ports), 1)  # 多对话应分散到多个节点
    def test_quota_exhausted_rotates(self):
        p1 = self.POOL.pick("k", fp="q")
        self.POOL._success[p1] = 18  # 85%*20=17 → 已将尽
        p2 = self.POOL.pick("k", fp="q")
        self.assertNotEqual(p1, p2)
        self.assertEqual(self.POOL._conv[("k", "q")][0], p2)  # 记忆更新
    def test_no_avg_quota_poisoning(self):
        """回归(2026-08-18): 少数被烧 IP 的小 hint 不得把健康节点的配额阈值拖低。"""
        p = self.POOL
        p._quota_hint[10800] = 5   # 被烧 IP 学到小配额
        p._quota_hint[10801] = 4
        p._success[10802] = 10     # 健康节点已服务 10 次, 远小于 default_quota 20*0.85=17
        self.assertFalse(p._exhausted(10802))   # 旧均值兜底(avg~4)会误标
        p._success[10800] = 5      # 有 hint 的节点按自己的 hint: 5*0.85=4.25
        self.assertTrue(p._exhausted(10800))
    def test_daily_cool_zero_success_rotation_signal(self):
        """回归(2026-08-19): success=0 撞 429(共享出口被外部烧穿)也要进
        quota_exhausted 供看门狗轮换; 纯比值判断(0 < 0.85*20)永远不成立,
        只靠比值会让这些节点躺到午夜(今天 9/25 节点就是这样死的)。"""
        p = 10800
        self.POOL.cool_daily(p, 3600)  # 0 成功撞日配额 → 触发 _write_quota
        self.assertFalse(self.POOL._exhausted(p))  # 比值判断确实看不到它
        with open(os.path.join(self.tmp, "quota_exhausted")) as f:
            ports = f.read().split()
        self.assertIn("10800", ports)

    def test_short_cool_not_rotation_signal(self):
        """瞬态冷却(连接错误/5xx/短暂 429)不应触发重注册轮换。"""
        self.POOL.cool_short(10801, 300)
        self.POOL._write_quota()
        with open(os.path.join(self.tmp, "quota_exhausted")) as f:
            ports = f.read().split()
        self.assertNotIn("10801", ports)

    def test_429_learns_quota(self):
        p = self.POOL.pick("k2", fp="q2")
        self.POOL._success[p] = 40
        self.POOL.cool_daily(p, 3600)
        self.assertEqual(self.POOL._quota_hint.get(p), 40)
        self.assertNotEqual(self.POOL.pick("k2", fp="q2"), p)  # 冷却跳过
    def test_cooldown_short(self):
        p = self.POOL.pick("k3", fp="q3")
        self.POOL.cool_short(p, 3600)
        self.assertNotEqual(self.POOL.pick("k3", fp="q3"), p)
    def test_recycled_clears_state(self):
        p = self.POOL.pick("k4", fp="q4")
        self.POOL._success[p] = 30
        self.POOL.cool_daily(p, 3600)
        with open(os.path.join(self.tmp, "recycled"), "w") as f:
            f.write("%d\n" % p)
        self.POOL._read_recycled()
        self.assertEqual(self.POOL._cooldown.get(p, 0), 0)  # 冷却已清
        self.assertNotIn(p, self.POOL._success)
    def test_conv_lru_bounded(self):
        for i in range(10):
            self.POOL.pick("k5", fp="lru%d" % i)
        self.assertLessEqual(len(self.POOL._conv), self.cfg["conv_table_size"])
    def test_fallback_when_pool_depleted(self):
        import warp_gateway as wg
        p = wg.NodePool({**self.cfg, "node_ports": [10800]})
        p._success[10800] = 100
        # 池子只剩将尽节点 → 兜底返回它(而不是 None)
        self.assertEqual(p.pick("k", fp="f"), 10800)

class TestConnPool(unittest.TestCase):
    def setUp(self):
        self.tmp, self.cfg = make_env()
        self.wg = reload_gateway(self.cfg, self.tmp)
        self.POOL = self.wg.POOL
    def test_keep_borrow(self):
        c = FakeConn()
        self.POOL.keep(10800, c)
        self.assertIs(self.POOL.borrow(10800), c)
        self.assertIsNone(self.POOL.borrow(10800))  # 取出后为空
    def test_keep_replace_closes_old(self):
        c1, c2 = FakeConn(), FakeConn()
        self.POOL.keep(10800, c1)
        self.POOL.keep(10800, c2)
        self.assertTrue(c1.closed)
        self.assertIs(self.POOL._idle.get(10800), c2)
    def test_drop_closes(self):
        c = FakeConn()
        self.POOL.keep(10800, c)
        self.POOL.drop(10800, self.POOL.borrow(10800))
        self.assertTrue(c.closed)
    def test_cool_clears_idle(self):
        c = FakeConn()
        self.POOL.keep(10801, c)
        self.POOL.cool_short(10801, 5)
        self.assertNotIn(10801, self.POOL._idle)
        self.assertTrue(c.closed)
    def test_recycled_clears_idle(self):
        c = FakeConn()
        self.POOL.keep(10802, c)
        with open(os.path.join(self.tmp, "recycled"), "w") as f:
            f.write("10802\n")
        self.POOL._read_recycled()
        self.assertNotIn(10802, self.POOL._idle)
        self.assertTrue(c.closed)

if __name__ == "__main__":
    unittest.main(verbosity=2)
