#!/usr/bin/env bash
# ===========================================================================
#  BrightysKingdom launcher (Linux / macOS)
#
#    ./run.sh              Start the tunnel head   (the public server; default)
#    ./run.sh kingdom      Start the proxy + frontend + tunnel client
#    ./run.sh proxy        Start the proxy only (no frontend)
#    ./run.sh setup        Install frontend dependencies
#    ./run.sh tunnel       Start only the tunnel client (debugging)
#    ./run.sh help         Show this help
#
#  On the public server you normally want pm2 rather than this script, so the
#  head survives reboots and restarts on crash:
#      pm2 start ecosystem.config.js --only tunnel-head
#  This script is for foreground runs and for debugging.
# ===========================================================================
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

MODE="${1:-head}"

FRONTEND_DIR="$ROOT/FrontEnd/Proxy-Management"
NODE_DIR="$FRONTEND_DIR/node"
VITE_DIR="$FRONTEND_DIR/vite-project"

usage() {
    cat <<'USAGE'
BrightysKingdom launcher

  ./run.sh              Start the tunnel head (public server; default)
  ./run.sh kingdom      Start the proxy + frontend + tunnel client
  ./run.sh proxy        Start the proxy only (no frontend)
  ./run.sh setup        Install frontend dependencies (npm install)
  ./run.sh tunnel       Start only the tunnel client (debugging)
  ./run.sh help         Show this help

For a managed deploy on the server:
  pm2 start ecosystem.config.js --only tunnel-head
USAGE
}

case "$MODE" in
    help|-h|--help) usage; exit 0 ;;
esac

# --- locate the interpreter ------------------------------------------------
# Both venv layouts are listed: bin/ on POSIX, Scripts/ on Windows, because
# this script also gets run under Git Bash and WSL against a Windows-made
# venv. Without the Scripts/ entries it silently falls through to a system
# python3 that has none of the dependencies, then dies later with a
# confusing ModuleNotFoundError instead of failing here.
# Ordered most-specific first so an explicit PYTHON always wins.
PY=""
for candidate in \
    "${PYTHON:-}" \
    "./venv/bin/python" \
    "./venv/Scripts/python.exe" \
    "./.venv/bin/python" \
    "./.venv/Scripts/python.exe" \
    "./kingdom/bin/python" \
    "./kingdom/Scripts/python.exe" \
    "$(command -v python3 2>/dev/null || true)"
do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "[ERROR] No Python interpreter found." >&2
    echo "        Create one:  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
    echo "        Or point at one:  PYTHON=/usr/bin/python3.12 ./run.sh" >&2
    exit 1
fi

# Python buffers stdout when it is not a tty, which makes logs appear frozen
# under nohup, systemd and pm2. Unbuffer so output lands as it happens.
export PYTHONUNBUFFERED=1

# Git Bash / MSYS2 rewrites environment values that look like POSIX paths when
# it spawns a native Windows binary: STREAMING_ROUTES=/ollama silently becomes
# "C:/Program Files/Git/ollama", and TUNNEL_FALLBACKS is mangled beyond repair.
# Only values starting with "/" are affected - plain URLs come through intact.
# Harmless on Linux, where the variable is simply unused. Separator is ";".
export MSYS2_ENV_CONV_EXCL="TUNNEL_FALLBACKS;STREAMING_ROUTES;NO_AUTH_ROUTES"

# Printed because picking the wrong interpreter is the most common cause
# of a baffling failure a few lines later.
echo "[run.sh] interpreter: $PY"

mkdir -p logs

