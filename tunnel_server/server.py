"""
Tunnel head — runs on the PUBLIC server. Self-hosted ngrok.

BrightysKingdom (on the local machine) dials *out* to this process and holds a
WebSocket open. Anything this process receives on its forwarding port is
serialised down that socket, answered by the Kingdom against whichever local
service its BACKEND_ROUTES map to, and streamed back.

    [public server]                              [local machine]
    tunnel_server                                 BrightysKingdom :2000
      :9000  /tunnel-agent  <===== WebSocket ==== tunnel_client (dials out)
      :9001  /<anything>    ─ forwarded ─┘              │
         ▲                                              ├─ /app1   → :3000
         │ chatbot containers                           ├─ /app2   → :5000
         └─ http://host.docker.internal:9001/ollama/... └─ /ollama → :11434

The local machine needs no public IP, no port forward, no inbound firewall
rule. Only this server does.

TWO PORTS, ON PURPOSE
---------------------
The forwarding surface has no authentication — it cannot have any, because the
chatbot's embedding client (langchain_ollama) sends no auth header and offers
no hook to add one. So the two surfaces are split across separate listeners:

  * PUBLIC port  — serves ONLY /tunnel-agent, and that is token-authenticated.
                   This is the one you expose to the internet.
  * LOCAL port   — serves the forwarding surface. Bind it where only your own
                   containers can reach it, and firewall it from the internet.

Collapsing these onto one port would publish an unauthenticated route to every
service on the local machine.

PROTOCOL (JSON text frames, bodies base64 so a text frame can carry bytes)
--------------------------------------------------------------------------
    head -> client:  {"t":"req","id","method","path","query","headers","body"}
    client -> head:  {"t":"hello","token","name"}     (first frame)
                     {"t":"head","id","status","headers"}
                     {"t":"chunk","id","data"}        (zero or more)
                     {"t":"end","id"} | {"t":"err","id","error"}

Responses stream rather than buffer: SSE chat emits tokens for tens of seconds
and /api/pull emits progress for minutes.

WebSocket connections are carried too, multiplexed over the same tunnel by
session id, so Socket.IO backends behind the Kingdom work through the tunnel:

    head -> client:  {"t":"ws_open","id","path","query","headers","protocols"}
    client -> head:  {"t":"ws_opened","id"} | {"t":"ws_error","id","error"}
    both ways:       {"t":"ws_msg","id","data","binary"}
                     {"t":"ws_close","id"}

RUN
---
    python tunnel_server/server.py
"""

import base64
import collections
import hmac
import json
import os
import sys
import time
import uuid

from gevent import monkey

monkey.patch_all()

import gevent  # noqa: E402
import requests  # noqa: E402
from flask import Blueprint, Flask, Response, request  # noqa: E402
from gevent.lock import Semaphore  # noqa: E402
from gevent.pywsgi import WSGIServer  # noqa: E402
from gevent.queue import Empty, Queue  # noqa: E402
from geventwebsocket.handler import WebSocketHandler  # noqa: E402

try:
    # Optional: lets run.sh / a bare `python server.py` pick up a .env on the
    # server. Kept optional so this file stays runnable with only flask,
    # gevent and gevent-websocket installed. PM2 supplies env directly.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# log() is defined here rather than further down because the configuration
# below parses TUNNEL_FALLBACKS at import time and reports bad entries.
def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# --- configuration ----------------------------------------------------------

TUNNEL_TOKEN = os.getenv("TUNNEL_TOKEN", "")
PUBLIC_PORT = int(os.getenv("TUNNEL_PUBLIC_PORT", "9000"))
PUBLIC_BIND = os.getenv("TUNNEL_PUBLIC_BIND", "0.0.0.0")
LOCAL_PORT = int(os.getenv("TUNNEL_LOCAL_PORT", "9001"))
LOCAL_BIND = os.getenv("TUNNEL_LOCAL_BIND", "0.0.0.0")

# A cold 14B model load measured ~20s before the first token, so keep headroom.
FIRST_BYTE_TIMEOUT = int(os.getenv("TUNNEL_FIRST_BYTE_TIMEOUT", "120"))
IDLE_TIMEOUT = int(os.getenv("TUNNEL_IDLE_TIMEOUT", "300"))

