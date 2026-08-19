# Tunnel — self-hosted ngrok for BrightysKingdom

BrightysKingdom already multiplexes every local service behind one port. This
adds the missing half: a **tunnel head** you run on a public server, which the
Kingdom dials out to. One outbound WebSocket publishes every local service.

```
[public server]                                [local machine]
tunnel_server/server.py                         BrightysKingdom :2000
  :9000  /tunnel-agent  <==== WebSocket ======  tunnel_client (dials OUT)
  :9001  /<anything>    ── forwarded ──┘              │
     ▲                                                ├─ /app1   → :3000
     │ chatbot containers                             ├─ /app2   → :5000
     └─ http://host.docker.internal:9001/ollama/...   └─ /ollama → :11434
```

The local machine needs no public IP, no port forward, no inbound firewall
rule. Only the public server does.

## Why two ports

The forwarding surface **cannot** be authenticated. The chatbot's embedding
client (`langchain_ollama`) sends no auth header and offers no hook to add one,
so requiring a key there would 401 every embed call and break RAG.

So the surfaces are split across separate listeners:

| Port | Serves | Exposure |
|---|---|---|
| `TUNNEL_PUBLIC_PORT` (9000) | `/tunnel-agent` only, token-authenticated | **Internet-facing** |
| `TUNNEL_LOCAL_PORT` (9001) | The forwarding surface, no auth | **Keep off the internet** |

Collapsing these onto one port would publish an unauthenticated route to every
service on your local machine. Each listener hard-refuses the other's paths.

## Running it

Three launchers, so neither machine needs a remembered command line.

| | Command | Runs |
|---|---|---|
| Local machine (Windows) | `start.bat` | Proxy + tunnel client |
| Local machine | `start.bat head` | Tunnel head, for testing the chain on one box |
| Local machine | `start.bat tunnel` | Tunnel client alone, for debugging |
| Server (Linux/macOS) | `./run.sh` | Tunnel head |
| Server | `./run.sh kingdom` | Proxy + tunnel client |
| Server, supervised | `pm2 start ecosystem.config.js --only tunnel-head` | Tunnel head under PM2 |

All three read `.env`, so configuration lives in one place. `run.sh` prints
which interpreter it selected — a wrong pick is the usual cause of an
otherwise baffling `ModuleNotFoundError`. Override it with
`PYTHON=/usr/bin/python3.12 ./run.sh`.

### PM2 on the server

```bash
pm2 start ecosystem.config.js --only tunnel-head
pm2 logs tunnel-head
pm2 save && pm2 startup        # then run the command it prints, as root
```

