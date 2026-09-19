// Node 22+ defines its own globalThis.localStorage: a placeholder that reads
// as undefined unless node runs with --localstorage-file. Vitest 3's jsdom
// environment only replaces globals that appear in its list of DOM keys,
// and localStorage joined that list in a later major (which needs Vite 6+).
// Until then, point the bare `localStorage` the app uses at jsdom's, so a
// test sees the same storage the page does. Vitest aliases both `window`
// and `document.defaultView` to the Node global, so the real jsdom window
// has to come from the JSDOM instance it leaves at `globalThis.jsdom`.
type Storages = Pick<Window, 'localStorage' | 'sessionStorage'>;

function isStorage(value: unknown): value is Storage {
  return typeof value === 'object' && value !== null
    && 'getItem' in value && typeof value.getItem === 'function'
    && 'setItem' in value && typeof value.setItem === 'function'
    && 'removeItem' in value && typeof value.removeItem === 'function'
    && 'clear' in value && typeof value.clear === 'function';
}

function jsdomStorages(): Storages | null {
  const host: object = globalThis;
  if (!('jsdom' in host)) return null;
  const dom = host.jsdom;
  if (typeof dom !== 'object' || dom === null || !('window' in dom)) return null;
  const win = dom.window;
  if (typeof win !== 'object' || win === null) return null;
  if (!('localStorage' in win) || !('sessionStorage' in win)) return null;
  const { localStorage, sessionStorage } = win;
  return isStorage(localStorage) && isStorage(sessionStorage) ? { localStorage, sessionStorage } : null;
}

const storages = jsdomStorages();
if (storages) {
  for (const name of ['localStorage', 'sessionStorage'] as const) {
    Object.defineProperty(globalThis, name, {
      get: () => storages[name],
      configurable: true,
    });
  }
}
