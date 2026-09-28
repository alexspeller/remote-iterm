// POST /api/restart-server: the phone's "restart the server" button.
//
// Served by this process — the one serving the web client on 7292 — rather
// than by the Python server it restarts, so the button still works in the
// cases it exists for: a Python server whose event loop is wedged, or which
// has died and not come back. It restarts only that server (see
// `iterm-server restart-server`); this process and the snapshotter keep
// running, so the page stays loadable throughout.
//
// Authenticated exactly like the Python server: the shared key in the JSON
// body, or the HttpOnly `remote_iterm_key` cookie that server issues (cookies
// are scoped by host, not port, so the page on 7292 sends it here too). A
// request from another origin is refused outright, since the cookie would
// otherwise let any web page restart the server.
import { spawn } from 'node:child_process';
import { timingSafeEqual } from 'node:crypto';
import { closeSync, openSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import type { IncomingMessage, ServerResponse } from 'node:http';

export const RESTART_PATH = '/api/restart-server';
const COOKIE_NAME = 'remote_iterm_key';
const MAX_BODY_BYTES = 4096;

export interface RestartHandle {
  /** Called once the restart has finished, successfully or not. */
  onExit(done: () => void): void;
}

export interface RestartOptions {
  /** The shared access key, or null if none can be read. */
  readKey: () => string | null;
  /** Starts the restart. Only one runs at a time. */
  startRestart: () => RestartHandle;
}

type Next = (error?: unknown) => void;

function respond(res: ServerResponse, status: number, body: Record<string, unknown>) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(body));
}

// Same-origin only: the page and this endpoint are the same host and port.
export function isSameOrigin(origin: string | undefined, host: string | undefined): boolean {
  if (origin === undefined) return true; // not a browser cross-origin request
  if (!host) return false;
  try {
    const parsed = new URL(origin);
    return (parsed.protocol === 'http:' || parsed.protocol === 'https:')
      && parsed.host.toLowerCase() === host.toLowerCase();
  } catch {
    return false;
  }
}

export function keyFromCookieHeader(header: string | undefined): string | null {
  if (!header) return null;
  for (const part of header.split(';')) {
    const eq = part.indexOf('=');
    if (eq === -1) continue;
    if (part.slice(0, eq).trim() !== COOKIE_NAME) continue;
    const value = part.slice(eq + 1).trim().replace(/^"(.*)"$/, '$1');
    try {
      return decodeURIComponent(value) || null;
    } catch {
      return value || null;
    }
  }
  return null;
}

function keyFromBody(raw: string): string | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null || !('key' in parsed)) return null;
  const { key } = parsed;
  return typeof key === 'string' && key ? key : null;
}

export function keysMatch(supplied: string, expected: string): boolean {
  const a = Buffer.from(supplied);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

export function createRestartHandler(options: RestartOptions) {
  let restarting = false;
  return (req: IncomingMessage, res: ServerResponse, next: Next) => {
    // Mounted at RESTART_PATH by connect, which strips it from req.url.
    if (req.url && req.url !== '/' && !req.url.startsWith('/?')) {
      next();
      return;
    }
    if (req.method !== 'POST') {
      respond(res, 405, { error: 'POST only' });
      return;
    }
    if (!isSameOrigin(req.headers.origin, req.headers.host)) {
      respond(res, 403, { error: 'origin not allowed' });
      return;
    }
    readBody(req).then((raw) => {
      const expected = options.readKey();
      if (!expected) {
        respond(res, 500, { error: 'no access key on the Mac' });
        return;
      }
      // Either credential will do, as with the Python server: a stale key in
      // the page's storage must not shut out a valid cookie.
      const candidates = [keyFromBody(raw), keyFromCookieHeader(req.headers.cookie)];
      if (!candidates.some((key) => key !== null && keysMatch(key, expected))) {
        respond(res, 401, { error: 'invalid access key' });
        return;
      }
      if (restarting) {
        respond(res, 409, { error: 'a restart is already under way' });
        return;
      }
      restarting = true;
      try {
        options.startRestart().onExit(() => { restarting = false; });
      } catch (error) {
        restarting = false;
        respond(res, 500, { error: `could not start the restart: ${String(error)}` });
        return;
      }
      respond(res, 202, { ok: true });
    }, () => respond(res, 400, { error: 'unreadable request' }));
  };
}

// --- The real key file and launcher ---

export function accessKeyPath(): string {
  const override = process.env.REMOTE_ITERM_KEY_FILE;
  if (override) return override.replace(/^~(?=$|\/)/, homedir());
  return join(homedir(), 'Library', 'Application Support', 'remote-iterm', 'access-key');
}

export function readAccessKey(): string | null {
  try {
    return readFileSync(accessKeyPath(), 'utf8').trim() || null;
  } catch {
    return null;
  }
}

// Runs `iterm-server restart-server` detached, in its own session, so it
// survives anything that happens to this process meanwhile, with its output
// in the stack's log.
export function spawnLauncherRestart(repoDir: string): RestartHandle {
  const log = openSync(join(repoDir, '.iterm-server.log'), 'a');
  try {
    const child = spawn('/bin/bash', [join(repoDir, 'iterm-server'), 'restart-server'], {
      cwd: repoDir,
      detached: true,
      stdio: ['ignore', log, log],
      env: process.env,
    });
    child.unref();
    return {
      onExit(done) {
        child.once('exit', done);
        child.once('error', done);
      },
    };
  } finally {
    closeSync(log);
  }
}
