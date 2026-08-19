@echo off
REM ===========================================================================
REM  BrightysKingdom launcher (Windows / local machine)
REM
REM    start.bat            Start the proxy + frontend + tunnel client
REM    start.bat proxy      Start the proxy only (no frontend)
REM    start.bat setup      Install frontend dependencies (npm install)
REM    start.bat head       Start the tunnel head locally    (testing only)
REM    start.bat tunnel     Start ONLY the tunnel client     (debugging)
REM    start.bat help       Show this help
REM
REM  The proxy publishes every local service (app1, app2, ollama, ...) on one
REM  port. If TUNNEL_SERVER_URL is set in .env it also dials out to the tunnel
REM  head, making those services reachable from the public server without
REM  exposing this machine. Unset, it just runs locally.
REM
REM  The frontend (FrontEnd\Proxy-Management) opens in two separate windows so
REM  each service's output stays readable. Closing this window does NOT close
REM  them - close them yourself, or use `start.bat proxy` to skip them.
REM ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=kingdom"

if /i "%MODE%"=="help"   goto :usage
if /i "%MODE%"=="-h"     goto :usage
if /i "%MODE%"=="--help" goto :usage

set "FRONTEND=%~dp0FrontEnd\Proxy-Management"
set "NODE_DIR=%FRONTEND%\node"
set "VITE_DIR=%FRONTEND%\vite-project"

REM --- locate the interpreter ------------------------------------------------
set "PY=%~dp0kingdom\Scripts\python.exe"
if exist "%PY%" goto :got_python
echo [warn] venv not found at kingdom\Scripts\python.exe - falling back to system python
set "PY=python"
where python >nul 2>&1 || goto :no_python
:got_python

if /i "%MODE%"=="setup" goto :run_setup

REM --- preflight -------------------------------------------------------------
if not exist "logs" mkdir "logs"

if exist ".env" goto :got_env
echo [warn] no .env file found - copy .env.example to .env and edit it
echo        defaults will be used for everything
:got_env

if /i "%MODE%"=="kingdom" goto :run_kingdom
if /i "%MODE%"=="proxy"   goto :run_proxy
if /i "%MODE%"=="head"    goto :run_head
if /i "%MODE%"=="tunnel"  goto :run_tunnel

echo [ERROR] Unknown mode "%MODE%"
echo.
call :print_usage
exit /b 1


:run_kingdom
call :start_frontend
goto :run_proxy


:run_proxy
echo ===========================================================
echo  BrightysKingdom
echo ===========================================================
echo  Proxy      http://localhost:2000
echo  Health     http://localhost:2000/health
echo  Ollama     http://localhost:2000/ollama/api/tags
if /i "%MODE%"=="kingdom" if "%FRONTEND_STARTED%"=="1" (
  echo  Admin API  http://localhost:%API_PORT%
  echo  Admin UI   http://localhost:%UI_PORT%
)
echo.
echo  The tunnel client starts automatically when TUNNEL_SERVER_URL
echo  is set in .env. Without it this is a normal local-only run.
echo.
echo  Ctrl+C to stop the proxy.
echo ===========================================================
echo.
"%PY%" run_gevent.py
goto :done


REM ---------------------------------------------------------------------------
REM  Frontend. Every check below only warns: a missing admin UI must never stop
REM  the proxy from starting, because the proxy is what production depends on.
REM ---------------------------------------------------------------------------
:start_frontend
set "FRONTEND_STARTED="

if exist "%VITE_DIR%\package.json" goto :fe_have_src
echo [warn] frontend not checked out - skipping
echo        git submodule update --init --recursive
goto :eof
:fe_have_src

where npm >nul 2>&1 || goto :fe_no_npm

if exist "%NODE_DIR%\node_modules" if exist "%VITE_DIR%\node_modules" goto :fe_have_deps
echo [warn] frontend dependencies not installed - skipping
echo        start.bat setup
goto :eof
:fe_have_deps

REM --- resolve ports dynamically ---------------------------------------------
REM Precedence: FRONTEND_API_PORT / FRONTEND_UI_PORT in the root .env, then the
REM admin API's own PORT, then the upstream defaults. Deliberately never the
REM root .env's PORT - that one is the PROXY's port (2000), and letting it
REM through would point the admin API at a port already in use.
set "API_PORT=5050"
set "UI_PORT=5173"
call :read_env "%NODE_DIR%\.env" PORT API_PORT
call :read_env "%~dp0.env" FRONTEND_API_PORT API_PORT
call :read_env "%~dp0.env" FRONTEND_UI_PORT UI_PORT

