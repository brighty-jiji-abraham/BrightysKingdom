/**
 * PM2 definition for a STANDALONE tunnel head deploy.
 *
 * Use this one when tunnel_server/ has been copied to the public server on
 * its own. If you are running from the full BrightysKingdom checkout, the
 * ecosystem.config.js at the repo root defines both this and the Kingdom.
 * Both name the app `tunnel-head`, so pm2 commands read the same either way.
 *
 *     ./run.sh setup                  # venv + dependencies
 *     cp .env.example .env            # then set TUNNEL_TOKEN
 *     pm2 start ecosystem.config.js
 *     pm2 logs tunnel-head
 *
 *     pm2 save
 *     pm2 startup                     # run the command it prints, as root
 *
 * SECRETS
 * -------
 * Nothing sensitive belongs in this file — it is committed. server.py reads
 * .env itself via python-dotenv, so TUNNEL_TOKEN lives there. The env block
 * below only forwards values already exported in your shell and documents
 * what the app reads.
 *
 * LOG ROTATION
 * ------------
 * PM2 does not rotate by default and this log is chatty:
 *     pm2 install pm2-logrotate
 *     pm2 set pm2-logrotate:max_size 50M
 *     pm2 set pm2-logrotate:retain 7
 */

const path = require('path');
const fs = require('fs');

// Prefer a venv beside server.py, then the parent repo's venv, then system
// python3. Override with:  PYTHON=/usr/bin/python3.12 pm2 start ecosystem.config.js
const CANDIDATES = [
  process.env.PYTHON,
  path.join(__dirname, 'venv', 'bin', 'python'),
  path.join(__dirname, 'venv', 'Scripts', 'python.exe'),
  path.join(__dirname, '..', 'kingdom', 'bin', 'python'),
  path.join(__dirname, '..', 'kingdom', 'Scripts', 'python.exe'),
];

const PYTHON =
  CANDIDATES.find((p) => p && fs.existsSync(p)) || 'python3';

if (PYTHON === 'python3') {
  console.warn(
    '[ecosystem] No venv found — falling back to system python3. ' +
      'If it lacks gevent, run ./run.sh setup first.'
  );
}

if (!process.env.TUNNEL_TOKEN && !fs.existsSync(path.join(__dirname, '.env'))) {
  console.warn(
    '[ecosystem] No TUNNEL_TOKEN exported and no .env present — ' +
      'the head will refuse every client. Copy .env.example to .env.'
  );
}

module.exports = {
  apps: [
    {
      name: 'tunnel-head',
      script: 'server.py',
      cwd: __dirname,
      interpreter: PYTHON,
      // Python cannot use PM2's cluster mode; fork is the only valid option.
      exec_mode: 'fork',
      instances: 1,
      autorestart: true,
      // A process that dies inside 10s counts as a failed start rather than a
      // successful run, so a crash-loop trips max_restarts instead of
      // retrying for ever.
      min_uptime: '10s',
      max_restarts: 10,
      restart_delay: 4000,
      max_memory_restart: '500M',
      time: true,
      merge_logs: true,
      out_file: 'logs/tunnel-head.out.log',
      error_file: 'logs/tunnel-head.err.log',
      env: {
        // Python buffers stdout when it is not a tty, so without this
        // `pm2 logs` shows nothing until the buffer fills and the process
        // looks hung.
        PYTHONUNBUFFERED: '1',
        TUNNEL_TOKEN: process.env.TUNNEL_TOKEN || '',
        // Faces the internet — token-authenticated agent endpoint.
        TUNNEL_PUBLIC_PORT: process.env.TUNNEL_PUBLIC_PORT || '9000',
        TUNNEL_PUBLIC_BIND: process.env.TUNNEL_PUBLIC_BIND || '0.0.0.0',
        // Unauthenticated forwarding surface — firewall this to your
        // container network. 0.0.0.0 is needed for Docker containers to
        // reach it via host.docker.internal.
        TUNNEL_LOCAL_PORT: process.env.TUNNEL_LOCAL_PORT || '9001',
        TUNNEL_LOCAL_BIND: process.env.TUNNEL_LOCAL_BIND || '0.0.0.0',
        // A cold 14B model load measured ~20s to first token; keep headroom.
        TUNNEL_FIRST_BYTE_TIMEOUT: process.env.TUNNEL_FIRST_BYTE_TIMEOUT || '120',
        TUNNEL_IDLE_TIMEOUT: process.env.TUNNEL_IDLE_TIMEOUT || '300',
        TUNNEL_WS_OPEN_TIMEOUT: process.env.TUNNEL_WS_OPEN_TIMEOUT || '30',
      },
    },
  ],
};
