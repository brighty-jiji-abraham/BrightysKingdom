@echo off
REM ===========================================================================
REM  Tunnel head launcher (Windows)
REM
REM    start.bat            Start the head
REM    start.bat setup      Create venv + install dependencies
REM    start.bat token      Generate a TUNNEL_TOKEN
REM    start.bat help       Show this help
REM
REM  The head normally runs on the public Linux server via run.sh or pm2.
REM  This exists for testing the whole chain on one Windows box, and for the
REM  case where the public host is Windows.
REM ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=start"

if /i "%MODE%"=="help"   goto :usage
if /i "%MODE%"=="-h"     goto :usage
if /i "%MODE%"=="--help" goto :usage

REM --- locate the interpreter ------------------------------------------------
REM A venv inside this folder wins, so a standalone deploy is self-describing.
REM The parent kingdom venv is the fallback when running from the full repo.
set "PY=%~dp0venv\Scripts\python.exe"
if exist "%PY%" goto :got_python
set "PY=%~dp0..\kingdom\Scripts\python.exe"
if exist "%PY%" goto :got_python
set "PY=python"
where python >nul 2>&1 || goto :no_python
echo [warn] no venv found - using system python
:got_python

if /i "%MODE%"=="setup" goto :run_setup
if /i "%MODE%"=="token" goto :run_token

if not exist "logs" mkdir "logs"

if exist ".env" goto :got_env
echo [warn] no .env file found - copy .env.example to .env and edit it
:got_env

if /i "%MODE%"=="start" goto :run_start

echo [ERROR] Unknown mode "%MODE%"
echo.
call :print_usage
exit /b 1


:run_setup
echo [setup] using %PY%
if exist "%~dp0venv\Scripts\python.exe" goto :setup_install
echo [setup] creating venv at .\venv
"%PY%" -m venv venv || exit /b 1
set "PY=%~dp0venv\Scripts\python.exe"
:setup_install
"%PY%" -m pip install --upgrade pip || exit /b 1
"%PY%" -m pip install -r requirements.txt || exit /b 1
echo.
echo [setup] done. Next:
echo     copy .env.example .env
echo     start.bat token          (paste the value into .env)
echo     start.bat
exit /b 0


:run_token
"%PY%" -c "import secrets; print(secrets.token_urlsafe(32))"
exit /b 0


:run_start
REM Python buffers stdout when it is not a tty, so unbuffer for pm2/nssm.
set "PYTHONUNBUFFERED=1"

REM Resolve the real ports rather than printing the defaults and claiming they
REM came from .env - a banner that names a port the head is not listening on
REM is worse than no banner.
set "PUB_PORT=9000"
set "LOC_PORT=9001"
call :read_env ".env" TUNNEL_PUBLIC_PORT PUB_PORT
call :read_env ".env" TUNNEL_LOCAL_PORT LOC_PORT

REM Same resolution as run.sh: environment first, then .env. server.py checks
REM this too, but failing here says what to do about it.
if not "%TUNNEL_TOKEN%"=="" goto :start_ok
call :read_env ".env" TUNNEL_TOKEN TUNNEL_TOKEN
if not "%TUNNEL_TOKEN%"=="" goto :start_ok
echo [ERROR] TUNNEL_TOKEN is not set - checked the environment and .env.
echo         Without it the head refuses every client.
echo.
echo             start.bat token       generate one, then put it in .env
exit /b 1
:start_ok

echo ===========================================================
echo  Tunnel head
echo ===========================================================
echo  Public port  %PUB_PORT%  /tunnel-agent   token-authenticated
echo  Local  port  %LOC_PORT%  forwarding      KEEP OFF THE INTERNET
echo  Status       http://127.0.0.1:%LOC_PORT%/_tunnel/status
echo.
echo  Firewall the local port so only your own containers reach it.
echo.
echo  Ctrl+C to stop.
echo ===========================================================
echo.
"%PY%" server.py
goto :done


:read_env
REM read_env <file> <key> <target-var> - last assignment wins, quotes stripped.
REM Looping every line rather than stopping at the first match is what makes a
REM duplicated key resolve the same way dotenv and the shell resolve it.
if not exist %1 goto :eof
REM %%~B already strips surrounding quotes, so no separate unquoting step is
REM needed - the `call set` that used to do it produced malformed syntax when
REM the value was empty, and only the missing-token path ever hit that.
REM An empty assignment in .env means "not set", so the caller keeps its default.
for /f "usebackq eol=# tokens=1,* delims==" %%A in (%1) do (
  if /i "%%~A"=="%~2" if not "%%~B"=="" set "%~3=%%~B"
)
goto :eof


:no_python
echo [ERROR] Python not found.
echo         Expected a venv at venv\Scripts\python.exe, the parent
echo         kingdom venv, or python on PATH.
echo.
echo         To set up:  start.bat setup
exit /b 1


:usage
call :print_usage
exit /b 0


:print_usage
echo Tunnel head launcher
echo.
echo   start.bat            Start the head
echo   start.bat setup      Create venv + install dependencies
echo   start.bat token      Generate a TUNNEL_TOKEN
echo   start.bat help       Show this help
echo.
echo On a Linux server use run.sh or pm2 instead.
goto :eof


:done
endlocal
