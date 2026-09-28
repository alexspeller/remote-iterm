// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createServer, type Server } from 'node:http';
import { RESTART_PATH, createRestartHandler, keyFromCookieHeader, type RestartHandle } from './restartEndpoint';

const KEY = 'the-shared-key';

// A real HTTP server with the handler mounted the way connect mounts it:
// the mount path is stripped from req.url before the handler sees it.
let server: Server;
let base: string;
let restarts: Array<() => void>;
let key: string | null;

beforeEach(async () => {
  restarts = [];
  key = KEY;
  const handler = createRestartHandler({
    readKey: () => key,
    startRestart: (): RestartHandle => {
      let finish: () => void = () => {};
      restarts.push(() => finish());
      return { onExit: (done) => { finish = done; } };
    },
  });
  server = createServer((req, res) => {
    if (req.url?.startsWith(RESTART_PATH)) {
      req.url = req.url.slice(RESTART_PATH.length) || '/';
      handler(req, res, () => { res.statusCode = 404; res.end(); });
      return;
    }
    res.statusCode = 404;
    res.end();
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (address === null || typeof address === 'string') throw new Error('the test server has no port');
  base = `http://127.0.0.1:${address.port}`;
});

afterEach(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

function restart(init: { body?: unknown; cookie?: string; origin?: string; method?: string } = {}) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (init.cookie) headers.Cookie = init.cookie;
  if (init.origin) headers.Origin = init.origin;
  return fetch(`${base}${RESTART_PATH}`, {
    method: init.method ?? 'POST',
    headers,
    body: init.method === 'GET' ? undefined : JSON.stringify(init.body ?? {}),
  });
}

describe('the restart endpoint', () => {
  it('restarts the server for the right key', async () => {
    const response = await restart({ body: { key: KEY }, origin: base });
    expect(response.status).toBe(202);
    expect(restarts).toHaveLength(1);
  });

  it('accepts the auth cookie in place of the key', async () => {
    const response = await restart({ cookie: `other=1; remote_iterm_key=${KEY}` });
    expect(response.status).toBe(202);
    expect(restarts).toHaveLength(1);
  });

  it('accepts a valid cookie even beside a stale key', async () => {
    const response = await restart({ body: { key: 'stale' }, cookie: `remote_iterm_key=${KEY}` });
    expect(response.status).toBe(202);
  });

  it('refuses a wrong key', async () => {
    const response = await restart({ body: { key: 'guess' } });
    expect(response.status).toBe(401);
    expect(restarts).toHaveLength(0);
  });

  it('refuses another site even when the browser sends the right cookie', async () => {
    const response = await restart({ cookie: `remote_iterm_key=${KEY}`, origin: 'http://evil.example' });
    expect(response.status).toBe(403);
    expect(restarts).toHaveLength(0);
  });

  it('refuses the same host on another port', async () => {
    const response = await restart({ body: { key: KEY }, origin: 'http://127.0.0.1:1' });
    expect(response.status).toBe(403);
  });

  it('only answers POST', async () => {
    const response = await restart({ method: 'GET' });
    expect(response.status).toBe(405);
    expect(restarts).toHaveLength(0);
  });

  it('runs one restart at a time', async () => {
    expect((await restart({ body: { key: KEY } })).status).toBe(202);
    expect((await restart({ body: { key: KEY } })).status).toBe(409);
    expect(restarts).toHaveLength(1);

    restarts[0]();  // the first restart finishes
    expect((await restart({ body: { key: KEY } })).status).toBe(202);
    expect(restarts).toHaveLength(2);
  });

  it('says so when the Mac has no key to check against', async () => {
    key = null;
    const response = await restart({ body: { key: KEY } });
    expect(response.status).toBe(500);
    expect(restarts).toHaveLength(0);
  });
});

describe('reading the auth cookie', () => {
  it('finds it among others and tolerates quoting', () => {
    expect(keyFromCookieHeader('a=1; remote_iterm_key="abc"; b=2')).toBe('abc');
    expect(keyFromCookieHeader('a=1')).toBeNull();
    expect(keyFromCookieHeader(undefined)).toBeNull();
  });
});
