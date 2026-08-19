# BrightysKingdom

A reverse proxy that puts every service running on one machine behind a single
port — and, with the included tunnel, makes that port reachable from a public
server without exposing the machine itself.

It exists because a developer workstation is a bad thing to put on the
internet: it sits behind NAT, its address changes, and opening a port per
service is neither practical nor safe. This gives you one door instead of many,
then lets that door be opened from the outside by something you control.

```
                                          ┌─ /app1    → 127.0.0.1:3000
   one endpoint ──►  BrightysKingdom  ────┼─ /app2    → 127.0.0.1:5000
                          :2000           ├─ /api     → 127.0.0.1:7000
                                          └─ /ollama  → 127.0.0.1:11434
```

## Features

- **Path-based routing** — one port fans out to any number of local backends.
- **Load balancing** across several instances per route (round-robin, random,
  least-connections) with background health checks that eject a failing backend
  after `UNHEALTHY_THRESHOLD` misses and restore it after `RECOVERY_THRESHOLD`
  successes. Health probing tries a configured endpoint, then falls back
  through `/health`, `/check-health`, `/status`, `/ping`, `/`.
- **Socket.IO proxying** with sticky sessions, so a client keeps reaching the
  backend instance that holds its session.
- **Streaming responses** relayed chunk-by-chunk instead of buffered, so
  Server-Sent Events and NDJSON arrive as they are produced.
- **Multipart passthrough** with raw streaming for uploads over 100 MB.
- **API-key auth** (`X-API-Key`, `Authorization: Bearer`, or `?api_key=`),
  token-bucket rate limiting keyed by API key or client IP, and a request
  filter that rejects path-traversal, XSS and SQL-injection patterns in URLs
  and in `User-Agent` / `Referer` headers.
- **Admin API** for backends, API keys, metrics and load-balancer state.
- **An outbound tunnel** (`tunnel_server/`) that publishes all of it from a
  public server, with reconnect, HTTP fallback and availability tracking.

## Requirements

- Python 3.13 (3.10+ should work; 3.13 is what this is developed against)
- Node.js only if you want PM2 process supervision

## Quick start

```bash
git clone --recurse-submodules <this-repo>
cd BrightysKingdom

python -m venv kingdom
kingdom/Scripts/pip install -r requirements.txt     # Windows
# ./kingdom/bin/pip install -r requirements.txt     # Linux/macOS

cp .env.example .env        # then edit it
```

Install the management UI too, if you want it:

```bash
git submodule update --init --recursive
start.bat setup        # Windows
./run.sh setup         # Linux/macOS
```

Start it:

| Platform | Command | Starts |
|---|---|---|
| Windows | `start.bat` | Proxy + frontend + tunnel client |
| Windows | `start.bat proxy` | Proxy only |
| Linux/macOS | `./run.sh kingdom` | Proxy + frontend + tunnel client |
| Linux/macOS | `./run.sh proxy` | Proxy only |
| Supervised | `pm2 start ecosystem.config.js --only kingdom` | Proxy only |

| Service | Default URL | Port set by |
|---|---|---|
| Proxy | <http://localhost:2000> | `PORT` |
| Admin UI | <http://localhost:5173> | `FRONTEND_UI_PORT` |
| Admin API | <http://localhost:5050> | `FRONTEND_API_PORT` |

Check the proxy with `curl localhost:2000/health`.

Frontend ports are resolved at launch and passed to the child processes
explicitly, so whatever you set is what actually gets bound and what the
launcher prints. Vite runs with `--strictPort` so it fails loudly instead of
quietly moving to the next free port and making the reported URL wrong.

> `PORT` in the root `.env` is the **proxy's** port. The admin API needs
> `FRONTEND_API_PORT` of its own: it would otherwise inherit `PORT` — `dotenv`
> does not override variables that are already set — and try to bind the port
> the proxy is already on.

The frontend is optional in the strict sense: if the submodule is not checked
out, `npm` is missing, or dependencies are not installed, the launchers print
what to do and **start the proxy anyway**. A missing admin UI never blocks the
thing production depends on.

The admin API also needs its own `FrontEnd/Proxy-Management/node/.env` (Mongo
URL, JWT secret). Without it, it starts and then crashes on the database
connection; the launchers warn about this before starting it.

> **Use `run_gevent.py`, not `main.py`.** The launchers already do.
> `main.py` starts the plain Flask dev server, which has no WebSocket support,
> so Socket.IO and the tunnel will not work.

## Configuration

Everything is environment-driven; `.env.example` carries the annotated list.
The parts you will actually touch:

```bash
# Backends. Comma-separate to load balance across instances.
APP1_URLS=http://127.0.0.1:3000
APP2_URLS=http://127.0.0.1:5000
API_URL=http://127.0.0.1:7000
OLLAMA_URLS=http://127.0.0.1:11434

MASTER_API_KEY=change-me
LOAD_BALANCER_STRATEGY=round_robin        # or random, least_connections

# Routes whose responses must never be buffered (SSE, NDJSON)
STREAMING_ROUTES=/ollama

# Outbound tunnel. Empty means a normal local-only run.
TUNNEL_SERVER_URL=
TUNNEL_TOKEN=
```

