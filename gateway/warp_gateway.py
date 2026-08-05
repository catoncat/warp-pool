#!/usr/bin/env python3
"""warp-pool gateway — OpenAI 兼容网关，按请求 round-robin 走 wireproxy WARP 节点池。

设计依据（2026-08 zen 服务端源码实证）:
- zen 匿名限流按 IP 按天（UTC 零点重置），新 IP 有 2x 日配额
- 每请求换一个 WARP 出口 IP = 限流难以触发；IP 被限则冷却到 UTC 零点
- 官方 CLI 请求头检查当前在服务端被注释停用，仍默认发送（未来恢复即生效）

仅依赖 Python 标准库（目标机 OpenCloudOS 无 pip）。
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
    "upstream_base": "/zen/v1",
    "node_ports": [10800],
    "node_host": "127.0.0.1",
    "keys_file": os.path.join(BASE, "keys.txt"),
    "nodes_state_file": os.path.join(STATE_DIR, "nodes.json"),
    "usage_log": os.path.join(STATE_DIR, "usage.jsonl"),
    "max_concurrent": 8,
    "max_attempts": 4,
    "connect_timeout": 15,
    "read_timeout": 300,
    "models_cache_sec": 300,
    "free_only": True,
    "spoof_headers": True,
    "opencode_version": "1.0.0",
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


# ---------- keys.txt 热加载 ----------

class KeyStore:
    def __init__(self, path: str):
        self.path = path
        self._mtime = 0.0
        self._keys: set[str] = set()

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

class NodePool:
    """round-robin + 冷却表。冷却原因:
    - daily 429: 到 UTC 零点（zen 按天限额）
    - 短 429/网络错误: 短冷却（tps 限流 / 节点异常）
    - health.sh 写入的 nodes.json: ok=false 的节点跳过
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cooldown: dict[int, float] = {}   # port -> 冷却截止 ts
        self._rr = itertools.count()
        self._state_mtime = 0.0
        self._unhealthy: set[int] = set()

    def _refresh_health(self):
        path = self.cfg["nodes_state_file"]
        try:
            mtime = os.stat(path).st_mtime
        except FileNotFoundError:
            return
        if mtime == self._state_mtime:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._unhealthy = {
                int(p) for p, st in data.items() if not st.get("ok", True)
            }
            self._state_mtime = mtime
        except (json.JSONDecodeError, OSError):
            pass

    def pick(self, exclude: set[int] | None = None) -> int | None:
        self._refresh_health()
        now = time.time()
        ports = self.cfg["node_ports"]
        with self._lock:
            for _ in range(len(ports)):
                port = ports[next(self._rr) % len(ports)]
                if exclude and port in exclude:
                    continue
                if port in self._unhealthy:
                    continue
                if self._cooldown.get(port, 0) > now:
                    continue
                return port
        return None

    def cool_daily(self, port: int, retry_after: float | None):
        until = time.time() + retry_after if retry_after else utc_midnight_ts()
        # daily 限流的 retryAfter 就是到 UTC 零点的秒数；封顶到零点
        until = min(until, utc_midnight_ts())
        with self._lock:
            self._cooldown[port] = max(self._cooldown.get(port, 0), until)

    def cool_short(self, port: int, sec: float = 60):
        with self._lock:
            self._cooldown[port] = max(
                self._cooldown.get(port, 0), time.time() + sec)

    def table(self) -> dict:
        self._refresh_health()
        now = time.time()
        with self._lock:
            return {
                str(p): {
                    "cooldown_in": max(0, round(self._cooldown.get(p, 0) - now)),
                    "unhealthy": p in self._unhealthy,
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
        raise OSError(f"socks5: connect failed rep={hdr[1]}")
    if atyp == 4:
        _recvn(s, 16)
    elif atyp == 3:
        _recvn(s, _recvn(s, 1)[0])
    elif atyp == 1:
        _recvn(s, 4)
    _recvn(s, 2)  # port
    return s


_IPV6_CACHE: dict = {}


def upstream_ipv6(host: str) -> str | None:
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
                 dest_ipv6: str | None = None):
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
        self._today: dict[str, dict] = {}  # "key|model" -> counters
        self._day = ""

    def record(self, key: str, model: str, usage: dict | None,
               estimated: bool):
        if not usage:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        entry = {
            "ts": int(time.time()), "day": day, "key": key, "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "estimated": estimated,
        }
        with self._lock:
            if day != self._day:
                self._today, self._day = {}, day
            k = f"{key}|{model}"
            agg = self._today.setdefault(k, {
                "requests": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0,
            })
            agg["requests"] += 1
            for f in ("prompt_tokens", "completion_tokens", "total_tokens"):
                agg[f] += entry[f]
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
SESSION_ID = uuid.uuid4().hex
PROJECT_ID = uuid.uuid4().hex
_models_cache: dict = {"ts": 0.0, "body": None}


def upstream_headers(cfg: dict) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.get("spoof_headers"):
        h["User-Agent"] = f"opencode/{cfg['opencode_version']}"
        h["x-opencode-client"] = "cli"
        h["x-opencode-session"] = SESSION_ID
        h["x-opencode-project"] = PROJECT_ID
        h["x-opencode-request"] = uuid.uuid4().hex
    return h


def parse_retry_after(body: bytes) -> float | None:
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
                 stream: bool):
    """返回 (conn, resp)。调用方负责 resp 读取完毕后 conn.close()。"""
    dest = upstream_ipv6(cfg["upstream_host"])
    conn = SocksHTTPSConnection(
        cfg["upstream_host"], (cfg["node_host"], port),
        timeout=cfg["connect_timeout"], dest_ipv6=dest)
    conn.request("POST", path, body=payload, headers=upstream_headers(cfg))
    if conn.sock:
        conn.sock.settimeout(cfg["read_timeout"])  # 流式读放宽超时
    resp = conn.getresponse()
    return conn, resp


