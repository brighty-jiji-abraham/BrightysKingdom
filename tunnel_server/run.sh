#!/usr/bin/env bash
# ===========================================================================
#  Tunnel head launcher (public server)
#
#    ./run.sh              Start the head          (default)
#    ./run.sh setup        Create venv + install dependencies
#    ./run.sh token        Generate a TUNNEL_TOKEN
#    ./run.sh help         Show this help
#
#  This folder is self-contained — server.py imports nothing from the rest of
#  BrightysKingdom, so you can scp just tunnel_server/ to the public host.
#
#  First deploy:
#      scp -r tunnel_server/ you@server:~/tunnel
#      ssh you@server
#      cd ~/tunnel && ./run.sh setup
#      cp .env.example .env && ./run.sh token      # paste into .env
#      ./run.sh                                    # foreground, to check it
#      pm2 start ecosystem.config.js               # then supervise it
# ===========================================================================
set -euo pipefail

cd "$(dirname "$0")"

MODE="${1:-start}"

usage() {
    cat <<'USAGE'
Tunnel head launcher

  ./run.sh              Start the head (default)
  ./run.sh setup        Create venv + install dependencies
  ./run.sh token        Generate a TUNNEL_TOKEN
  ./run.sh help         Show this help

Supervised:
  pm2 start ecosystem.config.js
  pm2 logs tunnel-head
USAGE
}

case "$MODE" in
    help|-h|--help) usage; exit 0 ;;
esac

# --- locate the interpreter ------------------------------------------------
# A venv inside this folder wins, so a standalone deploy is self-describing.
# Both layouts are listed because this also gets run under Git Bash and WSL
# against a Windows-made venv; without the Scripts/ entries it would fall
# through to a system python that lacks gevent and fail confusingly later.
find_python() {
    local candidate
    for candidate in \
        "${PYTHON:-}" \
        "./venv/bin/python" \
        "./venv/Scripts/python.exe" \
        "./.venv/bin/python" \
        "./.venv/Scripts/python.exe" \
        "../kingdom/bin/python" \
        "../kingdom/Scripts/python.exe" \
        "$(command -v python3 2>/dev/null || true)"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PY="$(find_python)"; then
    echo "[ERROR] No Python interpreter found." >&2
    echo "        Run:  ./run.sh setup" >&2
    exit 1
fi

case "$MODE" in
    setup)
        echo "[setup] using $PY"
        # If we only found a system python, build a venv next to server.py so
        # the head does not depend on system-wide packages.
        case "$PY" in
            ./venv/*|./.venv/*) ;;
            *)
                echo "[setup] creating venv at ./venv"
                "$PY" -m venv venv
                PY="./venv/bin/python"
                [ -x "$PY" ] || PY="./venv/Scripts/python.exe"
                ;;
        esac

        # An existing venv directory is not proof of a working one. Debian and
        # Ubuntu strip ensurepip out of the stdlib and ship it in python3-venv,
        # so `python3 -m venv` there can leave a venv behind with no pip in it.
        # That used to surface only at the install step, as a bare
        # "No module named pip" that said nothing about the cause.
        if ! "$PY" -m pip --version >/dev/null 2>&1; then
            echo "[setup] this venv has no pip - repairing with ensurepip"
            if ! "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
                echo "[setup] ensurepip unavailable, rebuilding the venv from scratch"
                rm -rf venv
                "${PYTHON:-python3}" -m venv venv >/dev/null 2>&1 || true
                PY="./venv/bin/python"
                [ -x "$PY" ] || PY="./venv/Scripts/python.exe"
            fi
        fi

        if [ ! -x "$PY" ] || ! "$PY" -m pip --version >/dev/null 2>&1; then
            echo "[ERROR] Could not get a working venv with pip." >&2
            echo "        Debian and Ubuntu ship venv and pip separately:" >&2
            echo >&2
            echo "            sudo apt install -y python3-venv python3-pip" >&2
            echo >&2
            echo "        Then remove the broken venv and try again:" >&2
            echo "            rm -rf venv && ./run.sh setup" >&2
            exit 1
        fi

        "$PY" -m pip install --upgrade pip || exit 1
        "$PY" -m pip install -r requirements.txt || exit 1
        echo
        echo "[setup] done. Next:"
        echo "    cp .env.example .env"
        echo "    ./run.sh token          # paste the value into .env"
        echo "    ./run.sh"
        exit 0
        ;;

    token)
        "$PY" -c "import secrets; print(secrets.token_urlsafe(32))"
        exit 0
        ;;
esac

# Python buffers stdout when it is not a tty, which makes logs look frozen
# under nohup, systemd and pm2. Unbuffer so output lands as it happens.
export PYTHONUNBUFFERED=1

# Git Bash / MSYS2 rewrites environment values that look like POSIX paths when
# it spawns a native Windows binary: STREAMING_ROUTES=/ollama silently becomes
# "C:/Program Files/Git/ollama", and TUNNEL_FALLBACKS is mangled beyond repair.
# Only values starting with "/" are affected - plain URLs come through intact.
# Harmless on Linux, where the variable is simply unused. Separator is ";".
export MSYS2_ENV_CONV_EXCL="TUNNEL_FALLBACKS;STREAMING_ROUTES;NO_AUTH_ROUTES"

# Printed because picking the wrong interpreter is the most common cause of a
# baffling failure a few lines later.
echo "[run.sh] interpreter: $PY"

mkdir -p logs

# server.py reads .env itself via python-dotenv; sourcing it here too makes
# the values visible to this script (for the token check and the banner).
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
else
    echo "[warn] no .env found - copy .env.example to .env and edit it"
fi

case "$MODE" in
    start)
        if [ -z "${TUNNEL_TOKEN:-}" ]; then
            echo "[ERROR] TUNNEL_TOKEN is not set (checked environment and .env)." >&2
            echo "        Without it the head refuses every client." >&2
            echo >&2
            echo "        ./run.sh token      # generate one, then put it in .env" >&2
            echo "        It must match TUNNEL_TOKEN in the Kingdom's .env." >&2
            exit 1
        fi
        echo "==========================================================="
        echo " Tunnel head"
        echo "==========================================================="
        echo " Public port  ${TUNNEL_PUBLIC_PORT:-9000}  /tunnel-agent  token-authenticated"
        echo " Local  port  ${TUNNEL_LOCAL_PORT:-9001}  forwarding     KEEP OFF THE INTERNET"
        echo " Status       http://127.0.0.1:${TUNNEL_LOCAL_PORT:-9001}/_tunnel/status"
        echo
        echo " Firewall the local port so only your own containers reach it."
        echo "==========================================================="
        exec "$PY" server.py
        ;;

    *)
        echo "[ERROR] Unknown mode \"$MODE\"" >&2
        echo >&2
        usage >&2
        exit 1
        ;;
esac
