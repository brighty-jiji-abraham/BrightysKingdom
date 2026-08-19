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
echo ===========================================================
echo  Tunnel head
echo ===========================================================
echo  Public port  9000  /tunnel-agent   token-authenticated
echo  Local  port  9001  forwarding      KEEP OFF THE INTERNET
echo  Status       http://127.0.0.1:9001/_tunnel/status
echo.
echo  Ports come from .env - the numbers above are the defaults.
echo  Firewall the local port so only your own containers reach it.
echo.
echo  Ctrl+C to stop.
echo ===========================================================
echo.
"%PY%" server.py
goto :done


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