# Headers describing the hop that just ended, not the one about to be made.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length", "host",
}
# The client already decompressed the body (requests' iter_content gunzips
# transparently), so echoing Content-Encoding would make callers double-decode.
STRIP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-encoding"}

# Handshake headers the far end's WebSocket client regenerates for its own
# connection. Forwarding ours would make it negotiate against a stale key.
WS_SKIP_HEADERS = HOP_BY_HOP | {
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-accept", "sec-websocket-protocol",
}

# How long to wait for the far end to report the backend socket is open.
WS_OPEN_TIMEOUT = int(os.getenv("TUNNEL_WS_OPEN_TIMEOUT", "30"))


def _flag(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _parse_fallbacks(raw):
    """Parse TUNNEL_FALLBACKS into [(prefix, base_url)], longest prefix first.

    Format:  /ollama=http://127.0.0.1:11434,/app1=http://backup:3000

    Longest-prefix ordering matters so a specific /ollama/embed entry can win
    over a general /ollama one.
    """
    raw = raw or ""
    # Git Bash rewrites values starting with "/" into Windows paths before a
    # native python.exe ever sees them. Detect the wreckage and say so, rather
    # than reporting a dozen "bad prefix" lines that explain nothing.
    if "Program Files" in raw or ";" in raw:
        log(f"!! TUNNEL_FALLBACKS looks mangled by MSYS path conversion: {raw!r}")
        log("!! run via ./run.sh, which sets MSYS2_ENV_CONV_EXCL, or use cmd instead")
        return []

    parsed = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            log(f"!! ignoring TUNNEL_FALLBACKS entry {part!r} - expected /prefix=url")
            continue
        prefix, url = part.split("=", 1)
        prefix, url = prefix.strip().rstrip("/"), url.strip().rstrip("/")
        if not prefix.startswith("/") or not url:
            log(f"!! ignoring TUNNEL_FALLBACKS entry {part!r} - bad prefix or url")
            continue
        parsed.append((prefix, url))
    parsed.sort(key=lambda pair: len(pair[0]), reverse=True)
    return parsed


#: Where to send traffic when the tunnel cannot serve it. Empty = return 503.
FALLBACKS = _parse_fallbacks(os.getenv("TUNNEL_FALLBACKS", ""))
#: Whether a timeout or client-side error also triggers fallback, or only a
#: fully absent client. Timeouts cost FIRST_BYTE_TIMEOUT before failing over,
#: so leaving this on trades worst-case latency for availability.
FALLBACK_ON_ERROR = _flag("TUNNEL_FALLBACK_ON_ERROR", "true")
FALLBACK_READ_TIMEOUT = int(os.getenv("TUNNEL_FALLBACK_READ_TIMEOUT", "600"))
#: The prefix is kept when talking to the tunnel (the Kingdom strips it
#: itself). A fallback pointed straight at a backend needs it removed,
#: which is the common case.
STRIP_FALLBACK_PREFIX = _flag("TUNNEL_FALLBACK_STRIP_PREFIX", "true")



# ============================================================================
# Tracking
# ============================================================================

class Tracker:
    """Connection history, availability accounting and counters.

    Lives in memory, so a restart resets it — durable history is the stdout
    log, which pm2 captures to logs/tunnel-head.out.log. This exists so the
    current state is queryable without grepping logs, and so you can answer
    "has the GPU box been flapping?" from a single endpoint.
    """

    MAX_EVENTS = 200

    def __init__(self):
        self.started_at = time.time()
        self.events = collections.deque(maxlen=self.MAX_EVENTS)
        self.counters = collections.Counter()

        self.connections = 0
        self.disconnects = 0
        self.connected_since = None      # None while offline
        self.last_connected_at = None
        self.last_disconnected_at = None
        self.last_disconnect_reason = None
        self.client_name = None
        # Accumulated connected time from *completed* sessions only; the
        # in-progress one is added on read so the number never goes stale.
        self._uptime_closed = 0.0

    # -- recording ---------------------------------------------------------

    def record(self, kind, detail=""):
        now = time.time()
        self.events.append({
            "at": int(now),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "kind": kind,
            "detail": detail,
        })

    def count(self, name, amount=1):
        self.counters[name] += amount

    def on_connect(self, name):
        self.connections += 1
        self.connected_since = time.time()
        self.last_connected_at = self.connected_since
        self.client_name = name
        downtime = ""
        if self.last_disconnected_at:
            gap = int(self.connected_since - self.last_disconnected_at)
            downtime = f" after {gap}s offline"
        self.record("connect", f"client '{name}' connected{downtime}")

    def on_disconnect(self, name, reason):
        now = time.time()
        self.disconnects += 1
        if self.connected_since:
            self._uptime_closed += now - self.connected_since
        self.connected_since = None
        self.last_disconnected_at = now
        self.last_disconnect_reason = reason
        self.client_name = None
        self.record("disconnect", f"client '{name}' left: {reason}")

    # -- reporting ---------------------------------------------------------

    def uptime_seconds(self):
        live = (time.time() - self.connected_since) if self.connected_since else 0.0
        return self._uptime_closed + live

    def snapshot(self):
        total = max(time.time() - self.started_at, 1e-9)
        up = self.uptime_seconds()
        return {
            "head_started_at": int(self.started_at),
            "head_uptime_seconds": int(total),
            "tunnel_connected": self.connected_since is not None,
            "current_client": self.client_name,
            "connected_since": int(self.connected_since) if self.connected_since else None,
            "last_connected_at": int(self.last_connected_at) if self.last_connected_at else None,
            "last_disconnected_at": (
                int(self.last_disconnected_at) if self.last_disconnected_at else None
            ),
            "last_disconnect_reason": self.last_disconnect_reason,
            "connections": self.connections,
            "disconnects": self.disconnects,
            # Share of the head's lifetime with a client attached. Low values
            # mean the far end is flapping even if it happens to be up now.
            "availability_percent": round(100.0 * up / total, 2),
            "tunnel_uptime_seconds": int(up),
            "tunnel_downtime_seconds": int(max(total - up, 0)),
            "counters": dict(self.counters),
        }


tracker = Tracker()


# ============================================================================
# Registry
# ============================================================================

class TunnelClient:
    """One connected Kingdom and its in-flight requests."""

    def __init__(self, ws, name):
        self.ws = ws
        self.name = name or "unnamed"
        self.connected_at = time.time()
        self.served = 0
        self.closed = False
        self.pending = {}          # HTTP request id -> Queue
        self.ws_sessions = {}      # WebSocket session id -> Queue
        # ws.send() yields on the socket write; two greenlets sending at once
        # interleave frames and corrupt the stream.
        self._send_lock = Semaphore()

    def send_json(self, payload):
        data = json.dumps(payload, separators=(",", ":"))
        with self._send_lock:
            self.ws.send(data)

    def open_request(self):
        req_id = uuid.uuid4().hex
        queue = Queue()
        self.pending[req_id] = queue
        return req_id, queue

    def close_request(self, req_id):
        self.pending.pop(req_id, None)

    def deliver(self, req_id, item):
        queue = self.pending.get(req_id)
        if queue is None:
            return          # late frame for an abandoned request; normal
        queue.put(item)

    # -- WebSocket sessions ------------------------------------------------

    def open_ws_session(self):
        sid = uuid.uuid4().hex
        queue = Queue()
        self.ws_sessions[sid] = queue
        return sid, queue

    def close_ws_session(self, sid):
        self.ws_sessions.pop(sid, None)

    def deliver_ws(self, sid, item):
        queue = self.ws_sessions.get(sid)
        if queue is not None:
            queue.put(item)

    def fail_all(self, reason):
        self.closed = True
        for queue in list(self.pending.values()):
            queue.put(("err", reason))
        self.pending.clear()
        # Unblock every live WebSocket pump too, or browsers hang on a socket
        # whose far end is already gone.
        for queue in list(self.ws_sessions.values()):
            queue.put(("close", reason))
        self.ws_sessions.clear()

    def stats(self):
        return {
            "name": self.name,
            "connected_at": int(self.connected_at),
            "uptime_seconds": int(time.time() - self.connected_at),
            "in_flight": len(self.pending),
            "websockets": len(self.ws_sessions),
            "requests_served": self.served,
        }


class TunnelOffline(Exception):
    pass


class TunnelTimeout(Exception):
    pass


class TunnelError(Exception):
    pass


class Tunnel:
    def __init__(self):
        self.client = None
        self._lock = Semaphore()

    def register(self, ws, name):
        """Attach a client, displacing any previous one.

        Last writer wins on purpose: after a network blip the stale connection
        may not be reaped yet, and the fresh socket should take over rather
        than have requests dispatched into a dead one.
        """
        with self._lock:
            previous = self.client
            self.client = TunnelClient(ws, name)
            log(f"client '{self.client.name}' connected")
        tracker.on_connect(self.client.name)

        if previous is not None and not previous.closed:
            log(f"displacing previous client '{previous.name}'")
            tracker.record("displaced", f"client '{previous.name}' replaced by a newer connection")
            previous.fail_all("client replaced by a newer connection")
            try:
                previous.ws.close()
            except Exception:
                pass

        return self.client

    def unregister(self, client, reason):
        with self._lock:
            if self.client is client:
                self.client = None
        client.fail_all(reason)
        log(f"client '{client.name}' disconnected ({reason})")
        tracker.on_disconnect(client.name, reason)

    def is_online(self):
        return self.client is not None and not self.client.closed

    def stats(self):
        if not self.is_online():
            return {"online": False}
        return dict(self.client.stats(), online=True)

    def dispatch(self, method, path, query, headers, body):
        """Run one HTTP request through the tunnel.

        Returns (status, headers, body_iterator). The iterator is consumed
        after the Flask view returns, so everything it needs is captured here.
        """
        client = self.client
        if client is None or client.closed:
            raise TunnelOffline("no tunnel client is connected")

        forward_headers = {
            k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP
        }

        req_id, queue = client.open_request()
        try:
            client.send_json({
                "t": "req",
                "id": req_id,
                "method": method,
                "path": path,
                "query": query,
                "headers": forward_headers,
                "body": base64.b64encode(body or b"").decode("ascii"),
            })
        except Exception as exc:
            client.close_request(req_id)
            raise TunnelOffline(f"failed to write to client socket: {exc}")

        log(f"-> {method} {path} (id={req_id[:8]})")

        try:
            kind, *rest = queue.get(timeout=FIRST_BYTE_TIMEOUT)
        except Empty:
            client.close_request(req_id)
            raise TunnelTimeout(f"no response head within {FIRST_BYTE_TIMEOUT}s")

        if kind == "err":
            client.close_request(req_id)
            raise TunnelError(rest[0])
        if kind != "head":
            client.close_request(req_id)
            raise TunnelError(f"expected response head, got {kind!r}")

        status, response_headers = rest[0], rest[1]
        client.served += 1

        def stream():
            try:
                while True:
                    try:
                        item = queue.get(timeout=IDLE_TIMEOUT)
                    except Empty:
                        log(f"!! id={req_id[:8]} stalled — no chunk in {IDLE_TIMEOUT}s")
                        return
                    event = item[0]
                    if event == "chunk":
                        yield item[1]
                    elif event == "end":
                        log(f"<- {status} {path} (id={req_id[:8]})")
                        return
                    elif event == "err":
                        # Head is already on the wire; the status cannot change.
                        # Truncating the body is the only signal left.
                        log(f"!! id={req_id[:8]} failed mid-stream: {item[1]}")
                        return
            finally:
                client.close_request(req_id)

        return status, response_headers, stream()


tunnel = Tunnel()


# ============================================================================
# Routes
# ============================================================================

bp = Blueprint("tunnel", __name__)


def _is_public_port():
    return str(request.environ.get("SERVER_PORT", "")) == str(PUBLIC_PORT)


@bp.route("/tunnel-agent", websocket=True)
def tunnel_agent():
    """Accept the Kingdom's outbound WebSocket and pump frames until it drops.

    websocket=True is required, not cosmetic: Werkzeug inspects the Upgrade
    header and binds the URL map with url_scheme 'ws'. A rule not declared as a
    WebSocket rule raises WebsocketMismatch, and the view never runs — the
    client just sees a bare 400 after the 101.
    """
    ws = request.environ.get("wsgi.websocket")

    if not _is_public_port():
        # geventwebsocket completes the 101 handshake inside run_application,
        # before the WSGI app is ever called, so by the time we get here the
        # socket is already upgraded and a returned status would be discarded.
        # Close it explicitly rather than leaving a half-open socket that
        # looks accepted to the caller. No client is registered either way.
        log(f"!! /tunnel-agent attempted on the private port from {request.remote_addr}")
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
            return ""
        return {"error": "agent endpoint is served on the public port only"}, 404

    if ws is None:
        return {
            "error": "This endpoint requires a WebSocket upgrade",
            "hint": "Connect with BrightysKingdom's tunnel_client, not a browser",
        }, 400

    if not TUNNEL_TOKEN:
        log("!! TUNNEL_TOKEN is not set — refusing all clients")
        _safe_send(ws, {"t": "denied", "error": "tunnel token not configured on server"})
        ws.close()
        return ""

    try:
        raw = ws.receive()
    except Exception as exc:
        log(f"!! handshake read failed: {exc}")
        return ""
    if raw is None:
        return ""

    try:
        hello = json.loads(raw)
    except (TypeError, ValueError):
        _safe_send(ws, {"t": "denied", "error": "first frame must be JSON hello"})
        ws.close()
        return ""

    if hello.get("t") != "hello" or not hmac.compare_digest(
        str(hello.get("token") or ""), str(TUNNEL_TOKEN)
    ):
        log(f"!! rejected client from {request.remote_addr} — bad token")
        _safe_send(ws, {"t": "denied", "error": "invalid tunnel token"})
        ws.close()
        return ""

    client = tunnel.register(ws, hello.get("name"))
    _safe_send(ws, {"t": "ready"})

    # Single reader; fans each frame out to the greenlet awaiting that id.
    reason = "socket closed"
    try:
        while True:
            message = ws.receive()
            if message is None:
                break
            try:
                frame = json.loads(message)
            except (TypeError, ValueError):
                continue

            kind = frame.get("t")
            if kind == "head":
                client.deliver(frame.get("id"), (
                    "head", int(frame.get("status", 502)), frame.get("headers") or {}))
            elif kind == "chunk":
                client.deliver(frame.get("id"), (
                    "chunk", base64.b64decode(frame.get("data") or "")))
            elif kind == "end":
                client.deliver(frame.get("id"), ("end", None))
            elif kind == "err":
                client.deliver(frame.get("id"), ("err", frame.get("error", "client error")))
            elif kind == "ws_opened":
                client.deliver_ws(frame.get("id"), ("opened",))
            elif kind == "ws_error":
                client.deliver_ws(frame.get("id"), ("error", frame.get("error", "ws error")))
            elif kind == "ws_msg":
                client.deliver_ws(frame.get("id"), (
                    "msg",
                    base64.b64decode(frame.get("data") or ""),
                    bool(frame.get("binary")),
                ))
            elif kind == "ws_close":
                client.deliver_ws(frame.get("id"), ("close", "closed by backend"))
    except Exception as exc:
        reason = f"read loop error: {exc}"
    finally:
        tunnel.unregister(client, reason)
        try:
            ws.close()
        except Exception:
            pass

    return ""


@bp.route("/_tunnel/status")
def tunnel_status():
    """Current state. `healthy` answers "can this head serve requests?",
    which is not the same as "is the tunnel up" once a fallback exists."""
    if _is_public_port():
        return {"error": "not found"}, 404

    stats = tunnel.stats()
    online = stats.get("online", False)
    snap = tracker.snapshot()
    fallbacks = [{"prefix": prefix, "upstream": base} for prefix, base in FALLBACKS]

    return {
        "service": "tunnel-head",
        "client": stats,
        "tunnel_online": online,
        # Serving via fallback still counts as healthy - the caller is being
        # answered. Alert on tunnel_online if you want to know about the
        # GPU box specifically.
        "healthy": online or bool(fallbacks),
        "degraded": (not online) and bool(fallbacks),
        "fallbacks": fallbacks,
        "fallback_on_error": FALLBACK_ON_ERROR,
        "availability_percent": snap["availability_percent"],
        "connections": snap["connections"],
        "disconnects": snap["disconnects"],
        "last_disconnect_reason": snap["last_disconnect_reason"],
    }, (200 if (online or fallbacks) else 503)


@bp.route("/_tunnel/metrics")
def tunnel_metrics():
    """Counters and availability accounting, for scraping or a dashboard."""
    if _is_public_port():
        return {"error": "not found"}, 404
    snap = tracker.snapshot()
    snap["client"] = tunnel.stats()
    return snap, 200


@bp.route("/_tunnel/events")
def tunnel_events():
    """Recent connect / disconnect / fallback events, newest last.

    In-memory ring buffer of the last 200, so it survives a flapping client
    but not a head restart. The durable record is the stdout log.
    """
    if _is_public_port():
        return {"error": "not found"}, 404
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), Tracker.MAX_EVENTS))
    except ValueError:
        limit = 50
    events = list(tracker.events)[-limit:]
    return {
        "count": len(events),
        "buffered": len(tracker.events),
        "capacity": Tracker.MAX_EVENTS,
        "events": events,
    }, 200