if exist "%NODE_DIR%\.env" goto :fe_have_env
echo [warn] node\.env missing - the admin API will probably fail to start
echo        copy FrontEnd\Proxy-Management\node\.env.example FrontEnd\Proxy-Management\node\.env
:fe_have_env

REM /k keeps each window open after the process exits, so a crash stays
REM readable instead of vanishing with the window.
REM PORT is set explicitly: dotenv does not override an inherited value, so
REM leaving it to chance risks the admin API binding the wrong port.
REM --strictPort makes vite fail loudly rather than silently taking the next
REM free port, which would make the URL reported below wrong.
start "Proxy Admin API" /D "%NODE_DIR%" cmd /k "set PORT=%API_PORT%&& npm start"
start "Proxy Admin UI"  /D "%VITE_DIR%" cmd /k "npm run dev -- --port %UI_PORT% --strictPort"
set "FRONTEND_STARTED=1"
goto :eof


:read_env
REM read_env <file> <key> <target-var> - last assignment wins, quotes stripped
if not exist %1 goto :eof
for /f "usebackq eol=# tokens=1,* delims==" %%A in (%1) do (
  if /i "%%~A"=="%~2" set "%~3=%%~B"
)
call set "%~3=%%%~3:"=%%"
goto :eof

:fe_no_npm
echo [warn] npm not on PATH - skipping frontend (install Node.js to enable it)
goto :eof


:run_setup
if exist "%VITE_DIR%\package.json" goto :setup_have_src
echo [ERROR] frontend not checked out.
echo         git submodule update --init --recursive
exit /b 1
:setup_have_src

where npm >nul 2>&1 || goto :setup_no_npm

echo [setup] installing admin API dependencies
pushd "%NODE_DIR%" && call npm install & popd

echo [setup] installing admin UI dependencies
pushd "%VITE_DIR%" && call npm install & popd

if exist "%NODE_DIR%\.env" goto :setup_done
echo.
echo [setup] the admin API still needs its own .env:
echo     copy FrontEnd\Proxy-Management\node\.env.example FrontEnd\Proxy-Management\node\.env
echo   then set connectionURL (MongoDB) and JWT_SECRET.
:setup_done
echo.
echo [setup] done. Start everything with:  start.bat
exit /b 0

:setup_no_npm
echo [ERROR] npm not found - install Node.js first.
exit /b 1


:run_head
REM Normally the head runs on the PUBLIC server via pm2 (see ecosystem.config.js).
REM This mode exists so you can exercise the whole chain on one machine.
if not "%TUNNEL_TOKEN%"=="" goto :head_ok
echo [ERROR] TUNNEL_TOKEN is not set in this shell.
echo         The head refuses every client without it.
echo.
echo         Generate one:
echo             "%PY%" -c "import secrets; print(secrets.token_urlsafe(32))"
echo         Then:
echo             set TUNNEL_TOKEN=your-token-here
exit /b 1
:head_ok
echo ===========================================================
echo  Tunnel head (LOCAL TEST MODE)
echo ===========================================================
echo  Public port  9000  /tunnel-agent   token-authenticated
echo  Local  port  9001  forwarding      KEEP OFF THE INTERNET
echo  Status       http://localhost:9001/_tunnel/status
echo ===========================================================
echo.
"%PY%" tunnel_server\server.py
goto :done


:run_tunnel
echo ===========================================================
echo  Tunnel client only (debug)
echo ===========================================================
echo  Requires TUNNEL_SERVER_URL and TUNNEL_TOKEN in .env.
echo  Expects the proxy to already be running on port 2000.
echo ===========================================================
echo.
"%PY%" -m proxy_server.core.tunnel_client
goto :done


:no_python
echo [ERROR] Python not found.
echo         Expected a venv at kingdom\Scripts\python.exe, or python on PATH.
echo.
echo         To create the venv:
echo             python -m venv kingdom
echo             kingdom\Scripts\pip install -r requirements.txt
exit /b 1


:usage
call :print_usage
exit /b 0


:print_usage
echo BrightysKingdom launcher
echo.
echo   start.bat            Start the proxy + frontend + tunnel client
echo   start.bat proxy      Start the proxy only (no frontend)
echo   start.bat setup      Install frontend dependencies (npm install)
echo   start.bat head       Start the tunnel head locally (testing only)
echo   start.bat tunnel     Start only the tunnel client (debugging)
echo   start.bat help       Show this help
echo.
echo On the public server use run.sh or pm2 instead - see tunnel_server\README.md
goto :eof


:done
endlocal