def fetch_upstream(cfg: dict, port: int, path: str, payload: bytes,
                   strip_tools: bool):
    """处理 429 冷却、tools 400 兜底之外的单次请求;返回 (conn, resp, port)。"""
    body = payload
    if strip_tools:
        data = json.loads(payload)
        data.pop("tools", None)
        data.pop("tool_choice", None)
        body = json.dumps(data).encode()
    conn, resp = forward_once(cfg, port, path, body, False)
    if resp.status == 400 and not strip_tools and b'"tools"' in payload:
        conn.close()
        return fetch_upstream(cfg, port, path, payload, True)
    return conn, resp, port


def relay_stream(handler, conn, resp, key: str, model: str):
    """SSE 透传 + 从尾部 chunk 抓 usage。"""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "close")  # 无 Content-Length, 流完即关
    handler.end_headers()
    handler.close_connection = True
    tail = b""
    usage = None
    try:
        while True:
            chunk = resp.read1(16384)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
            tail = (tail + chunk)[-8192:]
    except (BrokenPipeError, ConnectionResetError, socket.timeout):
        pass
    finally:
        conn.close()
    for line in tail.split(b"\n"):
        if line.startswith(b"data: ") and b'"usage"' in line:
            try:
                usage = json.loads(line[6:]).get("usage") or usage
            except json.JSONDecodeError:
                pass
    USAGE.record(key, model, usage, estimated=usage is None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "warp-pool/1.0"

    def log_message(self, fmt, *args):
        pass  # journald 噪音控制; 关键事件显式 log

    def _auth_key(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return self.headers.get("x-api-key")

    def _json(self, status: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, msg: str, typ="rate_limit_error"):
        self._json(status, {"error": {"message": msg, "type": typ}})

    # ---- GET ----
    def do_GET(self):
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        key = self._auth_key()
        if not key or not KEYS.valid(key):
            self._err(401, "invalid api key", "auth_error")
            return
        if self.path == "/v1/models":
            self._handle_models()
        elif self.path == "/stats":
            self._json(200, {"today": USAGE.today(), "nodes": POOL.table()})
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
                conn.request("GET", f"{CFG['upstream_base']}/models",
                             headers=upstream_headers(CFG))
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                if resp.status != 200:
                    self._err(resp.status, raw.decode("utf-8", "replace"))
                    return
                data = json.loads(raw)
                if CFG.get("free_only") and isinstance(data.get("data"), list):
                    data["data"] = [m for m in data["data"]
                                    if m.get("id", "").endswith("-free")]
                body = json.dumps(data, ensure_ascii=False).encode()
                _models_cache.update(ts=now, body=body)
            except (OSError, json.JSONDecodeError) as e:
                POOL.cool_short(port, 30)
                self._err(502, f"upstream error: {e}", "upstream_error")
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
            self._err(401, "invalid api key", "auth_error")
            return
        if self.path != "/v1/chat/completions":
            self._err(404, "not found", "not_found")
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        try:
            req = json.loads(payload)
        except json.JSONDecodeError:
            self._err(400, "invalid json", "invalid_request")
            return
        model = req.get("model", "")
        stream = bool(req.get("stream"))
        if DEBUG:  # 调试: 记录会触发上游 thinking 校验的请求形态(只打结构, 不打内容)
            try:
                a = [m.get("role") for m in req.get("messages", [])]
                rc = [len(m.get("reasoning_content") or "") for m in req.get("messages", []) if m.get("reasoning_content")]
                tk = [bool(m.get("tool_calls")) for m in req.get("messages", []) if m.get("role") == "assistant"]
                print(f"[req] model={model} stream={stream} thinking={req.get('thinking')} "
                      f"roles={a} rc_len={rc} asst_toolcalls={tk}", flush=True)
            except Exception:
                pass
        # pi/Claude 系客户端对 reasoning 模型会发 developer role, zen 只认 system
        for m in req.get("messages", []):
            if m.get("role") == "developer":
                m["role"] = "system"
        if stream:
            req.setdefault("stream_options", {})["include_usage"] = True
            payload = json.dumps(req).encode()
        with SEM:
            self._chat_with_retry(key, model, payload, stream)

    def _chat_with_retry(self, key: str, model: str, payload: bytes,
                         stream: bool):
        path = f"{CFG['upstream_base']}/chat/completions"
        tried: set[int] = set()
        last_status, last_body = 502, b"no node available"
        for _ in range(CFG["max_attempts"]):
            port = POOL.pick(exclude=tried)
            if port is None:
                break
            tried.add(port)
            try:
                conn, resp, port = fetch_upstream(CFG, port, path, payload,
                                                  strip_tools=False)
            except (OSError, socket.timeout) as e:
                POOL.cool_short(port, 30)
                last_status, last_body = 502, f"node error: {e}".encode()
                continue
            if resp.status == 429:
                body = resp.read()
                conn.close()
                # zen 日配额限流(FreeUsageLimitError): 该 IP 当天废了 → 冷却到 UTC 零点
                if b"FreeUsageLimitError" in body or b"rateLimitExceeded" in body:
                    POOL.cool_daily(port)
                else:
                    ra = parse_retry_after(body)
                    POOL.cool_short(port, ra if ra else 60)
                last_status, last_body = 429, body
                continue  # 立即换下一节点重试(成本 ~0.1s)
            if resp.status >= 500:
                body = resp.read()
                conn.close()
                POOL.cool_short(port, 15)
                last_status, last_body = resp.status, body
                continue
            if resp.status != 200:
                body = resp.read()
                conn.close()
                self._relay_raw(resp.status, body)
                return
            # 200
            if stream or resp.headers.get("Content-Type", "").startswith(
                    "text/event-stream"):
                relay_stream(self, conn, resp, key, model)
            else:
                body = resp.read()
                conn.close()
                try:
                    usage = json.loads(body).get("usage")
                except json.JSONDecodeError:
                    usage = None
                USAGE.record(key, model, usage, estimated=usage is None)
                self._relay_raw(200, body, node=port)
            return
        status = last_status if last_status in (429,) else 503
        try:
            msg = json.loads(last_body)
        except (json.JSONDecodeError, TypeError):
            msg = {"error": {"message": "upstream rate limited, pool exhausted",
                             "type": "rate_limit_error"}}
        self._json(status, msg)

    def _relay_raw(self, status: int, body: bytes, node: int | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if node:
            self.send_header("X-Warp-Pool-Node", str(node))
        self.end_headers()
        self.wfile.write(body)


def main():
    listen = CFG["listen"]
    host, _, port = listen.rpartition(":")
    srv = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    srv.daemon_threads = True
    print(f"[warp-gateway] listen {listen} nodes={CFG['node_ports']} "
          f"upstream={CFG['upstream_host']}{CFG['upstream_base']}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
