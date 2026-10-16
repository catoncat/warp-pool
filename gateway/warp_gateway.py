#!/usr/bin/env python3
"""warp-pool gateway — OpenAI 兼容网关，走 wireproxy WARP 节点池转发上游 0penc0de 免费模型。

设计依据（2026-08 上游服务端源码实证）:
- zen 匿名限流按 IP 按天计「请求数」(非 token): IpRateLimitTable.count+1;
  新 IP (lifetimeCount < dailyLimit*7) 有 2x 日配额; 超限 429 冷却到 UTC 零点。
- 匿名请求带 session 头 → 上游按 session 哈希做 provider 路由/粘性(stickyProvider);
  上游转发前会删掉自己的私有头。
- 新 IP 有 promoTokens 试用期(走 trial provider 池), 同一个 IP 连续使用会
  "老化"出试用期、进入稳定主 provider → DeepSeek 前缀缓存(KV cache)可命中。

因此网关三层设计:
1. 并发: 信号量只锁「排队+建连+等响应头」阶段(短), 流式转发不占槽 →
   一个长流不再堵死后续请求(原 max_concurrent 锁整个流生命周期是排队根因)。
2. 会话粘性: 同一 API key 的请求优先粘在同一节点(IP), 让 IP 老化出试用期、
   进入稳定 provider → 缓存命中; 粘性在配额将尽/冷却/失败时自动换节点。
3. 配额感知: 每节点记录当日成功数, 从实际 429 学习配额上限,
   达阈值(~85%)提前换新 IP(写 quota_exhausted 由 health.sh 重注册),
   而不是撞 429 后被动冷到 UTC 零点浪费新 IP 配额。
外加: session/project ID 持久化到 state(重启不变 → zen 侧哈希路由稳定)。

仅依赖 Python 标准库(目标机 OpenCloudOS 无 pip)。
"""

import itertools
import json
import os
import socket
import ssl
import struct
import threading
import time
import uuid
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("WARP_POOL_STATE", os.path.join(os.path.dirname(BASE), "state"))
CONFIG_PATH = os.environ.get("WARP_GATEWAY_CONFIG", os.path.join(BASE, "config.json"))

DEFAULT_CONFIG = {
    "listen": "0.0.0.0:10900",
    "upstream_host": "opencode.ai",
    "upstream_base": "/inference/v1",
    "node_ports": [10800],
    "node_host": "127.0.0.1",
    "keys_file": os.path.join(BASE, "keys.txt"),
    "nodes_state_file": os.path.join(STATE_DIR, "nodes.json"),
    "usage_log": os.path.join(STATE_DIR, "usage.jsonl"),
    "max_concurrent": 8,          # 并发「建连槽」数(不是并发流数)
    "queue_timeout": 15,          # 排队等建连槽的最长秒数, 超时 503 快速失败
    "ttft_timeout": 25,           # 上游首字节(TTFT)最长等待, 超时换节点
    "first_event_timeout": 15,    # 首个有效 SSE 事件上限, 超时当节点故障换节点(0=关闭)
    "max_attempts": 15,
    "connect_timeout": 15,
    "read_timeout": 300,
    "max_body_bytes": 64 * 1024 * 1024,   # 请求体上限, 防超大 Content-Length 拖住线程
    "models_cache_sec": 300,
    "free_only": True,
    "spoof_headers": True,
    "opencode_version": "latest/2.0.29/cli",   # UA = <品牌>/<version>, 需近期版本(协议字面量, 见 UPSTREAM_GATE.md)
    "sticky_ttl": 1800,           # key->节点 粘性有效期(秒), 过期重分配
    "default_quota": 40,          # 未知节点日配额假设(新 IP 2x=40, 从 429 学习修正)
    "quota_ratio": 0.85,          # 成功数达配额该比例即视为「将尽」提前轮换
    "quota_state_file": os.path.join(STATE_DIR, "quota.json"),
    # 上游已上架但任何协议都调不通的模型, 不回给客户端(免得客户端列表里
    # 出现"看得见点不动"的项)。只影响 /v1/models 列表, 不拦请求。
    "model_blacklist": ["jev-1.13-free"],
}


def utc_midnight_ts() -> float:
    now = time.time()
    return now - (now % 86400) + 86400


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def _load_or_create_id(name: str) -> str:
    """项目 ID 持久化到 state 目录: 网关重启不变。"""
    path = os.path.join(STATE_DIR, name + ".id")
    try:
        with open(path) as f:
            val = f.read().strip()
        if val:
            return val
    except FileNotFoundError:
        pass
    val = uuid.uuid4().hex
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(val)
    except OSError:
        pass
    return val


_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SES_BY_FP: dict = {}   # fp -> [ses_id, ts]  会话级粘性(上游前缀缓存)
_SES_LOCK = threading.Lock()

def make_session_id() -> str:
    """上游官方 Identifier.descending 格式:
    ses_ + 12hex(~(ts_ms*4096+counter) 低48位) + 14位base62。
    inference 免费档 gate 校验该格式(时间戳可解码、必须取反)。"""
    current = (int(time.time() * 1000) << 12) + 1
    value = (~current) & 0xFFFFFFFFFFFF
    return "ses_" + value.to_bytes(6, "big").hex() + \
        "".join(_B62[b % 62] for b in os.urandom(14))

def session_for_fp(fp: str | None, ttl: float = 1800) -> str:
    """同一对话指纹复用同一 session(上游前缀缓存命中); 过期换新。"""
    if not fp:
        return make_session_id()
    now = time.time()
    with _SES_LOCK:
        ent = _SES_BY_FP.get(fp)
        if ent and now - ent[1] < ttl:
            ent[1] = now
            return ent[0]
        ses = make_session_id()
        _SES_BY_FP[fp] = [ses, now]
        if len(_SES_BY_FP) > 4096:   # 粗暴防膨胀
            cutoff = now - ttl
            _SES_BY_FP.update({k: v for k, v in _SES_BY_FP.items() if v[1] >= cutoff})
        return ses


# ---------- keys.txt 热加载 ----------

class KeyStore:
    def __init__(self, path: str):
        self.path = path
        self._mtime = 0.0
        self._keys = set()

    def valid(self, key: str) -> bool:
        try:
            mtime = os.stat(self.path).st_mtime
            if mtime != self._mtime:
                with open(self.path) as f:
                    self._keys = {
                        ln.strip() for ln in f
                        if ln.strip() and not ln.startswith("#")
                    }
                self._mtime = mtime
        except FileNotFoundError:
            self._keys = set()
        return key in self._keys


# ---------- 节点池 ----------