Adding a route takes one entry in `BACKEND_ROUTES` in
`proxy_server/config/settings.py` plus its `*_URLS` variable.

## Routes

| Path | Auth | Purpose |
|---|---|---|
| `/<app>/*` | API key | Proxied to the matching backend |
| `/ollama/*` | **none** | Proxied to Ollama — see Security notes |
| `/<app>/socket.io/*` | none | Socket.IO proxy with sticky sessions |
| `/<app>/webhooks/<service>/webhook/<user>` | none | Webhook passthrough |
| `/health`, `/proxy-stats`, `/debug-websocket` | none | Health and statistics |
| `/admin/*` | mixed | Admin API — see Security notes |

## The tunnel

`tunnel_server/` is a self-hosted alternative to ngrok, in two halves:

```
[public server]                              [this machine]
tunnel_server/server.py                       BrightysKingdom :2000
  :9000  /tunnel-agent  ◄═══ WebSocket ═════  tunnel client (dials OUT)
  :9001  /<anything>    ── forwarded ──┘            │
     ▲                                              ├─ /app1   → :3000
     │ your containers / apps                       └─ /ollama → :11434
     └─ http://host.docker.internal:9001/ollama/...
```

The local machine dials out and holds the connection open, so it needs no
public IP, no port forward and no inbound firewall rule. Only the public server
does.

It carries HTTP, streaming responses, and WebSockets (full duplex, text and
binary, multiplexed by session). It reconnects with exponential backoff, can
fail over to a configured HTTP upstream while the tunnel is down, and tracks
connection history and availability at `/_tunnel/status`, `/_tunnel/metrics`
and `/_tunnel/events`.

**See [`tunnel_server/README.md`](tunnel_server/README.md)** for setup,
configuration and limits.

## Deployment

```bash
pm2 start ecosystem.config.js --only kingdom       # this machine
pm2 start ecosystem.config.js --only tunnel-head   # public server
pm2 save && pm2 startup
```

`ecosystem.config.js` holds no secrets — it is committed and reads config from
`.env`. There is a second one inside `tunnel_server/` for deploying just the
tunnel head standalone.

## Project layout

```
proxy_server/
  core/app.py              Flask application factory
  core/proxy.py            Forwarding, streaming, multipart
  core/websocket_tunnel.py Socket.IO bridge
  core/tunnel_client.py    Outbound tunnel client
  config/settings.py       All configuration and route tables
  middleware/              Auth, rate limiting, request filtering
  services/                Load balancing, health checks, metrics
  routes/                  Proxy and admin blueprints
tunnel_server/             Tunnel head — self-contained, deployable alone
FrontEnd/Proxy-Management  Management UI (git submodule)
run_gevent.py              Entrypoint (gevent + WebSocket support)
start.bat / run.sh         Launchers
ecosystem.config.js        PM2 definitions
```

## Security notes

Read these before putting this anywhere public. It is designed to sit on a
private machine behind the tunnel, and several defaults assume that.

- **Most `/admin/*` read endpoints are unauthenticated.** `/admin/backends`,
  `/admin/backends/summary`, `/admin/routes`, `/admin/websocket-sessions` and
  `/admin/api-keys/validate` require no key at all; `/admin/metrics` and
  `/admin/load-balancer/stats` use `optional_auth`, which does not enforce
  anything. Between them they disclose backend URLs, the route table and
  session data. Writes (`/admin/backends/add`, `/admin/api-keys`,
  `/admin/config`) do require the master key.
- **`/ollama/*` is deliberately unauthenticated.** The LangChain Ollama
  embedding client sends no auth header and offers no hook to add one, so
  requiring a key there would reject every embedding call. If you expose this
  proxy directly, put an IP allowlist in front of `/ollama` — an open Ollama
  lets anyone generate on your GPU, pull models until the disk fills, or delete
  them.
- **API keys live in memory only.** They are lost on restart; only
  `MASTER_API_KEY` is restored from config. Keys created through
  `/admin/api-keys` do not survive a restart and are not shared across
  processes. `GET /admin/api-keys` returns them in plaintext.
- **Rate limiting is per-process and in-memory**, so it does not hold across a
  restart or multiple workers.
- **The root `.env` is tracked in git** in this repository, so anything
  committed there is in history. `tunnel_server/.env` is gitignored; the root
  one is not, because it predates that decision. Use `git rm --cached .env` if
  you want it out.

## Known limits

- **Large uploads through the tunnel are buffered.** The proxy itself streams
  multipart over 100 MB, but the tunnel reads the request body whole. Fine for
  API traffic and Socket.IO, not for file transfer.
- **Only `/socket.io/` paths can upgrade to WebSocket through the proxy.**
  Other WebSocket paths fall through to Flask routing, which rejects the
  upgrade unless the rule is declared `websocket=True`.
- **Git Bash on Windows corrupts config values that start with `/`** via MSYS
  path conversion — `STREAMING_ROUTES=/ollama` silently becomes a Windows path.
  The `run.sh` scripts set `MSYS2_ENV_CONV_EXCL` to prevent it; if you launch
  Python by hand there, use `cmd`.
- `proxy_server/routes/proxy_routes_old.py` is superseded and unused.