`ecosystem.config.js` holds no secrets — it is committed. `TUNNEL_TOKEN` comes
from `.env`, which the app loads itself. The config sets `PYTHONUNBUFFERED=1`
(without it Python buffers stdout when not on a tty and `pm2 logs` looks
frozen) and `exec_mode: fork` (Python cannot use PM2's cluster mode).

PM2 does not rotate logs by default and these are chatty:

```bash
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 50M
pm2 set pm2-logrotate:retain 7
```

## Fallback when the tunnel is down

Without a fallback, every request gets a 503 for as long as the local machine
is offline. Point a prefix at a stand-in upstream instead:

```
TUNNEL_FALLBACKS=/ollama=http://127.0.0.1:11434
```

Longest matching prefix wins, and several can be listed comma-separated. When
the tunnel is unavailable the head forwards there over plain HTTP, streaming
the response the same way. When the client reconnects, traffic returns to the
tunnel on the very next request — there is nothing to restart and no flag to
flip.

Every response carries `X-Tunnel-Source: tunnel` or `X-Tunnel-Source:
fallback`, so which path served a given request is visible from the caller.

**The prefix is stripped for the fallback but not for the tunnel.** That
asymmetry is deliberate: the tunnel hands the whole path to the Kingdom, whose
load balancer strips it, while a fallback usually points straight at a backend
that expects `/api/tags`, not `/ollama/api/tags`. Set
`TUNNEL_FALLBACK_STRIP_PREFIX=false` if yours expects the prefix.

Failover happens only *before* a response head is committed to the wire, which
is the only point where it is safe. `TUNNEL_FALLBACK_ON_ERROR` (default on)
also fails over on timeouts and client errors, not just an absent client — but
a timeout waits `TUNNEL_FIRST_BYTE_TIMEOUT` first, so it trades worst-case
latency for availability.

**WebSockets do not fail over.** Only HTTP does. A tunnelled WebSocket is
refused while the tunnel is down.

## Tracking

Three endpoints, served on the private port only so they leak nothing publicly:

| Endpoint | Shows |
|---|---|
| `/_tunnel/status` | Current state: `tunnel_online`, `degraded`, configured fallbacks, availability |
| `/_tunnel/metrics` | Counters, connect/disconnect totals, uptime vs downtime |
| `/_tunnel/events?limit=N` | Recent connect / disconnect / fallback events, newest last |

`healthy` answers "can this head serve requests?", which stops being the same
question as "is the tunnel up" once a fallback exists — serving via fallback is
healthy but `degraded`. **Alert on `tunnel_online`, not `healthy`**, if you
want to hear about the GPU box specifically.

`availability_percent` is the share of the head's lifetime with a client
attached. A low number means the far end is flapping even when it happens to
be up right now.

Events are an in-memory ring buffer of the last 200 — they survive a flapping
client, not a head restart. The durable record is stdout, which pm2 captures to
`logs/tunnel-head.out.log`.

## Setup — public server

This folder is self-contained: `server.py` imports nothing from the rest of
BrightysKingdom, so copy just `tunnel_server/` to the host.

```bash
scp -r tunnel_server/ you@server:~/tunnel
ssh you@server
cd ~/tunnel

./run.sh setup                 # creates ./venv, installs requirements.txt
cp .env.example .env
./run.sh token                 # generate TUNNEL_TOKEN, paste it into .env

./run.sh                       # foreground, to check it comes up
pm2 start ecosystem.config.js  # then supervise it
```

`./run.sh` on its own starts the head. Other modes: `setup`, `token`, `help`.
On Windows use `start.bat` with the same modes.

Firewall `TUNNEL_LOCAL_PORT` so only your own containers reach it. If Docker
containers are the only consumers, narrowing `TUNNEL_LOCAL_BIND` to the docker
bridge address is tighter still.

`tunnel_server/.env` is gitignored. The repo root `.env` is **not** — it was
already tracked before these files existed, so do not put the tunnel token
there expecting it to stay private.

### Files in this folder

| File | Purpose |
|---|---|
| `server.py` | The head. No dependencies on the rest of the repo. |
| `requirements.txt` | Flask, gevent, gevent-websocket, python-dotenv |
| `.env.example` | Copy to `.env` and set `TUNNEL_TOKEN` |
| `run.sh` | Linux/macOS launcher — `setup`, `token`, `help` |
| `start.bat` | Windows launcher, same modes |
| `ecosystem.config.js` | PM2, for a standalone deploy |

There is a second `ecosystem.config.js` at the repo root which defines both
this app and the Kingdom, for running from a full checkout. Both name the app
`tunnel-head`, so pm2 commands are the same either way — just do not start
both.

## Setup — local machine

In the Kingdom's `.env`:

```
TUNNEL_SERVER_URL=ws://your-server:9000/tunnel-agent
TUNNEL_TOKEN=<same value as the server>
TUNNEL_CLIENT_NAME=gpu-box
```

Then start it as usual with `python run_gevent.py` — **not** `main.py`, which
has no WebSocket support. The tunnel client starts automatically when
`TUNNEL_SERVER_URL` is set, and is a no-op when it isn't, so local-only runs
are unaffected. It reconnects with exponential backoff (1s → 30s) on drops.

Debug it standalone with `python -m proxy_server.core.tunnel_client`.

## Pointing the chatbot at Ollama

In the chatbot's `.env` on the production server:

```
OLLAMA_HOST="http://host.docker.internal:9001/ollama"
```

No trailing `/v1` — the chat factory appends it and the embedding client uses
the bare root, so one value serves both.

Per-tenant `botconfigurations.ollamaBaseUrl` in Mongo **overrides** this env
var. If a tenant has it set, edit it there too or the env change silently does
nothing for that tenant.

## Configuration

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `TUNNEL_TOKEN` | both | *(none — required)* | Shared secret |
| `TUNNEL_PUBLIC_PORT` | server | `9000` | Agent WebSocket |
| `TUNNEL_LOCAL_PORT` | server | `9001` | Forwarding surface |
| `TUNNEL_PUBLIC_BIND` / `TUNNEL_LOCAL_BIND` | server | `0.0.0.0` | Bind addresses |
| `TUNNEL_FIRST_BYTE_TIMEOUT` | server | `120` | Wait for the response head |
| `TUNNEL_IDLE_TIMEOUT` | server | `300` | Max gap between chunks |
| `TUNNEL_WS_OPEN_TIMEOUT` | server | `30` | Wait for the backend socket to open |
| `TUNNEL_FALLBACKS` | server | *(none)* | `/prefix=url,...` — where to send traffic while the tunnel is down |
| `TUNNEL_FALLBACK_ON_ERROR` | server | `true` | Also fail over on timeout/error, not just an absent client |
| `TUNNEL_FALLBACK_STRIP_PREFIX` | server | `true` | Remove the route prefix before calling the fallback |
| `TUNNEL_FALLBACK_READ_TIMEOUT` | server | `600` | Read timeout for the fallback upstream |
| `TUNNEL_SERVER_URL` | client | *(empty — local-only)* | Where to dial |
| `TUNNEL_LOCAL_TARGET` | client | `http://127.0.0.1:2000` | The Kingdom itself |
| `TUNNEL_READ_TIMEOUT` | client | `600` | Long enough for `/api/pull` |
| `TUNNEL_WS_CONNECT_TIMEOUT` | client | `20` | Backend WebSocket connect only — cleared afterwards so idle sockets are not reaped |
| `OLLAMA_URLS` | Kingdom | `http://127.0.0.1:11434` | Local Ollama |
| `STREAMING_ROUTES` | Kingdom | `/ollama` | Routes never buffered |

## What the tunnel carries

| | Supported | Notes |
|---|---|---|
| HTTP request/response | yes | Any method, any path |
| Streaming responses | yes | SSE and NDJSON relayed chunk-by-chunk |
| WebSockets | yes | Full duplex, text + binary, multiplexed by session id |
| Large request bodies | **no** | Read whole into memory and sent as one frame — see Limits |

## Verified behaviour

Tested end-to-end over the full chain against real Ollama (`qwen2.5:14b`,
`qwen3-embedding`) and a WebSocket echo backend:

**HTTP**
- `/api/tags`, `/api/embed`, `/api/version` relay correctly
- SSE chat streams incrementally — 39 chunks over 0.64s, not one lump
- 6 concurrent embeds multiplex over one socket, 6/6 distinct, no cross-wiring
- Bad token refused without disturbing the connected client
- `/tunnel-agent` refused on the private port; forwarding refused on the public port
- `require_auth` still enforced on `/app1` (401) while `/ollama` is exempt
- Client displaced by a newer connection reconnects automatically and resumes

**Fallback and tracking**
- Tunnel down → served by the fallback, `X-Tunnel-Source: fallback`
- Route prefix stripped for the fallback (`/ollama/api/tags` → `/api/tags`),
  preserved for the tunnel — verified by an upstream that echoes its path
- Client reconnects → next request returns to the tunnel automatically,
  `X-Tunnel-Source: tunnel`, `degraded: false`
- Client killed again → fails back, no restart needed
- Events recorded the full cycle: fallback → connect → disconnect (with
  reason) → fallback
- Counters, availability %, uptime and downtime all tracked correctly
- A path with no matching fallback entry still 503s, with a hint naming the
  path that did not match

**WebSocket**
- Unprompted server→client push arrives (proves full duplex, not request/reply)
- Text and binary frames both relay; binary survives base64 byte-for-byte
- 20 rapid round trips stay in order
- Path, query string (`?EIO=4`) and custom headers reach the backend
- Two concurrent sockets stay independent, no cross-wiring
- Sockets survive 30s idle
- HTTP and WebSocket traffic coexist on one tunnel
- Clean close releases the session on both ends (`websockets: 0`)

## Limits

**Large uploads are buffered.** The request body is read whole and base64'd
into a single WebSocket frame. The Kingdom itself allows 1GB and has a 64KB
chunked path for >100MB multipart, so for big uploads the tunnel is the
bottleneck and will exhaust memory. Streaming request bodies would need
`req_chunk` frames mirroring the response side. Fine for API traffic and
Socket.IO; not for file transfer.

**Non-Socket.IO WebSocket paths through the Kingdom.** The Kingdom's own
`CompleteSocketIOHandler` only intercepts paths containing `/socket.io/`;
other WebSocket paths fall through to Flask routing, where Werkzeug rejects
them unless the rule is declared `websocket=True`. That is a Kingdom
limitation, not a tunnel one — the tunnel relays whatever the Kingdom serves.

## Operational notes

**Streaming needed a proxy fix.** `_forward_standard_request` used
`stream=False` and `_create_response` did `Response(resp.content, ...)`, which
buffers the whole body. That is invisible for ordinary JSON but fatal for SSE —
the caller would sit silent for the entire generation then receive every token
at once. It now picks the streaming path by response content type
(`text/event-stream`, `application/x-ndjson`) or by `STREAMING_ROUTES` prefix.

**`/ollama` is deliberately unauthenticated** — see the docstring on
`proxy_ollama_request` in `proxy_server/routes/proxy_routes.py`. If you ever
expose the Kingdom directly, put an IP allowlist in front of it. An open Ollama
lets anyone generate on your GPU, `/api/pull` until the disk fills, or
`/api/delete` your models.

**Use `wss://` in production.** `ws://` sends the token in clear text. Put the
public port behind TLS before running this across the internet.

**Embedding dimensions are load-bearing.** The chatbot stamps each FAISS index
with its embedding provider and dimension and refuses to load on a mismatch.
`qwen3-embedding:latest` returns **4096** dims, `qwen3-embedding:0.6b` returns
1024 — serving the wrong tag means rebuilding every tenant index. Match what
production was already using.

**One client at a time.** A second client connecting displaces the first
(last-writer-wins), which is what you want after a network blip but means two
machines sharing a token will fight.