def fingerprint(messages, k=2):
    """会话指纹: 前 k 条消息(role+content)的 SHA1。

    同一对话的每个请求, 前 k 条消息固定不变(历史只追加在尾部) →
    指纹稳定 → 粘同一节点(缓存命中); 不同对话指纹不同 → 分散到不同
    节点(并发不挤单 IP)。messages 不足 k 条时取全部。
    """
    import hashlib
    h = hashlib.sha1()
    for m in messages[:k]:
        h.update(m.get("role", "").encode("utf-8", "ignore"))
        c = m.get("content")
        if isinstance(c, str):
            h.update(c.encode("utf-8", "ignore"))
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    h.update(str(part.get("text", "")).encode("utf-8", "ignore"))
    return h.hexdigest()


def responses_fingerprint(req, k=2):
    """responses 协议会话指纹: input 可为 str 或 array(OpenAI 新协议消息)。
    前 k 个输入文本块稳定 → 粘同一节点(出口 IP/前缀缓存命中)。"""
    import hashlib
    h = hashlib.sha1()
    inp = req.get("input")
    n = 0

    def add_text(t):
        nonlocal n
        if n >= k:
            return
        h.update(str(t).encode("utf-8", "ignore"))
        n += 1

    if isinstance(inp, str):
        add_text(inp)
    elif isinstance(inp, list):
        for item in inp:
            if n >= k:
                break
            if isinstance(item, str):
                add_text(item)
            elif isinstance(item, dict):
                h.update(str(item.get("role", "")).encode("utf-8", "ignore"))
                c = item.get("content")
                if isinstance(c, str):
                    add_text(c)
                elif isinstance(c, list):
                    for part in c:
                        if n >= k:
                            break
                        if isinstance(part, dict) and part.get("type") == "input_text":
                            add_text(part.get("text", ""))
    return h.hexdigest()


def normalize_responses_usage(usage: dict | None) -> dict | None:
    """responses(OpenAI 新协议) usage → 网关内部统计格式。
    responses: input_tokens/output_tokens/input_tokens_details.cached_tokens;
    内部: prompt_tokens/completion_tokens/prompt_tokens_details.cached_tokens。"""
    if not usage:
        return None
    ptd = usage.get("input_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "prompt_tokens_details": {"cached_tokens": ptd.get("cached_tokens") or 0},
    }



