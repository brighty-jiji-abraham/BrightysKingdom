"""
Tunnel client — the outbound half. Makes BrightysKingdom reachable from a
public server without exposing this machine.

The Kingdom already multiplexes every local service behind one port. This
module gives that port a public face: it dials OUT to tunnel_server on the
public host, holds the WebSocket open, and replays whatever arrives against
its own listener on 127.0.0.1. Requests then flow through the Kingdom's normal
routing — BACKEND_ROUTES, load balancing, middleware, all of it — so one
tunnel covers /app1, /app2, /ollama and anything added later.

    tunnel_server (public)  <=== outbound WebSocket === this module
                                                            │
                                                            ▼
                                                  Kingdom :2000 (loopback)
                                                    ├─ /app1   → :3000
                                                    ├─ /app2   → :5000
                                                    └─ /ollama → :11434

Forwarding over loopback rather than calling the WSGI app in-process is
deliberate: it reuses the running server exactly as an external caller would,
including the geventwebsocket handler and every middleware, and keeps this
module runnable as a separate process for debugging.

USAGE
-----
Normally started for you by run_gevent.py when TUNNEL_SERVER_URL is set.
Standalone, for debugging:

    python -m proxy_server.core.tunnel_client
"""

import base64
import collections
import json
import os
import time

import gevent
import requests
import websocket
from gevent.queue import Queue

from dotenv import load_dotenv

from proxy_server.utils.logger import get_logger

logger = get_logger(__name__)

# Config below is read at import time. When run_gevent.py is the entrypoint,
# settings.py has already loaded .env — but `python -m proxy_server.core.
# tunnel_client` imports this module first, so without this the standalone
# path would silently ignore .env and never dial out.
load_dotenv()

# --- configuration ----------------------------------------------------------

# These are the .env values only. They seed the database on first run and are
# the fallback when it is unreachable - resolve_config() below is what the
# client actually uses, and it re-reads on every reconnect so a change made in
# the admin panel takes effect without restarting the proxy.
ENV_SERVER_URL = os.getenv("TUNNEL_SERVER_URL", "")
ENV_TOKEN = os.getenv("TUNNEL_TOKEN", "")
ENV_CLIENT_NAME = os.getenv("TUNNEL_CLIENT_NAME", "")
ENV_LOCAL_TARGET = os.getenv("TUNNEL_LOCAL_TARGET", "http://127.0.0.1:2000")

DEFAULT_LOCAL_TARGET = "http://127.0.0.1:2000"


def resolve_config():
    """Current tunnel settings: database first, .env as fallback.

    Reading through the config store rather than module constants is what
    lets the endpoint be changed from the admin panel. Falls back to .env
    whenever Mongo is unreachable, so a config-store outage cannot silently
    unpublish this machine.
    """
    try:
        from proxy_server.services import config_store

        settings, source = config_store.load_tunnel()
    except Exception as exc:                      # pragma: no cover
        logger.warning(f"tunnel: could not read settings ({exc}) - using .env")
        settings, source = {
            "server_url": ENV_SERVER_URL,
            "token": ENV_TOKEN,
            "client_name": ENV_CLIENT_NAME,
            "local_target": ENV_LOCAL_TARGET,
        }, "env"

    target = (settings.get("local_target") or DEFAULT_LOCAL_TARGET).rstrip("/")
    return {
        "server_url": settings.get("server_url") or "",
        "token": settings.get("token") or "",
        "client_name": settings.get("client_name") or "",
        "local_target": target,
        # ws:// form, for replaying tunnelled WebSockets.
        "local_ws_target": target.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        ),
        "source": source,
    }

CONNECT_TIMEOUT = float(os.getenv("TUNNEL_CONNECT_TIMEOUT", "10"))
# Long enough for an Ollama /api/pull or a slow generation.
READ_TIMEOUT = float(os.getenv("TUNNEL_READ_TIMEOUT", "600"))
CHUNK_SIZE = int(os.getenv("TUNNEL_CHUNK_SIZE", "8192"))

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0

