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

/**
 * Pass through only variables that actually have a value.
 *
 * `FOO: process.env.FOO || ''` sets FOO to an empty string in the child, and
 * python-dotenv (override=False) then treats it as already set and skips the
 * value in .env. Omitting the key is what lets .env answer for it.
 */
function passThrough(names) {
  const out = {};
  for (const name of names) {
    const value = process.env[name];
    if (value !== undefined && value !== '') out[name] = value;
  }
  return out;
}

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
        // Omitted rather than defaulted to '' so .env can answer - see
        // passThrough above.
        ...passThrough([
          'TUNNEL_TOKEN',
          'TUNNEL_PUBLIC_PORT',
          'TUNNEL_PUBLIC_BIND',
          'TUNNEL_LOCAL_PORT',
          'TUNNEL_LOCAL_BIND',
          'TUNNEL_FIRST_BYTE_TIMEOUT',
          'TUNNEL_IDLE_TIMEOUT',
          'TUNNEL_WS_OPEN_TIMEOUT',
        ]),
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
        // Tunnel settings live in MongoDB now; .env only seeds them and acts
        // as the fallback. Nothing is defaulted to '' here, because an empty
        // value would shadow both.
        ...passThrough([
          'TUNNEL_SERVER_URL',
          'TUNNEL_TOKEN',
          'TUNNEL_CLIENT_NAME',
          'TUNNEL_LOCAL_TARGET',
          'PROXY_MONGO_URL',
          'PROXY_MONGO_DB',
        ]),
      },
    },
  ],
};