class NodePool:
    """会话粘性 + 配额感知 + 冷却表。

    - 冷却: daily 429 到 UTC 零点(zen 按天限额); 短 429/网络错短冷却;
      health.sh 写 nodes.json ok=false 的跳过。
    - 粘性: 同一 key 优先用最近节点, 让 IP 老化出试用期 → 缓存稳定;
      配额将尽/冷却/不健康时自动换节点并更新粘性。
    - 配额: 每节点记当日成功请求数; 429 时学习真实配额上限;
      成功数 >= 配额*ratio 视为将尽 → 不参与新粘性分配,
      并写 quota_exhausted 供 health.sh 提前重注册换新 IP。
    - recycled: health.sh 重注册换新 IP 后把端口写 state/recycled,
      网关读到即清冷却+清统计(新 IP 新配额), 不浪费到 UTC 零点。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cooldown = {}   # port -> 冷却截止 ts
        self._cooldown_daily = set()  # 日配额冷却端口集(写 quota_exhausted 供看门狗轮换)
        self._rr = itertools.count()
        self._state_mtime = 0.0
        self._unhealthy = set()
        self._sticky = {}  # key -> (port, last_ts)
        self._success = {}    # port -> 当日成功请求数
        self._fails = {}      # port -> 连续失败次数(短冷却按指数增长, 防坏节点每小时被反复试)
        self._quota_hint = {} # port -> 从 429 学到的配额上限
        self._day = time.strftime("%Y-%m-%d", time.gmtime())  # 启动即当天, 首轮不误清
        self._recycled_mtime = 0.0
        self._last_quota_write = 0.0
        self._idle = {}   # port -> 空闲上游连接(前次响应已完整读尽, 可复用)
        self._borrow_hits = 0    # 连接复用命中(借到空闲连接)
        self._borrow_misses = 0  # 未命中(需新建)
        self._conv = {}          # (key, 会话指纹) -> (port, last_ts); 多对话各粘各的节点

    # ---- 状态刷新 ----

    def _roll_day(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day == self._day:
            return
        with self._lock:
            self._success.clear()
            self._fails.clear()
            self._quota_hint.clear()
            self._cooldown.clear()
            self._cooldown_daily.clear()
            self._day = day

    def _refresh_health(self):
        path = self.cfg["nodes_state_file"]
        try:
            mtime = os.stat(path).st_mtime
        except FileNotFoundError:
            return
        if mtime != self._state_mtime:
            try:
                with open(path) as f:
                    data = json.load(f)
                with self._lock:
                    self._unhealthy = {
                        int(p) for p, st in data.items() if not st.get("ok", True)
                    }
                self._state_mtime = mtime
            except (json.JSONDecodeError, OSError):
                pass

    def _read_recycled(self):
        """health.sh 重注册(换新 IP)后通知: 清冷却与统计, 让新配额立即可用。"""
        path = os.path.join(STATE_DIR, "recycled")
        try:
            mtime = os.stat(path).st_mtime
        except FileNotFoundError:
            return
        if mtime == self._recycled_mtime:
            return
        try:
            with open(path) as f:
                ports = {int(ln.strip()) for ln in f if ln.strip().isdigit()}
            idle = []
            with self._lock:
                for p in ports:
                    self._cooldown.pop(p, None)
                    self._cooldown_daily.discard(p)
                    self._success.pop(p, None)
                    self._fails.pop(p, None)
                    self._quota_hint.pop(p, None)
                    idle.append(self._idle.pop(p, None))
                for k in [k for k, (p, _) in list(self._sticky.items())
                          if p in ports]:
                    self._sticky.pop(k, None)
            for c in idle:
                if c is not None:
                    try:
                        c.close()
                    except OSError:
                        pass
            self._recycled_mtime = mtime
            os.remove(path)
            if DEBUG:
                print("[pool] recycled new-IP ports: " + str(sorted(ports)), flush=True)
        except OSError:
            pass

    # ---- 配额 ----

    def _effective_quota(self, port: int) -> int:
        """该节点日配额估计: 只信本节点从 429 学到的, 否则默认 default_quota。"""
        # 只信本节点自己从 429 学到的配额; 冷启动回退 default_quota。
        # 不用全池均值兜底: 少数被烧的旧 IP (hint 4-6) 会把均值拖到 ~5,
        # 导致健康节点 success>=5 就被误标 exhausted 并触发批量重拨 (2026-08-18 事故)。
        hint = self._quota_hint.get(port)
        if hint:
            return hint
        return int(self.cfg.get("default_quota", 20))

    def _exhausted(self, port: int) -> bool:
        q = self._effective_quota(port)
        return self._success.get(port, 0) >= q * self.cfg.get("quota_ratio", 0.85)

    def _quota_left(self, port: int) -> int:
        q = self._effective_quota(port)
        return int(q * self.cfg.get("quota_ratio", 0.85)
                   - self._success.get(port, 0))

    def record_success(self, port: int):
        with self._lock:
            self._success[port] = self._success.get(port, 0) + 1
            self._fails.pop(port, None)   # 成功即清连续失败计数
        self._write_quota()

    def cool_daily(self, port: int, retry_after):
        until = time.time() + retry_after if retry_after else utc_midnight_ts()
        until = min(until, utc_midnight_ts())
        idle = None
        with self._lock:
            self._cooldown[port] = max(self._cooldown.get(port, 0), until)
            self._cooldown_daily.add(port)
            self._fails.pop(port, None)   # 配额耗尽与"节点坏"是两回事, 不叠加
            # 学习真实配额上限: 撞 429 时的成功数 ≈ 该 IP 日配额
            s = self._success.get(port, 0)
            if s > self._quota_hint.get(port, 0):
                self._quota_hint[port] = s
            idle = self._idle.pop(port, None)
        if idle is not None:
            try:
                idle.close()
            except OSError:
                pass
        self._write_quota()

    def cool_short(self, port: int, sec: float = 60):
        """短冷却。连续失败时按指数增长(上限 8×): 偶发抖动 1×, 真坏节点迅速被动隔离。"""
        idle = None
        with self._lock:
            n = self._fails.get(port, 0)
            self._fails[port] = min(n + 1, 8)
            eff = min(sec * (2 ** min(n, 3)), sec * 8)
            self._cooldown[port] = max(
                self._cooldown.get(port, 0), time.time() + eff)
            idle = self._idle.pop(port, None)
        if idle is not None:
            try:
                idle.close()
            except OSError:
                pass

    # ---- 上游连接复用 ----
    # 每节点保留 1 条「响应已完整读尽」的空闲连接; 复用时若连接已被上游
    # 关闭(空闲超时/IP 轮换), request 会抛 OSError → 调用方丢弃后新建重试。

    def borrow(self, port: int):
        """取出该节点空闲连接; 无则返回 None(需新建)。"""
        with self._lock:
            conn = self._idle.pop(port, None)
            if conn is not None:
                self._borrow_hits += 1
            else:
                self._borrow_misses += 1
            return conn

    def pool_stats(self) -> dict:
        """连接复用统计(供 /stats)。"""
        with self._lock:
            total = self._borrow_hits + self._borrow_misses
            return {
                "reuse_hits": self._borrow_hits,
                "reuse_misses": self._borrow_misses,
                "reuse_rate": round(100.0 * self._borrow_hits / total, 1) if total else 0.0,
                "idle_conns": len(self._idle),
            }

    def keep(self, port: int, conn):
        """响应完整读尽且连接健康 → 归还池(每端口最多 1 条, 重复则关旧的)。"""
        with self._lock:
            old = self._idle.get(port)
            self._idle[port] = conn
        if old is not None and old is not conn:
            try:
                old.close()
            except OSError:
                pass

    def drop(self, port: int, conn):
        try:
            conn.close()
        except OSError:
            pass

    def _write_quota(self):
        """节流(10s)写 quota.json + quota_exhausted 供 health.sh 联动提前轮换。"""
        now = time.time()
        if now - self._last_quota_write < 10:
            return
        self._last_quota_write = now
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            data = {str(p): {
                "success": self._success.get(p, 0),
                "quota_hint": self._quota_hint.get(p, 0),
                "exhausted": self._exhausted(p),
            } for p in self.cfg["node_ports"]}
            with open(self.cfg["quota_state_file"], "w") as f:
                json.dump(data, f)
            now = time.time()
            # 不仅比值耗尽(接近上限)要轮换, 已撞 429 日冷却的端口也要:
            # 共享出口现实下很多节点 success=0 就被外部烧穿, 比值判断永远
            # 不成立(0 < 85%*quota), 只靠比值会让它们躺到午夜 (2026-08-19)。
            ex = [str(p) for p in self.cfg["node_ports"]
                  if self._exhausted(p)
                  or (p in self._cooldown_daily and self._cooldown.get(p, 0) > now)]
            ex_path = os.path.join(STATE_DIR, "quota_exhausted")
            with open(ex_path, "w") as f:
                f.write("\n".join(ex) + ("\n" if ex else ""))
        except OSError:
            pass

    # ---- 选择 ----

    def pick(self, key=None, fp=None, exclude=None):
        """选节点。fp=会话指纹时按对话粘性: 同一对话固定同一节点(缓存命中),
        不同对话 round-robin 挑配额最大(最空闲)的节点 → 并发摊到多 IP。
        """
        self._refresh_health()
        self._read_recycled()
        self._roll_day()
        now = time.time()
        ports = self.cfg["node_ports"]
        if not ports:
            return None
        with self._lock:
            # 1) 会话粘性: 该 (key, 对话) 用过的节点仍健康/未冷却/配额未将尽
            if key and fp:
                conv = self._conv.get((key, fp))
                if conv:
                    port, last = conv
                    if (port in ports and port not in self._unhealthy
                            and self._cooldown.get(port, 0) <= now
                            and not self._exhausted(port)):
                        self._conv[(key, fp)] = (port, now)
                        return port
            # 2) round-robin 起点扫一圈, 挑剩余配额最大的节点(配额摊匀)
            start = next(self._rr) % len(ports)
            best, best_q = None, -1
            fallback = None
            for i in range(len(ports)):
                port = ports[(start + i) % len(ports)]
                if exclude and port in exclude:
                    continue
                if port in self._unhealthy or self._cooldown.get(port, 0) > now:
                    continue
                q = self._quota_left(port)
                if q > 0 and q > best_q:
                    best, best_q = port, q
                elif fallback is None:
                    fallback = port  # 配额将尽但健康, 池子枯竭时兜底
            port = best if best is not None else fallback
            if port is not None and key and fp:
                self._conv[(key, fp)] = (port, now)
                self._trim_conv()
            return port

    def _trim_conv(self):
        """LRU: 会话表超过上限时淘汰最久未用的条目。"""
        limit = int(self.cfg.get("conv_table_size", 256))
        if len(self._conv) <= limit:
            return
        oldest = sorted(self._conv.items(), key=lambda kv: kv[1][1])[: len(self._conv) - limit]
        for k, _ in oldest:
            self._conv.pop(k, None)

    def table(self) -> dict:
        self._refresh_health()
        now = time.time()
        with self._lock:
            return {
                str(p): {
                    "cooldown_in": max(0, round(self._cooldown.get(p, 0) - now)),
                    "unhealthy": p in self._unhealthy,
                    "success": self._success.get(p, 0),
                    "quota_hint": self._quota_hint.get(p, 0),
                    "exhausted": self._exhausted(p),
                }
                for p in self.cfg["node_ports"]
            }


# ---------- SOCKS5 ----------

def _recvn(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("socks5: connection closed")
        buf += chunk
    return buf


def socks5_connect(socks_addr, dest: str, dest_port: int,
                   timeout: float) -> socket.socket:
    """dest 可为域名或 IP 字面量(v4/v6)。IP 字面量用 ATYP 直连,
    强制走对应地址族出口 —— IPv6 出口每节点唯一, 是抗 zen 限流的关键。"""
    s = socket.create_connection(socks_addr, timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    if _recvn(s, 2) != b"\x05\x00":
        s.close()
        raise OSError("socks5: method rejected")
    try:
        ip = ipaddress.ip_address(dest)
    except ValueError:
        ip = None
    if ip is None:
        hb = dest.encode("idna")
        atyp, at = 3, bytes([len(hb)]) + hb
    else:
        atyp, at = ((1 if ip.version == 4 else 4), ip.packed)
    s.sendall(b"\x05\x01\x00" + bytes([atyp]) + at
              + struct.pack(">H", dest_port))
    hdr = _recvn(s, 4)
    if hdr[1] != 0:
        s.close()
        raise OSError("socks5: connect failed rep=%d" % hdr[1])
    if atyp == 4:
        _recvn(s, 16)
    elif atyp == 3:
        _recvn(s, _recvn(s, 1)[0])
    elif atyp == 1:
        _recvn(s, 4)
    _recvn(s, 2)  # port
    return s


_IPV6_CACHE = {}


def upstream_ipv6(host: str):
    """解析域名到 IPv6 字面量(缓存 600s); 让每个节点走唯一 IPv6 出口。"""
    now = time.time()
    hit = _IPV6_CACHE.get(host)
    if hit and now - hit[0] < 600:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET6,
                                   socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    ip = infos[0][4][0].split("%")[0]
    _IPV6_CACHE[host] = (now, ip)
    return ip


class SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, socks_addr, timeout: float,
                 dest_ipv6=None):
        super().__init__(host, 443, timeout=timeout)
        self._socks_addr = socks_addr
        # Host/SNI 用域名(证书校验), 但 socks 目标强制 IPv6
        self._dest = dest_ipv6 or host

    def connect(self):
        raw = socks5_connect(self._socks_addr, self._dest, self.port,
                             self.timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)


# ---------- 用量统计 ----------

class UsageTracker:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._today = {}  # "key|model" -> counters
        self._day = ""

    def record(self, key: str, model: str, usage: dict | None,
               estimated: bool):
        if not usage:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        # zen 后端混用两种 usage 格式: DeepSeek 官方
        # (prompt_cache_hit/miss_tokens) 与 OpenAI 标准
        # (prompt_tokens_details.cached_tokens) → 两种都解析, 否则统计失真
        hit = usage.get("prompt_cache_hit_tokens") or 0
        miss = usage.get("prompt_cache_miss_tokens") or 0
        if not hit and not miss:
            ptd = usage.get("prompt_tokens_details") or {}
            cached = ptd.get("cached_tokens") or 0
            if cached:
                hit = cached
                miss = max(0, (usage.get("prompt_tokens") or 0) - cached)
        entry = {
            "ts": int(time.time()), "day": day, "key": key, "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "estimated": estimated,
        }
        with self._lock:
            if day != self._day:
                self._today, self._day = {}, day
            k = key + "|" + model
            agg = self._today.setdefault(k, {
                "requests": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0,
            })
            agg["requests"] += 1
            for f in ("prompt_tokens", "completion_tokens", "total_tokens"):
                agg[f] += entry[f]
            if DEBUG:
                hit = entry["cache_hit_tokens"]
                miss = entry["cache_miss_tokens"]
                rate = 100.0 * hit / (hit + miss) if (hit + miss) else 0.0
                print("[usage] model=%s in=%d out=%d cache_hit=%d cache_miss=%d hit_rate=%.0f%%"
                      % (entry["model"], entry["prompt_tokens"],
                         entry["completion_tokens"], hit, miss, rate), flush=True)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def today(self) -> dict:
        with self._lock:
            return dict(self._today)


# ---------- 网关 ----------

CFG = load_config()
DEBUG = os.environ.get("WARP_DEBUG") == "1"  # 诊断日志开关(默认关, 公开版无输出)
KEYS = KeyStore(CFG["keys_file"])
POOL = NodePool(CFG)
USAGE = UsageTracker(CFG["usage_log"])
SEM = threading.BoundedSemaphore(CFG["max_concurrent"])
SESSION_ID = make_session_id()   # 兜底 session(合法 descending 格式)
PROJECT_ID = _load_or_create_id("project")
_models_cache = {"ts": 0.0, "body": None}


# inference 免费档 gate 要求 tools 里必须有一个名为 read 且带 path 参数的工具
GATE_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read the contents of a file or directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File or directory to read"}},
            "required": ["path"],
        },
    },
}

def inject_gate_tools(req: dict) -> None:
    """确保 tools 数组存在且含名为 read 的工具(参数需含 path 属性)。"""
    tools = req.get("tools")
    if not isinstance(tools, list):
        tools = []
        req["tools"] = tools
    for t in tools:
        fn = (t or {}).get("function") or {}
        if fn.get("name") == "read":
            params = fn.get("parameters") or {}
            props = params.get("properties") or {}
            if "path" in props:
                return          # 已有合规 read 工具
            props["path"] = {"type": "string"}
            return
    tools.append(json.loads(json.dumps(GATE_READ_TOOL)))


def filter_models(data: dict, cfg: dict) -> dict:
    """过滤上游 /models 的列表: free_only 只留 -free; model_blacklist 拉黑。

    只影响列表, 不拦请求 —— 知道 id 的客户端依旧可以直调。
    """
    if not isinstance(data.get("data"), list):
        return data
    if cfg.get("free_only"):
        data["data"] = [m for m in data["data"] if m.get("id", "").endswith("-free")]
    blocked = set(cfg.get("model_blacklist") or ())
    if blocked:
        data["data"] = [m for m in data["data"] if m.get("id") not in blocked]
    return data


def upstream_headers(cfg: dict, ses: str | None = None) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.get("spoof_headers"):
        h["Authorization"] = "Bearer public"   # inference 免费档匿名 key, 必须显式携带
        h["User-Agent"] = "opencode/" + str(cfg["opencode_version"])
        h["x-opencode-client"] = "cli"
        # inference 免费档 gate: session 必须是合法 descending ID, 且两个头都要在
        h["x-opencode-session"] = ses or SESSION_ID
        h["x-opencode-session-id"] = ses or SESSION_ID
        h["x-opencode-project"] = PROJECT_ID
        # 注意: 不发 x-opencode-request —— 旧 zen 头, 新 inference gate 见到即 403
    return h


# 明确的"配额已用尽"标记(小写比对)。上游换措辞时宁可多列几个。
QUOTA_MARKERS = (
    b"freeusagelimiterror", b"ratelimitexceeded", b"rate_limit_exceeded",
    b"insufficient_quota", b"quota_exceeded", b"quota exceeded",
    b"quota exhausted", b"usage_limit", b"usage limit",
    b"insufficient balance", b"credit balance is too low",
    "额度不足".encode(), "配额已用尽".encode(), "已达到使用上限".encode(),
)


def is_quota_error(body: bytes) -> bool:
    """是否"配额已用尽"(而非普通速率限制/临时错误)。

    只认显式标记: 上游换措辞时判错方向的代价是不对称的 —— 漏判(当短冷却)会让 25 个
    节点被逐个试到死; 误判(当配额耗尽)会把一堆健康节点白锁到 UTC 零点。所以标记表
    宁宽(覆盖已知措辞), 但绝不把"看不出原因"的 429 归为配额耗尽。
    """
    low = body.lower()
    return any(m in low for m in QUOTA_MARKERS)


def parse_retry_after(body: bytes):
    try:
        msg = json.loads(body)
        text = json.dumps(msg)
        # zen FreeUsageLimitError 消息里通常带 retryAfter 秒数
        for key in ("retryAfter", "retry_after", "resetInSec"):
            if isinstance(msg, dict):
                v = msg.get(key) or (msg.get("error") or {}).get(key)
                if isinstance(v, (int, float)):
                    return float(v)
        import re
        m = re.search(r"retry[^0-9]{0,12}(\d{3,7})", text)
        if m:
            return float(m.group(1))
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


class UpstreamError(Exception):
    def __init__(self, status: int, body: bytes):
        self.status, self.body = status, body


def forward_once(cfg: dict, port: int, path: str, payload: bytes,
                 stream: bool, conn=None, ses: str | None = None):
    """返回 (conn, resp)。调用方负责 resp 读取完毕后 keep/drop 连接。

    conn=None 时新建 socks+TLS 连接; 传入 conn 时复用(要求前次响应已完整
    读尽)。复用连接若已被上游关闭, request 会抛 OSError, 调用方丢弃重连。
    TTFT 超时: 等响应头阶段用 ttft_timeout(短), 拿到响应头后放宽到 read_timeout
    (流式转发可能很长)。上游长时间不出首字节视为卡死 → 换节点。
    """
    dest = upstream_ipv6(cfg["upstream_host"])
    if conn is None:
        conn = SocksHTTPSConnection(
            cfg["upstream_host"], (cfg["node_host"], port),
            timeout=cfg["connect_timeout"], dest_ipv6=dest)
    if conn.sock:
        conn.sock.settimeout(cfg.get("ttft_timeout", 25))  # 首字节上限
    conn.request("POST", path, body=payload, headers=upstream_headers(cfg, ses))
    if conn.sock:
        conn.sock.settimeout(cfg.get("ttft_timeout", 25))  # 首字节上限
    resp = conn.getresponse()
    if conn.sock:
        conn.sock.settimeout(cfg.get("read_timeout", 300))  # 响应阶段放宽
    return conn, resp


def fetch_upstream(cfg: dict, port: int, path: str, payload: bytes,
                   strip_tools: bool, conn=None, ses: str | None = None):
    """处理 tools 400 兜底之外的单次请求;返回 (conn, resp, port)。"""
    body = payload
    if strip_tools:
        data = json.loads(payload)
        data.pop("tools", None)
        data.pop("tool_choice", None)
        body = json.dumps(data).encode()
    conn, resp = forward_once(cfg, port, path, body, False, conn=conn, ses=ses)
    if resp.status == 400 and not strip_tools and b'"tools"' in payload:
        POOL.drop(port, conn)   # 兜底改为不带 tools 重发, 原连接作废
        return fetch_upstream(cfg, port, path, payload, True, ses=ses)
    return conn, resp, port


RESPONSES_ERROR_TYPES = ("response.failed", "error")
RESPONSES_DELTA_TYPES = (
    "response.output_text.delta", "response.refusal.delta",
    "response.reasoning_summary_text.delta", "response.reasoning_text.delta",
    "response.function_call_arguments.delta",
)
RESPONSES_FINAL_TYPES = ("response.completed", "response.incomplete")


def scan_responses_event(ev: dict):
    """responses 协议的单个事件 → ("error"|"effective"|"prelude", detail)。

    responses 流的前导帧(created/in_progress/output_item.added)不带内容, 不能算
    "有效事件"; 真正的信号是各类 delta 或终态事件(completed/incomplete 里带完整
    response 对象)。
    """
    t = ev.get("type") or ""
    if t in RESPONSES_ERROR_TYPES or ev.get("error"):
        err = ev.get("error") or ((ev.get("response") or {}).get("error"))
        msg = err.get("message") if isinstance(err, dict) else (str(err) if err else t)
        return "error", msg or "upstream responses error"
    if t in RESPONSES_DELTA_TYPES or t in RESPONSES_FINAL_TYPES:
        return "effective", None
    return "prelude", None


def scan_prelude(buf: bytes, fmt: str = "chat"):
    """扫描首个"有效事件"之前已到达的 SSE 数据。

    返回 (state, detail):
      "error"     → 上游在给出任何内容前就在流里报错(如配额/限流), 换节点重试
      "effective" → 出现了正文/reasoning/tool_call/finish_reason → 可以提交给客户端
      "prelude"   → 只有 role/usage 之类的前置帧, 继续憋
    只解析完整的 "data: " 行; 半行留给下一轮。
    """
    for line in buf.split(b"\n"):
        if not line.startswith(b"data: "):
            continue
        data = line[6:].strip()
        if not data:
            continue
        if data == b"[DONE]":
            return "effective", None      # 极端情况: 空流但正常收尾
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if fmt == "responses":
            state, detail = scan_responses_event(ev)
            if state != "prelude":
                return state, detail
            continue
        if ev.get("error"):
            err = ev["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return "error", msg or "upstream stream error"
        for ch in ev.get("choices") or []:
            delta = ch.get("delta") or {}
            if (delta.get("content") or delta.get("reasoning_content")
                    or delta.get("tool_calls") or ch.get("finish_reason")):
                return "effective", None
    return "prelude", None


def _send_stream_head(handler, port: int):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")  # 无 Content-Length, 流完即关
    handler.send_header("X-Warp-Pool-Node", str(port))
    handler.end_headers()
    handler.close_connection = True


def _send_json(handler, body: bytes, port: int):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Warp-Pool-Node", str(port))
    handler.end_headers()
    handler.wfile.write(body)


def relay_stream(handler, conn, resp, key: str, model: str, port: int, usagenorm=None,
                 fmt: str = "chat") -> bool:
    """SSE 透传 + 从尾部 chunk 抓 usage。返回 True=已提交给客户端, False=换节点重试。

    首事件护栏: 上游可能先回 200 再在流里报错(配额/限流), 或干脆不出帧。在出现第一个
    "有效事件"前只憋在内存里、不给客户端发任何字节; 若先到的是 error 事件或首事件超时
    → 返回 False 交由调用方换节点(重试对客户端完全无感)。一旦提交, 后续失败只能就地
    透传 —— 已发出去的流不可重放。
    """
    first_timeout = float(CFG.get("first_event_timeout", 0) or 0)
    read_timeout = CFG.get("read_timeout", 300)
    pending = b""
    committed = False
    drained = False
    tail = b""
    usage = None
    try:
        while True:
            if getattr(conn, "sock", None) is not None:
                conn.sock.settimeout(first_timeout if (first_timeout > 0 and not committed)
                                     else read_timeout)
            chunk = resp.read1(16384)
            if not chunk:
                drained = True
                break
            if not committed:
                pending += chunk
                state, _detail = scan_prelude(pending, fmt)
                if state == "error":
                    break                      # 未提交 → 换节点
                if state != "effective":
                    continue                   # 只收到前置帧 → 继续憋
                committed = True               # 提交: 先补发憋住的全部帧
                _send_stream_head(handler, port)
                handler.wfile.write(pending)
                handler.wfile.flush()
                tail = pending[-8192:]
                pending = b""
                continue
            handler.wfile.write(chunk)
            handler.wfile.flush()
            tail = (tail + chunk)[-8192:]
    except (BrokenPipeError, ConnectionResetError):
        pass  # 客户端断流 → 不复用
    except socket.timeout:
        pass  # 未提交=等首事件超时(换节点); 已提交=流中途卡住(就地结束)
    finally:
        if getattr(conn, "sock", None) is not None:
            conn.sock.settimeout(read_timeout)
        if committed and drained and conn.sock:
            POOL.keep(port, conn)   # 归还池, 下个请求省去 socks+TLS 建连
        else:
            POOL.drop(port, conn)
    if not committed:
        return False
    for line in tail.split(b"\n"):
        if line.startswith(b"data: ") and b'"usage"' in line:
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if fmt == "responses":
                usage = (ev.get("response") or {}).get("usage") or ev.get("usage") or usage
            else:
                usage = ev.get("usage") or usage
    if usagenorm:
        usage = usagenorm(usage)
    USAGE.record(key, model, usage, estimated=usage is None)
    return True


def relay_aggregate(handler, conn, resp, key: str, model: str, port: int, usagenorm=None,
                    fmt: str = "chat") -> bool:
    """上游强制流式; 客户端要非流式时聚合 SSE 还原成完整 JSON。

    chat 协议: 还原成 chat.completion; responses 协议: 直接用终态事件里上游给的完整
    response 对象(不自拼, 免得漏字段)。
    返回 True=已写出响应, False=首事件前就失败(换节点重试)。
    """
    first_timeout = float(CFG.get("first_event_timeout", 0) or 0)
    read_timeout = CFG.get("read_timeout", 300)
    content_parts = []
    reasoning_parts = []
    tool_calls = {}          # index -> {id, name, arguments}
    finish_reason = None
    usage = None
    final = None             # responses: 终态事件里的完整 response 对象
    reusable = False
    seen_content = False     # 已拿到正文 → 之后的 error 不再当节点故障
    done_seen = False        # 上游干净收尾: 即使一个字都没出, 也算"正常但空"的回复
    bail = False
    buf = b""
    try:
        while True:
            if getattr(conn, "sock", None) is not None:
                conn.sock.settimeout(first_timeout if (first_timeout > 0 and not seen_content)
                                     else read_timeout)
            chunk = resp.read1(16384)
            if not chunk:
                reusable = True
                break
            buf += chunk
            lines = buf.split(b"\n")
            buf = lines.pop()   # 末尾可能是半行, 留下一轮再拼
            for line in lines:
                if not line.startswith(b"data: "):
                    continue
                data = line[6:].strip()
                if not data:
                    continue
                if data == b"[DONE]":
                    done_seen = True
                    continue
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if fmt == "responses":
                    state, detail = scan_responses_event(ev)
                    if state == "error" and not seen_content:
                        print("[first-event] error event before content: node=%d %s"
                              % (port, str(detail)[:200]), flush=True)
                        bail = True
                        break
                    if state == "effective":
                        seen_content = True
                    if (ev.get("type") or "") in RESPONSES_FINAL_TYPES:
                        final = ev.get("response") or final
                        done_seen = True
                    usage = (ev.get("response") or {}).get("usage") or usage
                    continue
                if ev.get("error") and not seen_content:
                    print("[first-event] error event before content: node=%d %s"
                          % (port, str(ev["error"])[:200]), flush=True)
                    bail = True
                    break
                usage = ev.get("usage") or usage
                for ch in ev.get("choices") or []:
                    finish_reason = ch.get("finish_reason") or finish_reason
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                    rc = delta.get("reasoning_content")
                    if rc:
                        reasoning_parts.append(rc)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if (delta.get("content") or delta.get("reasoning_content")
                            or delta.get("tool_calls") or ch.get("finish_reason")):
                        seen_content = True
            if bail:
                break
    except (BrokenPipeError, ConnectionResetError, socket.timeout):
        bail = not seen_content   # 等首个有效事件时超时 → 当节点故障重试
    finally:
        if getattr(conn, "sock", None) is not None:
            conn.sock.settimeout(read_timeout)
        if reusable and conn.sock:
            POOL.keep(port, conn)
        else:
            POOL.drop(port, conn)
    if bail or not (seen_content or done_seen):
        return False    # 一个字都没产出且上游没收尾 → 换节点重试(客户端还没收到任何东西)

    if fmt == "responses":
        if final is None:
            return False    # 流被截断, 没等到终态事件 → 换节点
        out = dict(final)
        out.setdefault("model", model)
        if usagenorm:
            usage = usagenorm(usage)
        USAGE.record(key, model, usage, estimated=usage is None)
        _send_json(handler, json.dumps(out, ensure_ascii=False).encode(), port)
        return True

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [
            {"id": slot["id"] or ("call_%d" % i), "type": "function",
             "function": {"name": slot["name"], "arguments": slot["arguments"]}}
            for i, slot in sorted(tool_calls.items())]
    if usagenorm:
        usage = usagenorm(usage)
    out = {
        "id": "chatcmpl-warp-" + uuid.uuid4().hex[:16],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    USAGE.record(key, model, usage, estimated=usage is None)
    _send_json(handler, json.dumps(out, ensure_ascii=False).encode(), port)
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "warp-pool/1.0"

    def log_message(self, fmt, *args):
        pass  # journald 噪音控制; 关键事件显式 log

    def _auth_key(self):
        # Bearer 大小写不敏感(RFC 6750: scheme 大小写不敏感)
        parts = self.headers.get("Authorization", "").split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return self.headers.get("x-api-key")

    def _json(self, status: int, obj: dict, headers: dict | None = None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, msg: str, typ="rate_limit_error",
             headers: dict | None = None):
        self._json(status, {"error": {"message": msg, "type": typ}}, headers=headers)

    def _auth_error(self):
        """401: 区分「没带 key」与「key 无效」(排障时一眼知道是哪种),
        并附 WWW-Authenticate: 标准客户端收到 401 后会据此重新协商凭据。"""
        if not (self.headers.get("Authorization") or self.headers.get("x-api-key")):
            msg = "missing api key (send 'Authorization: Bearer <key>' or 'x-api-key')"
        else:
            msg = "invalid api key"
        self._json(401, {"error": {"message": msg, "type": "auth_error"}},
                   headers={"WWW-Authenticate": 'Bearer realm="warp-pool"'})

    # ---- GET ----
    def do_GET(self):
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        key = self._auth_key()
        if not key or not KEYS.valid(key):
            self._auth_error()
            return
        if self.path == "/v1/models":
            self._handle_models()
        elif self.path == "/stats":
            self._json(200, {"today": USAGE.today(), "nodes": POOL.table(),
                             "pool": POOL.pool_stats()})
        else:
            self._err(404, "not found", "not_found")

    def _handle_models(self):
        now = time.time()
        if _models_cache["body"] and now - _models_cache["ts"] < CFG["models_cache_sec"]:
            body = _models_cache["body"]
        else:
            port = POOL.pick()
            if port is None:
                self._err(503, "no healthy upstream node")
                return
            try:
                dest = upstream_ipv6(CFG["upstream_host"])
                conn = SocksHTTPSConnection(
                    CFG["upstream_host"], (CFG["node_host"], port),
                    timeout=CFG["connect_timeout"], dest_ipv6=dest)
                conn.request("GET", CFG["upstream_base"] + "/models",
                             headers=upstream_headers(CFG))
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                if resp.status != 200:
                    self._err(resp.status, raw.decode("utf-8", "replace"))
                    return
                POOL.record_success(port)  # models 请求同样消耗 IP 配额
                data = filter_models(json.loads(raw), CFG)
                body = json.dumps(data, ensure_ascii=False).encode()
                _models_cache.update(ts=now, body=body)
            except (OSError, json.JSONDecodeError) as e:
                POOL.cool_short(port, 30)
                self._err(502, "upstream error: " + str(e), "upstream_error")
                return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- POST ----
    def do_POST(self):
        key = self._auth_key()
        if not key or not KEYS.valid(key):
            self._auth_error()
            return
        if self.path not in ("/v1/chat/completions", "/v1/responses"):
            self._err(404, "not found", "not_found")
            return
        is_responses = self.path == "/v1/responses"
        # 请求体边界: 缺 CL / 空体 / CL 非法 / 超限都明确报错, 不把线程吊在读上
        max_body = int(CFG.get("max_body_bytes", 64 * 1024 * 1024))
        try:
            length = int(self.headers.get("Content-Length", -1))
        except ValueError:
            self._err(400, "invalid Content-Length", "invalid_request")
            return
        if length < 0:
            self._err(400, "missing request body (set Content-Length)", "invalid_request")
            return
        if length == 0:
            self._err(400, "empty request body", "invalid_request")
            return
        if length > max_body:
            self._err(413, "request body too large (max %d bytes)" % max_body,
                      "invalid_request")
            return
        payload = self.rfile.read(length)
        try:
            req = json.loads(payload)
        except json.JSONDecodeError:
            self._err(400, "invalid json", "invalid_request")
            return
        model = req.get("model", "")
        client_stream = bool(req.get("stream"))
        if DEBUG:
            h = self.headers
            print("[hdr] ua=%s xos=%s xreq=%s user=%r n_msg=%d first=%s last=%s"
                  % ((h.get("User-Agent") or "")[:36], (h.get("x-opencode-session") or "")[:12],
                     (h.get("x-opencode-request") or "")[:8], req.get("user"),
                     len(req.get("messages", [])),
                     (req.get("messages") or [{}])[0].get("role"),
                     (req.get("messages") or [{}])[-1].get("role")), flush=True)
        if DEBUG:  # 调试: 记录会触发上游 thinking 校验的请求形态(只打结构, 不打内容)
            try:
                a = [m.get("role") for m in req.get("messages", [])]
                rc = [len(m.get("reasoning_content") or "") for m in req.get("messages", []) if m.get("reasoning_content")]
                tk = [bool(m.get("tool_calls")) for m in req.get("messages", []) if m.get("role") == "assistant"]
                print("[req] model=%s stream=%s thinking=%s roles=%s rc_len=%s asst_toolcalls=%s"
                      % (model, client_stream, req.get("thinking"), a, rc, tk), flush=True)
            except Exception:
                pass
        if is_responses:
            # responses 协议: gate 同样要求 stream+tools; 工具格式是平铺 {name, parameters}。
            # 上游还要求至少一个输出控制字段(stream_options 或 max_output_tokens), 缺了
            # 直接 400 ModelProtocolUnsupported; 这里统一补 stream_options(不硬加
            # max_output_tokens, 免得改客户端想要的输出上限)。
            req["stream"] = True
            req.setdefault("stream_options", {})["include_usage"] = True
            rtools = req.get("tools")
            if not isinstance(rtools, list):
                rtools = []
                req["tools"] = rtools
            if not any((t or {}).get("name") == "read" for t in rtools):
                rtools.append({"type": "function", "name": "read", "description": "Read a file",
                               "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}})
            payload = json.dumps(req).encode()
            fp = responses_fingerprint(req)
            self._chat_with_retry(key, model, payload, client_stream, fp=fp,
                                  upath="/responses", usagenorm=normalize_responses_usage,
                                  aggregate=not client_stream, fmt="responses")
            return
        # pi/Claude 系客户端对 reasoning 模型会发 developer role, zen 只认 system
        for m in req.get("messages", []):
            if m.get("role") == "developer":
                m["role"] = "system"
        # 网关兜底: DeepSeek 上游要求 thinking 历史里每条 assistant 消息都带
        # reasoning_content 字段(空串即可); pi 未配 compat 时可能缺字段→400, 这里补上
        for m in req.get("messages", []):
            if m.get("role") == "assistant" and "reasoning_content" not in m:
                m["reasoning_content"] = ""
        # inference gate: 必须 stream=true + tools 含 read → 注入后强制流式;
        # 客户端要非流式时由网关聚合 SSE 还原成完整 JSON
        req["stream"] = True
        req.setdefault("stream_options", {})["include_usage"] = True
        inject_gate_tools(req)
        payload = json.dumps(req).encode()
        fp = fingerprint(req.get("messages", []))
        self._chat_with_retry(key, model, payload, client_stream, fp=fp,
                              aggregate=not client_stream)

    def _chat_with_retry(self, key: str, model: str, payload: bytes,
                         stream: bool, fp: str | None = None,
                         upath: str = "/chat/completions", usagenorm=None,
                         aggregate: bool = False, fmt: str = "chat"):
        """重试循环。SEM 只包「排队→建连→等响应头」(短阶段):
        拿到 200 后立即释放建连槽, 流式转发不占槽 —— 长流不再堵死后续请求。

        429(日配额) → 冷却到 UTC 零点 + 学习配额上限, 立即换节点;
        排队超过 queue_timeout → 503 快速失败(不无限阻塞)。
        """
        path = CFG["upstream_base"] + upath
        tried = set()
        t_req = time.time()  # TTFT 计时: 从网关收到完整请求到上游响应头
        last_status, last_body = 502, b"no node available"
        retry_after = 30  # 终态错误时以 Retry-After 头告知客户端退避秒数
        ses = session_for_fp(fp)   # 同对话同 session → 上游前缀缓存命中
        for _ in range(CFG["max_attempts"]):
            port = POOL.pick(key, fp=fp, exclude=tried)
            if port is None:
                break
            tried.add(port)
            acquired = SEM.acquire(timeout=CFG.get("queue_timeout", 15))
            if not acquired:
                last_status, last_body = 503, b"gateway busy: queue timeout"
                retry_after = max(retry_after, 2)
                break
            released = False
            try:
                try:
                    idled = POOL.borrow(port)  # 复用空闲连接, None=新建
                    conn, resp, port = fetch_upstream(
                        CFG, port, path, payload, strip_tools=False,
                        conn=idled, ses=ses)
                except (OSError, socket.timeout) as e:
                    if idled is not None:
                        # 复用的空闲连接已失效(上游关/换 IP) → 丢弃后
                        # 同节点新建连接重试一次, 不烧 attempt 数
                        POOL.drop(port, idled)
                        try:
                            conn, resp, port = fetch_upstream(
                                CFG, port, path, payload, strip_tools=False, ses=ses)
                        except (OSError, socket.timeout) as e2:
                            POOL.cool_short(port, 30)
                            last_status, last_body = 502, ("node error: " + str(e2)).encode()
                            continue
                    else:
                        POOL.cool_short(port, 30)
                        last_status, last_body = 502, ("node error: " + str(e)).encode()
                        continue
                if resp.status == 429:
                    body = resp.read()
                    POOL.drop(port, conn)
                    ra = parse_retry_after(body)
                    # 日配额限流: 该 IP 当天废了 → 冷却到 UTC 零点
                    if is_quota_error(body):
                        POOL.cool_daily(port, ra)
                        retry_after = max(retry_after,
                                          int(ra) if ra else int(utc_midnight_ts() - time.time()))
                    else:
                        POOL.cool_short(port, ra if ra else 60)
                        retry_after = max(retry_after, int(ra) if ra else 60)
                    last_status, last_body = 429, body
                    continue  # 立即换下一节点重试(成本 ~0.1s)
                if resp.status >= 500:
                    body = resp.read()
                    POOL.drop(port, conn)
                    POOL.cool_short(port, 15)
                    last_status, last_body = resp.status, body
                    continue
                if resp.status != 200:
                    body = resp.read()
                    POOL.drop(port, conn)
                    self._relay_raw(resp.status, body)
                    return
                # 200: 上游已开始计费/计配额 → 记成功, 释放建连槽, 转发不占槽
                POOL.record_success(port)
                if DEBUG:
                    print("[ttft] model=%s ttft=%.2fs node=%d stream=%s"
                          % (model, time.time() - t_req, port, stream), flush=True)
                SEM.release()
                released = True
                if aggregate:
                    ok = relay_aggregate(self, conn, resp, key, model, port, usagenorm, fmt=fmt)
                elif stream or resp.headers.get("Content-Type", "").startswith(
                        "text/event-stream"):
                    ok = relay_stream(self, conn, resp, key, model, port, usagenorm, fmt=fmt)
                else:
                    body = resp.read()   # 完整读完 → 归还池复用
                    POOL.keep(port, conn)
                    try:
                        usage = json.loads(body).get("usage")
                    except json.JSONDecodeError:
                        usage = None
                    if usagenorm:
                        usage = usagenorm(usage)
                    USAGE.record(key, model, usage, estimated=usage is None)
                    self._relay_raw(200, body, node=port)
                    return
                if ok:
                    return
                # 首事件前就失败(流内 error 事件/首事件超时/空流): 客户端一个字节都没收到
                # → 当该节点故障换下一个。注: 上面的 record_success 已经记过这次成功,
                # 会轻微高估该节点用量(宁可高估把节点提前轮换, 也不漏记)。
                POOL.drop(port, conn)
                POOL.cool_short(port, 20)
                last_status, last_body = 502, b"upstream produced no output before first event"
                continue
            finally:
                if not released:
                    SEM.release()
        # 全节点尝试完: 按真实失败类别报终态(旧实现一律报 rate_limit "pool exhausted",
        # 客户端分不清"被限流"还是"节点故障", 也没有退避提示 → 高并发下风暴式重试)
        if last_status == 429:
            try:
                msg = json.loads(last_body)
                if not isinstance(msg, dict) or "error" not in msg:
                    raise ValueError
            except (json.JSONDecodeError, TypeError, ValueError):
                msg = {"error": {"message": "upstream rate limited, pool exhausted",
                                 "type": "rate_limit_error"}}
            self._json(429, msg, headers={"Retry-After": str(max(1, retry_after))})
            return
        try:
            parsed = json.loads(last_body)
            if isinstance(parsed, dict) and "error" in parsed:
                self._json(503, parsed, headers={"Retry-After": str(max(1, retry_after))})
                return
        except (json.JSONDecodeError, TypeError):
            pass
        text = last_body.decode("utf-8", "replace").strip() if last_body else ""
        if not text:
            text = "no healthy upstream node available, retry later"
        self._json(503, {"error": {"message": text, "type": "server_error"}},
                   headers={"Retry-After": str(max(1, retry_after))})

    def _relay_raw(self, status: int, body: bytes, node: int | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if node:
            self.send_header("X-Warp-Pool-Node", str(node))
        self.end_headers()
        self.wfile.write(body)


class WarpHTTPServer(ThreadingHTTPServer):
    # 默认 backlog=5: 高并发(>5 连接)时内核直接 RST 超出的连接 → 偶发 ConnectionResetError。
    # 网关面向多客户端长流, 128 与并发量级匹配。
    request_queue_size = 128
    daemon_threads = True


def main():
    listen = CFG["listen"]
    host, _, port = listen.rpartition(":")
    srv = WarpHTTPServer((host or "0.0.0.0", int(port)), Handler)
    print("[warp-gateway] listen %s nodes=%d upstream=%s%s session=%s..."
          % (listen, len(CFG["node_ports"]), CFG["upstream_host"],
             CFG["upstream_base"], SESSION_ID[:8]), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