SKIP_REQUEST_HEADERS = {
    "host", "connection", "keep-alive", "transfer-encoding", "upgrade",
    "content-length", "proxy-authorization", "proxy-authenticate", "te", "trailer",
}

# websocket-client performs its own handshake, so the originating one's
# negotiation headers must not be replayed.
SKIP_WS_HEADERS = SKIP_REQUEST_HEADERS | {
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-accept", "sec-websocket-protocol",
}

WS_CONNECT_TIMEOUT = float(os.getenv("TUNNEL_WS_CONNECT_TIMEOUT", "20"))

#: How many recent requests to keep for the admin UI. In memory only, so it
#: resets with the process - the durable record is the proxy log.
REQUEST_LOG_SIZE = int(os.getenv("TUNNEL_REQUEST_LOG_SIZE", "200"))

#: Minutes of traffic history to keep for the chart.
TRAFFIC_WINDOW_MINUTES = int(os.getenv("TUNNEL_TRAFFIC_WINDOW_MINUTES", "60"))


#: The running client, so the admin API can report on it. There is at most
#: one per process.
_active_client = None


class TunnelClient:
    def __init__(self):
        self.ws = None
        # websocket-client is not safe for concurrent sends and we run one
        # greenlet per in-flight request. Serialise every write.
        self._send_lock = gevent.lock.Semaphore()
        self.session = requests.Session()
        self.active = 0
        # session id -> {"ws": <websocket>, "out": Queue, "closed": bool}
        self.ws_sessions = {}

        # --- state for /admin/tunnel --------------------------------------
        # The admin UI cannot reach the head's own /_tunnel/* endpoints: they
        # are bound to a private port on the public server, firewalled from
        # the browser. This side of the tunnel knows just as much about its
        # own health, so it reports from here instead.
        #
        # cfg is a snapshot of the settings this connection is using. It is
        # refreshed at the top of every reconnect, so an edit made in the
        # admin panel is picked up by closing the socket rather than by
        # restarting the process.
        self.cfg = resolve_config()
        self.connected = False
        self.connected_at = None
        self.reconnects = 0
        self.requests_served = 0
        self.last_error = None
        self.started_at = time.time()

        # What actually crossed the tunnel, for the admin UI. Bounded on both
        # counts so a busy tunnel cannot grow memory without limit.
        self.recent = collections.deque(maxlen=REQUEST_LOG_SIZE)
        self.traffic = collections.OrderedDict()   # minute -> counters

    def record(self, method, path, status, started, error=None, kind="http"):
        """Note one completed request, for the log and the traffic chart."""
        now = time.time()
        elapsed_ms = int((now - started) * 1000)

        self.recent.append({
            "at": int(now),
            "kind": kind,
            "method": method,
            "path": path,
            "status": status,
            "ms": elapsed_ms,
            "error": error,
        })

        minute = int(now // 60) * 60
        bucket = self.traffic.get(minute)
        if bucket is None:
            bucket = {"count": 0, "errors": 0, "ms": 0}
            self.traffic[minute] = bucket
        bucket["count"] += 1
        bucket["ms"] += elapsed_ms
        # A transport failure has no status code, so treat it as an error too -
        # otherwise a tunnel that cannot reach the Kingdom looks perfectly
        # healthy on the chart.
        if error or (isinstance(status, int) and status >= 400):
            bucket["errors"] += 1

        cutoff = minute - TRAFFIC_WINDOW_MINUTES * 60
        for old_minute in [m for m in self.traffic if m < cutoff]:
            del self.traffic[old_minute]

    def traffic_series(self):
        """Per-minute counts, zero-filled across the window.

        Gaps are filled deliberately: a quiet minute must read as a zero bar,
        not as a missing one, or the chart implies traffic it never saw.
        """
        now_minute = int(time.time() // 60) * 60
        series = []
        for i in range(TRAFFIC_WINDOW_MINUTES - 1, -1, -1):
            minute = now_minute - i * 60
            b = self.traffic.get(minute)
            series.append({
                "minute": minute,
                "count": b["count"] if b else 0,
                "errors": b["errors"] if b else 0,
                "avg_ms": int(b["ms"] / b["count"]) if b and b["count"] else 0,
            })
        return series

    def status(self):
        """Snapshot for the admin API."""
        return {
            "configured": bool(self.cfg["server_url"]),
            "connected": self.connected,
            "server_url": self.cfg["server_url"],
            "local_target": self.cfg["local_target"],
            "client_name": self.client_name(),
            "config_source": self.cfg["source"],
            "connected_at": int(self.connected_at) if self.connected_at else None,
            "connected_seconds": (
                int(time.time() - self.connected_at) if self.connected_at else 0
            ),
            "reconnects": self.reconnects,
            "requests_served": self.requests_served,
            "requests_in_flight": self.active,
            "websocket_sessions": len(self.ws_sessions),
            "last_error": self.last_error,
        }

    # -- socket plumbing ---------------------------------------------------

    def send(self, payload):
        data = json.dumps(payload, separators=(",", ":"))
        with self._send_lock:
            self.ws.send(data)

    def send_error(self, req_id, message):
        try:
            self.send({"t": "err", "id": req_id, "error": str(message)})
        except Exception:
            pass

    # -- request handling --------------------------------------------------

    def handle_request(self, frame):
        """Replay one tunnelled request against the local Kingdom."""
        req_id = frame.get("id")
        method = frame.get("method", "GET")
        path = frame.get("path", "/")
        query = frame.get("query") or ""
        headers = {
            k: v for k, v in (frame.get("headers") or {}).items()
            if k.lower() not in SKIP_REQUEST_HEADERS
        }
        body = base64.b64decode(frame.get("body") or "")

        url = f"{self.cfg['local_target']}{path}"
        if query:
            url = f"{url}?{query}"

        self.active += 1
        self.requests_served += 1
        started = time.time()
        logger.info(f"tunnel: -> {method} {path} (id={req_id[:8]}, active={self.active})")

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                data=body if body else None,
                stream=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=False,
            )
        except requests.exceptions.ConnectionError as exc:
            logger.error(f"tunnel: cannot reach the Kingdom at {self.cfg['local_target']} — {exc}")
            self.send_error(req_id, f"cannot reach local Kingdom at {self.cfg['local_target']}: {exc}")
            self.record(method, path, None, started, error="cannot reach the proxy")
            self.active -= 1
            return
        except Exception as exc:
            logger.error(f"tunnel: local request failed: {exc}")
            self.send_error(req_id, f"local request failed: {exc}")
            self.record(method, path, None, started, error=str(exc)[:160])
            self.active -= 1
            return

        try:
            self.send({
                "t": "head",
                "id": req_id,
                "status": response.status_code,
                "headers": dict(response.headers),
            })

            # iter_content gunzips transparently, which is why the head strips
            # Content-Encoding before replying to the original caller.
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    self.send({
                        "t": "chunk",
                        "id": req_id,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    })

            self.send({"t": "end", "id": req_id})
            self.record(method, path, response.status_code, started)
            logger.info(
                f"tunnel: <- {response.status_code} {path} "
                f"in {time.time() - started:.2f}s (id={req_id[:8]})"
            )
        except websocket.WebSocketException as exc:
            # Tunnel died mid-stream; the head has already failed this request.
            logger.warning(f"tunnel: lost while streaming id={req_id[:8]}: {exc}")
            self.record(method, path, None, started, error="tunnel lost mid-stream")
        except Exception as exc:
            logger.error(f"tunnel: stream error id={req_id[:8]}: {exc}")
            self.send_error(req_id, f"stream error: {exc}")
            self.record(method, path, None, started, error=str(exc)[:160])
        finally:
            response.close()
            self.active -= 1

    # -- tunnelled WebSocket sessions --------------------------------------

    def open_ws_session(self, frame):
        """Open a backend WebSocket for a session the head just announced.

        Each session gets exactly two greenlets and one queue:

            pump_ws_up    — the ONLY reader of the backend socket
            pump_ws_down  — the ONLY writer of the backend socket
            session["out"] — the head's frames, handed over without blocking

        That split is load-bearing. websocket-client sockets are not safe for
        concurrent access, and its close() performs a close-handshake *read*.
        Calling close() from the tunnel's read loop while pump_ws_up was
        already blocked in recv_data() on the same socket wedged the entire
        tunnel: no further frames of any kind were processed. Teardown now
        goes through abort(), which shuts the socket down without reading.
        """
        sid = frame.get("id")
        path = frame.get("path", "/")
        query = frame.get("query") or ""
        headers = {
            k: v for k, v in (frame.get("headers") or {}).items()
            if k.lower() not in SKIP_WS_HEADERS
        }
        protocols = frame.get("protocols") or None

        url = f"{self.cfg['local_ws_target']}{path}"
        if query:
            url = f"{url}?{query}"

        try:
            local = websocket.create_connection(
                url,
                header=[f"{k}: {v}" for k, v in headers.items()],
                subprotocols=protocols,
                timeout=WS_CONNECT_TIMEOUT,
            )
            # create_connection's timeout also becomes the *recv* timeout, which
            # would tear down any socket idle longer than it. Socket.IO holds
            # connections open for minutes between events, so clear it — the
            # socket's lifetime is controlled by abort() below, not by a clock.
            local.settimeout(None)
        except Exception as exc:
            logger.error(f"tunnel: ws open failed for {path}: {exc}")
            self.record("WS", path, None, time.time(), error=str(exc)[:160],
                        kind="websocket")
            try:
                self.send({"t": "ws_error", "id": sid, "error": str(exc)})
            except Exception:
                pass
            return

        session = {"ws": local, "out": Queue(), "closed": False}
        self.ws_sessions[sid] = session

        # Register before announcing: the head starts pumping the instant it
        # sees ws_opened, so the session must already be reachable.
        self.send({"t": "ws_opened", "id": sid})
        self.record("WS", path, 101, time.time(), kind="websocket")
        logger.info(f"tunnel: ws open {path} (id={sid[:8]})")

        gevent.spawn(self.pump_ws_up, sid, session, path)
        gevent.spawn(self.pump_ws_down, sid, session, path)

    def pump_ws_up(self, sid, session, path):
        """backend -> head. Sole reader of the backend socket."""
        local = session["ws"]
        try:
            while not session["closed"]:
                opcode, data = local.recv_data()
                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    break
                if opcode in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
                    self.send({
                        "t": "ws_msg",
                        "id": sid,
                        "data": base64.b64encode(data).decode("ascii"),
                        "binary": opcode == websocket.ABNF.OPCODE_BINARY,
                    })
                # ping/pong are handled inside websocket-client
        except Exception as exc:
            if not session["closed"]:
                logger.info(f"tunnel: ws {path} backend closed (id={sid[:8]}): {exc}")
        finally:
            self.close_ws_session(sid, notify=True)

    def pump_ws_down(self, sid, session, path):
        """head -> backend. Sole writer of the backend socket."""
        local = session["ws"]
        try:
            while True:
                item = session["out"].get()
                if item[0] != "msg":
                    return
                data, binary = item[1], item[2]
                if binary:
                    local.send_binary(data)
                else:
                    local.send(data.decode("utf-8", "replace"))
        except Exception as exc:
            if not session["closed"]:
                logger.info(f"tunnel: ws {path} write ended (id={sid[:8]}): {exc}")
        finally:
            session["closed"] = True
            # abort() shuts the socket down without a close handshake, which is
            # what unblocks pump_ws_up's recv_data(). close() would try to read.
            try:
                local.abort()
            except Exception:
                pass

    def deliver_ws(self, frame):
        """head -> backend, enqueued.

        Called straight from the tunnel read loop, so it must never block:
        a queue put keeps ordering while leaving the loop free for every other
        session sharing this tunnel.
        """
        session = self.ws_sessions.get(frame.get("id"))
        if session is None or session["closed"]:
            return
        session["out"].put((
            "msg",
            base64.b64decode(frame.get("data") or ""),
            bool(frame.get("binary")),
        ))

    def close_ws_session(self, sid, notify=False):
        session = self.ws_sessions.pop(sid, None)
        if session is None:
            return
        already_closed = session["closed"]
        session["closed"] = True
        # Wake the writer, which performs the actual socket teardown.
        session["out"].put(("close",))
        if notify and not already_closed:
            try:
                self.send({"t": "ws_close", "id": sid})
            except Exception:
                pass

    def close_all_ws(self):
        """Called when the tunnel drops — no session can outlive its transport."""
        for sid in list(self.ws_sessions):
            self.close_ws_session(sid, notify=False)

    # -- websocket callbacks -----------------------------------------------

    def client_name(self):
        return self.cfg["client_name"] or os.getenv("COMPUTERNAME") or "kingdom"

    def on_open(self, ws):
        name = self.client_name()
        logger.info(
            f"tunnel: connected to {self.cfg['server_url']} "
            f"(settings from {self.cfg['source']}), authenticating as '{name}'"
        )
        self.send({"t": "hello", "token": self.cfg["token"], "name": name})

    def on_message(self, ws, message):
        try:
            frame = json.loads(message)
        except (TypeError, ValueError):
            return

        kind = frame.get("t")
        if kind == "req":
            # One greenlet per request so a long generation cannot block the
            # read loop and stall every other in-flight call.
            gevent.spawn(self.handle_request, frame)
        elif kind == "ws_open":
            # Spawned, not inline: create_connection blocks, and blocking here
            # would stall every other session sharing this read loop.
            gevent.spawn(self.open_ws_session, frame)
        elif kind == "ws_msg":
            self.deliver_ws(frame)
        elif kind == "ws_close":
            self.close_ws_session(frame.get("id"), notify=False)
        elif kind == "ready":
            # Only now is the tunnel usable: the socket being open is not the
            # same as the head having accepted the token.
            self.connected = True
            self.connected_at = time.time()
            self.last_error = None
            logger.info("tunnel: authenticated — local services are now reachable")
        elif kind == "denied":
            self.last_error = f"rejected by head: {frame.get('error')}"
            logger.error(f"tunnel: server rejected this client: {frame.get('error')}")
            logger.error("tunnel: check TUNNEL_TOKEN matches the value on the server")
            ws.close()
        elif kind == "ping":
            self.send({"t": "pong"})

    def on_error(self, ws, error):
        self.last_error = str(error)
        logger.warning(f"tunnel: socket error: {error}")

    def on_close(self, ws, status_code, message):
        self.connected = False
        self.connected_at = None
        logger.info(f"tunnel: connection closed (code={status_code})")
        # Backend sockets cannot outlive the transport that carried them.
        self.close_all_ws()

    # -- main loop ---------------------------------------------------------

    def run_forever(self):
        backoff = RECONNECT_MIN
        while True:
            # Re-read before each attempt: this is what makes an endpoint or
            # token changed in the admin panel take effect on the next
            # reconnect instead of needing a process restart.
            self.cfg = resolve_config()

            if not self.cfg["server_url"]:
                logger.info("tunnel: no server URL configured - idling")
                gevent.sleep(15)
                continue

            self.ws = websocket.WebSocketApp(
                self.cfg["server_url"],
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )
            started = time.time()
            try:
                # ping_interval keeps NAT tables and intermediate proxies from
                # dropping an idle tunnel.
                self.ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as exc:
                logger.error(f"tunnel: run_forever crashed: {exc}")

            # A connection that lasted a while was healthy — reset the backoff
            # so a brief blip doesn't inherit a 30s delay from an older outage.
            if time.time() - started > 60:
                backoff = RECONNECT_MIN

            self.reconnects += 1
            logger.info(f"tunnel: reconnecting in {backoff:.0f}s")
            gevent.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


def start_in_background():
    """Spawn the tunnel client if it is configured. Called from run_gevent.py.

    Returns the greenlet, or None when the tunnel is not configured — an
    unconfigured tunnel is a normal local-only run, not an error.
    """
    global _active_client

    # Seed the database from .env the first time, so an existing deployment
    # keeps working and there is no migration step to forget.
    try:
        from proxy_server.services import config_store

        if config_store.seed_tunnel_from_env():
            logger.info("tunnel: seeded settings from .env into the config store")
    except Exception as exc:
        logger.warning(f"tunnel: could not seed settings ({exc})")

    cfg = resolve_config()

    if not cfg["server_url"]:
        logger.info("tunnel: no server URL configured — running local-only")
        return None

    if not cfg["token"]:
        logger.error("tunnel: a server URL is set but the token is empty — not starting")
        return None

    _active_client = TunnelClient()
    logger.info(
        f"tunnel: dialling {cfg['server_url']} (settings from {cfg['source']}), "
        f"forwarding to {cfg['local_target']}"
    )
    return gevent.spawn(_active_client.run_forever)


def get_activity(limit=100):
    """Recent tunnel requests and the traffic series, for GET /admin/tunnel/requests."""
    client = _active_client
    if client is None:
        return {"requests": [], "traffic": [], "window_minutes": TRAFFIC_WINDOW_MINUTES}

    limit = max(1, min(int(limit), REQUEST_LOG_SIZE))
    return {
        # Newest first: the interesting request is almost always the last one.
        "requests": list(client.recent)[-limit:][::-1],
        "traffic": client.traffic_series(),
        "window_minutes": TRAFFIC_WINDOW_MINUTES,
        "capacity": REQUEST_LOG_SIZE,
    }


def reconnect():
    """Drop the current socket so the loop re-reads settings and re-dials.

    Called after the admin panel changes tunnel settings. Closing is enough:
    run_forever already reconnects, and it now resolves configuration afresh
    on each pass.
    """
    client = _active_client
    if client is None or client.ws is None:
        return False
    try:
        client.ws.close()
        return True
    except Exception:
        return False


def get_status():
    """Tunnel state for GET /admin/tunnel.

    Returns a `configured: False` shape rather than None when the tunnel is
    switched off, so the UI can say "not configured" instead of erroring on a
    missing payload.
    """
    if _active_client is None:
        cfg = resolve_config()
        return {
            "configured": bool(cfg["server_url"]),
            "connected": False,
            "server_url": cfg["server_url"],
            "local_target": cfg["local_target"],
            "client_name": cfg["client_name"] or os.getenv("COMPUTERNAME") or "kingdom",
            "config_source": cfg["source"],
            "connected_at": None,
            "connected_seconds": 0,
            "reconnects": 0,
            "requests_served": 0,
            "requests_in_flight": 0,
            "websocket_sessions": 0,
            "last_error": None if cfg["server_url"] else "no tunnel server URL configured",
        }
    return _active_client.status()


if __name__ == "__main__":
    from gevent import monkey

    monkey.patch_all()

    from proxy_server.utils.logger import setup_logger

    setup_logger()

    _cfg = resolve_config()
    if not _cfg["server_url"] or not _cfg["token"]:
        raise SystemExit(
            "no tunnel settings found - set them in the admin panel, or put "
            "TUNNEL_SERVER_URL and TUNNEL_TOKEN in .env"
        )

    TunnelClient().run_forever()