@bp.route("/", defaults={"path": ""},
          methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@bp.route("/<path:path>",
          methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def forward(path):
    """Forward everything else down the tunnel."""
    if _is_public_port():
        # The public listener exposes the authenticated agent endpoint only.
        # Serving the forwarding surface here would publish every local
        # service behind the Kingdom to the internet, unauthenticated.
        return {"error": "not found"}, 404

    target_path = "/" + path
    body = request.get_data()          # must read inside the request context
    headers = dict(request.headers)
    query = request.query_string.decode()

    try:
        status, response_headers, stream = tunnel.dispatch(
            request.method, target_path, query, headers, body)
    except (TunnelOffline, TunnelTimeout, TunnelError) as exc:
        # All three are raised BEFORE any response head reaches the caller, so
        # failing over here is safe: nothing is committed to the wire yet.
        # Once the head is sent, no fallback is possible.
        offline = isinstance(exc, TunnelOffline)
        target = _fallback_for(target_path)

        if target and (offline or FALLBACK_ON_ERROR):
            tracker.count("requests_via_fallback")
            tracker.record(
                "fallback",
                f"{request.method} {target_path} -> {target} ({type(exc).__name__})",
            )
            log(f"fallback: {request.method} {target_path} -> {target} ({exc})")
            return _serve_from_fallback(target, target_path, query, headers, body)

        tracker.count("requests_failed")
        if FALLBACKS and target is None:
            hint = f"no TUNNEL_FALLBACKS entry matches {target_path}"
        elif not FALLBACKS:
            hint = "set TUNNEL_FALLBACKS to serve this when the tunnel is down"
        else:
            hint = "TUNNEL_FALLBACK_ON_ERROR is off, so only an absent client fails over"

        if offline:
            return {"error": "tunnel client offline",
                    "detail": str(exc), "hint": hint}, 503
        if isinstance(exc, TunnelTimeout):
            return {"error": "tunnel client timed out",
                    "detail": str(exc), "hint": hint}, 504
        return {"error": "tunnel client reported a failure",
                "detail": str(exc), "hint": hint}, 502

    tracker.count("requests_via_tunnel")

    clean = [(k, v) for k, v in response_headers.items()
             if k.lower() not in STRIP_RESPONSE_HEADERS]
    # Lets a caller tell which path served it without reading the head's logs.
    clean.append(("X-Tunnel-Source", "tunnel"))

    # direct_passthrough stops Flask buffering the generator, without which SSE
    # arrives in one lump when generation finishes instead of token by token.
    return Response(stream, status=status, headers=clean, direct_passthrough=True)


def _fallback_for(path):
    """Longest matching prefix from TUNNEL_FALLBACKS, or None."""
    for prefix, base in FALLBACKS:
        if path == prefix or path.startswith(prefix + "/"):
            return base
    return None


#: Pooled on purpose: the fallback runs exactly when things are already going
#: wrong, and a fresh handshake per request is the last thing that needs.
_fallback_session = requests.Session()


def _serve_from_fallback(base, path, query, headers, body):
    """Serve one request from the fallback upstream, streaming the response.

    `path` still carries the route prefix, because the tunnel path relies on
    the Kingdom's own load balancer to strip it. A fallback aimed straight at
    a backend needs it removed - /ollama/api/tags must arrive as /api/tags -
    which is what STRIP_FALLBACK_PREFIX does.
    """
    upstream_path = path
    if STRIP_FALLBACK_PREFIX:
        for prefix, candidate in FALLBACKS:
            if candidate == base and (path == prefix or path.startswith(prefix + "/")):
                upstream_path = path[len(prefix):] or "/"
                break

    url = base + upstream_path
    if query:
        url = f"{url}?{query}"

    forward_headers = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}

    try:
        resp = _fallback_session.request(
            request.method,
            url,
            headers=forward_headers,
            data=body or None,
            stream=True,
            timeout=(10, FALLBACK_READ_TIMEOUT),
            allow_redirects=False,
        )
    except Exception as exc:
        tracker.count("fallback_failed")
        tracker.record("fallback_failed", f"{url}: {exc}")
        log(f"!! fallback also failed: {url} - {exc}")
        return {"error": "tunnel offline and fallback unreachable",
                "detail": str(exc), "fallback": base}, 502

    clean = [(k, v) for k, v in resp.headers.items()
             if k.lower() not in STRIP_RESPONSE_HEADERS]
    clean.append(("X-Tunnel-Source", "fallback"))

    def stream_fallback():
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(stream_fallback(), status=resp.status_code,
                    headers=clean, direct_passthrough=True)


@bp.route("/", defaults={"path": ""}, websocket=True)
@bp.route("/<path:path>", websocket=True)
def forward_websocket(path):
    """Relay a WebSocket connection down the tunnel.

    Registered as a separate `websocket=True` rule alongside the HTTP
    catch-all: Werkzeug matches WebSocket requests only against WebSocket
    rules, so the two coexist on the same path pattern without colliding.
    `/tunnel-agent` still wins over this because a static rule outranks a
    converter rule.

    Both directions are pumped concurrently — a greenlet drains the tunnel
    into the browser socket while this greenlet reads the browser socket into
    the tunnel. Socket.IO needs genuinely bidirectional traffic; a half-duplex
    relay deadlocks the moment the server pushes without being asked.
    """
    ws = request.environ.get("wsgi.websocket")
    if ws is None:
        return {"error": "WebSocket upgrade required"}, 400

    if _is_public_port():
        _close_quietly(ws)
        return ""

    client = tunnel.client
    if client is None or client.closed:
        log(f"!! ws {path} refused — no tunnel client connected")
        _close_quietly(ws)
        return ""

    target_path = "/" + path
    query = request.query_string.decode()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in WS_SKIP_HEADERS
    }
    protocols = [
        p.strip()
        for p in (request.headers.get("Sec-WebSocket-Protocol") or "").split(",")
        if p.strip()
    ]

    sid, queue = client.open_ws_session()
    try:
        client.send_json({
            "t": "ws_open",
            "id": sid,
            "path": target_path,
            "query": query,
            "headers": headers,
            "protocols": protocols,
        })
    except Exception as exc:
        log(f"!! ws {target_path} — failed to signal client: {exc}")
        client.close_ws_session(sid)
        _close_quietly(ws)
        return ""

    # Wait for the far end to actually reach the backend before pumping, so a
    # backend that is down surfaces as a refused socket rather than one that
    # accepts and silently swallows everything.
    try:
        item = queue.get(timeout=WS_OPEN_TIMEOUT)
    except Empty:
        log(f"!! ws {target_path} — backend did not open within {WS_OPEN_TIMEOUT}s")
        client.close_ws_session(sid)
        _close_quietly(ws)
        return ""

    if item[0] != "opened":
        log(f"!! ws {target_path} — backend refused: {item[1] if len(item) > 1 else item[0]}")
        tracker.count("ws_refused")
        client.close_ws_session(sid)
        _close_quietly(ws)
        return ""

    log(f"ws open {target_path} (id={sid[:8]})")
    tracker.count("ws_opened")

    def pump_to_browser():
        """tunnel -> browser"""
        while True:
            event = queue.get()
            kind = event[0]
            if kind == "msg":
                data, binary = event[1], event[2]
                try:
                    ws.send(data if binary else data.decode("utf-8", "replace"))
                except Exception:
                    return
            else:                       # close / error
                _close_quietly(ws)      # unblocks the ws.receive() below
                return

    writer = gevent.spawn(pump_to_browser)

    try:
        while True:
            message = ws.receive()
            if message is None:
                break
            binary = isinstance(message, (bytes, bytearray))
            payload = bytes(message) if binary else message.encode("utf-8")
            client.send_json({
                "t": "ws_msg",
                "id": sid,
                "data": base64.b64encode(payload).decode("ascii"),
                "binary": binary,
            })
    except Exception as exc:
        log(f"ws {target_path} read ended (id={sid[:8]}): {exc}")
    finally:
        try:
            client.send_json({"t": "ws_close", "id": sid})
        except Exception:
            pass
        writer.kill(block=False)
        client.close_ws_session(sid)
        _close_quietly(ws)
        log(f"ws closed {target_path} (id={sid[:8]})")

    return ""