# --- .env ------------------------------------------------------------------
# The apps load .env themselves via python-dotenv; sourcing it here as well
# makes values visible to this script (for the token check below) and to
# anything it spawns.
#
# Variables already set in the environment WIN over .env, matching what
# python-dotenv does. Plain `set -a; . ./.env` does the opposite - it
# overwrites them - so `PROXY_MONGO_URL=... ./run.sh` was silently ignored
# while the same override worked when the app was started directly. Having the
# two disagree makes overrides untrustworthy, so they now behave the same.
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
            *=*) ;;
            *) continue ;;
        esac
        key=${line%%=*}
        key=$(printf '%s' "$key" | tr -d '[:space:]')
        case "$key" in
            ''|*[!A-Za-z0-9_]*) continue ;;
        esac
        # Skip anything already exported, so CLI overrides survive.
        if [ -n "$(eval "printf '%s' \"\${$key+set}\"")" ]; then
            continue
        fi
        value=${line#*=}
        # Strip one layer of surrounding quotes, as dotenv does.
        case "$value" in
            \"*\") value=${value#\"}; value=${value%\"} ;;
            \'*\') value=${value#\'}; value=${value%\'} ;;
        esac
        export "$key=$value"
    done < .env
else
    echo "[warn] no .env found - copy .env.example to .env and edit it"
fi

require_token() {
    if [ -z "${TUNNEL_TOKEN:-}" ]; then
        echo "[ERROR] TUNNEL_TOKEN is not set (checked the environment and .env)." >&2
        echo "        Without it the head refuses every client." >&2
        echo >&2
        echo "        Generate one:" >&2
        echo "            $PY -c 'import secrets; print(secrets.token_urlsafe(32))'" >&2
        echo "        Then put it in .env on BOTH machines." >&2
        exit 1
    fi
}

# ===========================================================================
#  Frontend (FrontEnd/Proxy-Management, a git submodule)
#
#  Two services: an Express API and a Vite dev server. Both are optional -
#  every check below degrades to a warning rather than an error, because a
#  missing frontend must never stop the proxy from starting. The proxy is the
#  thing production depends on; the admin UI is not.
# ===========================================================================

FRONTEND_PIDS=""

frontend_available() {
    if [ ! -f "$VITE_DIR/package.json" ]; then
        echo "[warn] frontend not checked out - skipping"
        echo "       git submodule update --init --recursive"
        return 1
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "[warn] npm not on PATH - skipping frontend"
        return 1
    fi
    if [ ! -d "$NODE_DIR/node_modules" ] || [ ! -d "$VITE_DIR/node_modules" ]; then
        echo "[warn] frontend dependencies not installed - skipping"
        echo "       ./run.sh setup"
        return 1
    fi
    return 0
}

start_frontend() {
    frontend_available || return 0

    # The Express API needs its own .env (Mongo URL, JWT secret, mail creds).
    # Start it anyway if missing so the failure shows up in its own log rather
    # than being silently skipped, but say why it is likely to die.
    if [ ! -f "$NODE_DIR/.env" ]; then
        echo "[warn] $NODE_DIR/.env missing - the admin API will probably fail to start"
        echo "       cp FrontEnd/Proxy-Management/node/.env.example FrontEnd/Proxy-Management/node/.env"
    fi

    local api_port ui_port
    api_port="$(resolve_api_port)"
    ui_port="$(resolve_ui_port)"

    # PORT must be passed explicitly. Sourcing the root .env above exported
    # PORT=2000 (the PROXY's port), and dotenv does not override variables
    # that are already set - so node/.env's own PORT would be ignored and the
    # admin API would try to bind the proxy's port and collide with it.
    ( cd "$NODE_DIR" && PORT="$api_port" npm start ) \
        > "$ROOT/logs/frontend-api.log" 2>&1 &
    FRONTEND_PIDS="$FRONTEND_PIDS $!"
    echo " Admin API  http://localhost:$api_port         (logs/frontend-api.log)"

    # --strictPort makes vite fail loudly rather than quietly taking the next
    # free port, which would make the URL printed below a lie.
    ( cd "$VITE_DIR" && npm run dev -- --port "$ui_port" --strictPort ) \
        > "$ROOT/logs/frontend-ui.log" 2>&1 &
    FRONTEND_PIDS="$FRONTEND_PIDS $!"
    echo " Admin UI   http://localhost:$ui_port         (logs/frontend-ui.log)"
}

env_value() {
    # env_value <file> <key> - last assignment wins, quotes and spaces stripped
    [ -f "$1" ] || return 0
    grep -E "^[[:space:]]*$2=" "$1" 2>/dev/null \
        | tail -1 \
        | sed -e 's/^[^=]*=//' -e 's/[[:space:]]//g' -e 's/"//g'
}

resolve_api_port() {
    # FRONTEND_API_PORT (root .env) wins, then the admin API's own PORT, then
    # the upstream default. Never the root .env's PORT - that is the proxy's.
    if [ -n "${FRONTEND_API_PORT:-}" ]; then
        printf '%s' "$FRONTEND_API_PORT"
        return
    fi
    local found
    found="$(env_value "$NODE_DIR/.env" PORT)"
    printf '%s' "${found:-5050}"
}

resolve_ui_port() {
    # Vite has no port in vite.config.js, so its default is 5173 unless that
    # is taken. Passing it explicitly keeps the reported URL honest.
    printf '%s' "${FRONTEND_UI_PORT:-5173}"
}

stop_frontend() {
    # Without this, Ctrl+C kills only the proxy and leaves node and vite
    # holding their ports, so the next start fails with EADDRINUSE.
    for pid in $FRONTEND_PIDS; do
        kill "$pid" 2>/dev/null || true
    done
}

install_frontend() {
    if [ ! -f "$VITE_DIR/package.json" ]; then
        echo "[ERROR] frontend not checked out." >&2
        echo "        git submodule update --init --recursive" >&2
        exit 1
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "[ERROR] npm not found - install Node.js first." >&2
        exit 1
    fi

    echo "[setup] installing admin API dependencies"
    ( cd "$NODE_DIR" && npm install )

    echo "[setup] installing admin UI dependencies"
    ( cd "$VITE_DIR" && npm install )

    if [ ! -f "$NODE_DIR/.env" ]; then
        echo
        echo "[setup] the admin API still needs its own .env:"
        echo "    cp FrontEnd/Proxy-Management/node/.env.example FrontEnd/Proxy-Management/node/.env"
        echo "  then set connectionURL (MongoDB) and JWT_SECRET."
    fi
    echo
    echo "[setup] done. Start everything with:  ./run.sh kingdom"
}

run_kingdom() {
    local with_frontend="$1"

    echo "==========================================================="
    echo " BrightysKingdom"
    echo "==========================================================="
    echo " Proxy      http://localhost:2000"
    echo " Health     http://localhost:2000/health"
    echo " Ollama     http://localhost:2000/ollama/api/tags"

    if [ "$with_frontend" = "yes" ]; then
        start_frontend
    fi

    echo
    if [ -n "${TUNNEL_SERVER_URL:-}" ]; then
        echo " Tunnel     dialling ${TUNNEL_SERVER_URL}"
    else
        echo " Tunnel     disabled (TUNNEL_SERVER_URL unset) - local-only run"
    fi
    echo "==========================================================="

    if [ -n "$FRONTEND_PIDS" ]; then
        # Deliberately not `exec`: the trap has to survive so the frontend
        # children are cleaned up when this script exits.
        trap stop_frontend EXIT INT TERM
        "$PY" run_gevent.py
    else
        exec "$PY" run_gevent.py
    fi
}

case "$MODE" in
    head)
        require_token
        echo "==========================================================="
        echo " Tunnel head"
        echo "==========================================================="
        echo " Public port  ${TUNNEL_PUBLIC_PORT:-9000}  /tunnel-agent  token-authenticated"
        echo " Local  port  ${TUNNEL_LOCAL_PORT:-9001}  forwarding     KEEP OFF THE INTERNET"
        echo " Status       http://127.0.0.1:${TUNNEL_LOCAL_PORT:-9001}/_tunnel/status"
        echo
        echo " Firewall the local port so only your own containers reach it."
        echo "==========================================================="
        exec "$PY" tunnel_server/server.py
        ;;

    kingdom)
        run_kingdom yes
        ;;

    proxy)
        run_kingdom no
        ;;

    setup)
        install_frontend
        ;;

    tunnel)
        require_token
        if [ -z "${TUNNEL_SERVER_URL:-}" ]; then
            echo "[ERROR] TUNNEL_SERVER_URL is not set - nothing to dial." >&2
            exit 1
        fi
        echo "Tunnel client only - expects the proxy already running on port 2000"
        exec "$PY" -m proxy_server.core.tunnel_client
        ;;

    *)
        echo "[ERROR] Unknown mode \"$MODE\"" >&2
        echo >&2
        usage >&2
        exit 1
        ;;
esac
