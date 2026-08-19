/**
 * PM2 process definitions.
 *
 *   Public server (the usual case — run the tunnel head):
 *       pm2 start ecosystem.config.js --only tunnel-head
 *
 *   Local machine (proxy + tunnel client, if you want it supervised):
 *       pm2 start ecosystem.config.js --only kingdom
 *
 *   Survive reboots:
 *       pm2 save
 *       pm2 startup          # then run the command it prints, as root
 *
 *   Watch it:
 *       pm2 logs tunnel-head
 *       pm2 monit
 *
 * SECRETS
 * -------
 * Nothing sensitive lives in this file — it is committed. Both apps read .env
 * themselves via python-dotenv, so put TUNNEL_TOKEN there. The env blocks
 * below only pass through values already exported in your shell, and act as
 * documentation of what each app reads.
 *
 * LOG ROTATION
 * ------------
 * PM2 does not rotate by default and these logs are chatty:
 *       pm2 install pm2-logrotate
 *       pm2 set pm2-logrotate:max_size 50M
 *       pm2 set pm2-logrotate:retain 7
 */

const isWindows = process.platform === 'win32';

// PM2 resolves a relative interpreter against `cwd`. Override with e.g.
//   PYTHON=/usr/bin/python3.12 pm2 start ecosystem.config.js --only tunnel-head
const PYTHON =
  process.env.PYTHON ||
  (isWindows ? 'kingdom/Scripts/python.exe' : './venv/bin/python');

if (!process.env.TUNNEL_TOKEN) {
  // Not fatal: .env may supply it, and `--only kingdom` does not need it.
  console.warn(
    '[ecosystem] TUNNEL_TOKEN is not exported in this shell — relying on .env. ' +
      'If the head refuses every client, that is why.'
  );
}

/** Settings every app shares. */
const common = {
  cwd: __dirname,
  interpreter: PYTHON,
  // Python cannot use PM2's cluster mode; fork is the only valid option.
  exec_mode: 'fork',
  instances: 1,
  autorestart: true,
  // A process that dies inside 10s counts as a failed start rather than a
  // successful run, so a crash-loop trips max_restarts instead of retrying
  // for ever.
  min_uptime: '10s',
  max_restarts: 10,
  restart_delay: 4000,
  max_memory_restart: '500M',
  time: true,
  merge_logs: true,
  env: {
    // Python buffers stdout when it is not a tty, so without this `pm2 logs`
    // shows nothing until the buffer fills and the process looks hung.
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: __dirname,
  },
};

module.exports = {
  apps: [
    {
      ...common,
      name: 'tunnel-head',
      script: 'tunnel_server/server.py',
      out_file: 'logs/tunnel-head.out.log',
      error_file: 'logs/tunnel-head.err.log',
      env: {
        ...common.env,
        TUNNEL_TOKEN: process.env.TUNNEL_TOKEN || '',
        // Faces the internet — this is the token-authenticated agent endpoint.
        TUNNEL_PUBLIC_PORT: process.env.TUNNEL_PUBLIC_PORT || '9000',
        TUNNEL_PUBLIC_BIND: process.env.TUNNEL_PUBLIC_BIND || '0.0.0.0',
        // The forwarding surface. Unauthenticated by design, so firewall this
        // port to your container network. 0.0.0.0 is required for Docker
        // containers to reach it via host.docker.internal.
        TUNNEL_LOCAL_PORT: process.env.TUNNEL_LOCAL_PORT || '9001',
        TUNNEL_LOCAL_BIND: process.env.TUNNEL_LOCAL_BIND || '0.0.0.0',
        // A cold 14B model load measured ~20s to first token; keep headroom.
        TUNNEL_FIRST_BYTE_TIMEOUT: process.env.TUNNEL_FIRST_BYTE_TIMEOUT || '120',
        TUNNEL_IDLE_TIMEOUT: process.env.TUNNEL_IDLE_TIMEOUT || '300',
        TUNNEL_WS_OPEN_TIMEOUT: process.env.TUNNEL_WS_OPEN_TIMEOUT || '30',
      },
    },
    {
      ...common,
      name: 'kingdom',
      script: 'run_gevent.py',
      out_file: 'logs/kingdom.out.log',
      error_file: 'logs/kingdom.err.log',
      env: {
        ...common.env,
        // Leave TUNNEL_SERVER_URL empty for a local-only run; the tunnel
        // client then does not start at all.
        TUNNEL_SERVER_URL: process.env.TUNNEL_SERVER_URL || '',
        TUNNEL_TOKEN: process.env.TUNNEL_TOKEN || '',
        TUNNEL_CLIENT_NAME: process.env.TUNNEL_CLIENT_NAME || '',
        TUNNEL_LOCAL_TARGET:
          process.env.TUNNEL_LOCAL_TARGET || 'http://127.0.0.1:2000',
      },
    },
  ],
};