def _close_quietly(ws):
    try:
        ws.close()
    except Exception:
        pass


def _safe_send(ws, payload):
    try:
        ws.send(json.dumps(payload))
    except Exception:
        pass


# ============================================================================
# Entrypoint
# ============================================================================

def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.getenv("TUNNEL_MAX_CONTENT_LENGTH", str(1024 * 1024 * 1024)))
    app.register_blueprint(bp)
    return app


def main():
    if not TUNNEL_TOKEN:
        log("!! TUNNEL_TOKEN is not set — every client will be refused.")
        log("!! generate one:  python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        sys.exit(1)

    app = create_app()

    public = WSGIServer((PUBLIC_BIND, PUBLIC_PORT), application=app,
                        handler_class=WebSocketHandler)
    local = WSGIServer((LOCAL_BIND, LOCAL_PORT), application=app,
                       handler_class=WebSocketHandler)

    log("tunnel head starting")
    log(f"  public  {PUBLIC_BIND}:{PUBLIC_PORT}  -> /tunnel-agent (token-authenticated)")
    log(f"  local   {LOCAL_BIND}:{LOCAL_PORT}  -> forwarding surface (KEEP OFF THE INTERNET)")
    log(f"  status  http://127.0.0.1:{LOCAL_PORT}/_tunnel/status")
    if FALLBACKS:
        for prefix, base in FALLBACKS:
            log(f"  fallback {prefix} -> {base}")
        log(f"  fallback on error: {FALLBACK_ON_ERROR}, strip prefix: {STRIP_FALLBACK_PREFIX}")
    else:
        log("  fallback none - requests return 503 while the tunnel is down")

    # Binding is the first thing that can fail for an ordinary operational
    # reason — restarting without stopping the previous head, or another
    # service already on the port. A gevent traceback buries that, and under
    # pm2 it turns into a restart loop with no obvious cause.
    try:
        public.start()
    except OSError as exc:
        _die_port_in_use(PUBLIC_BIND, PUBLIC_PORT, "public", exc)

    try:
        local.start()
    except OSError as exc:
        public.stop()
        _die_port_in_use(LOCAL_BIND, LOCAL_PORT, "local", exc)

    try:
        local.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


def _die_port_in_use(bind, port, label, exc):
    """Explain a bind failure and exit, rather than dumping a stack trace."""
    log(f"!! cannot bind the {label} listener on {bind}:{port} — {exc}")
    log("!! most likely another tunnel head is still running.")
    log("!!")
    log("!!   pm2:     pm2 restart tunnel-head   (not `start` a second time)")
    log(f"!!   linux:   lsof -i :{port}          then kill that PID")
    log(f"!!   windows: netstat -ano | findstr :{port}   then taskkill /PID <pid> /F")
    log("!!")
    log(f"!! or move it: set TUNNEL_{label.upper()}_PORT to a free port in .env")
    sys.exit(1)


if __name__ == "__main__":
    main()
