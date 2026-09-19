// @vitest-environment jsdom
// @vitest-environment-options { "url": "http://192.168.2.29:7292/" }
//
// These tests render the real App and only fake the socket, so the input,
// its state, the localStorage round trip, and the terminal rendering are
// all exercised end to end. The page URL is a LAN address, the way a phone
// loads it, so loopback links can be seen to be rewritten.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import type { WindowState } from './types';

// A socket that records what the app emits, can deliver server events, and
// never connects — a phone whose Mac is unreachable — since the draft must
// be kept regardless.
const fakes = vi.hoisted(() => {
  type Handler = (payload: unknown) => void;
  class FakeSocket {
    readonly emitted: Array<{ event: string; payload: unknown }> = [];
    private readonly handlers = new Map<string, Handler[]>();
    get volatile() { return this; }
    on(event: string, handler: Handler) {
      this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
      return this;
    }
    emit(event: string, ...args: unknown[]) {
      this.emitted.push({ event, payload: args[0] });
      return this;
    }
    disconnect() { return this; }
    /** Deliver a server event to the app, as socket.io would. */
    receive(event: string, payload: unknown) {
      for (const handler of this.handlers.get(event) ?? []) handler(payload);
    }
  }
  const sockets: FakeSocket[] = [];
  return { FakeSocket, sockets };
});

vi.mock('socket.io-client', () => ({
  io: () => {
    const socket = new fakes.FakeSocket();
    fakes.sockets.push(socket);
    return socket;
  },
}));

// jsdom has no matchMedia; the app only asks it about orientation.
const portrait = (query: string): MediaQueryList => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false,
});

const commandBox = () => screen.getByRole<HTMLInputElement>('textbox', { name: 'Command' });
const latestSocket = () => fakes.sockets[fakes.sockets.length - 1];

// Unmounting and mounting again is what a reload looks like to the app:
// every piece of React state is gone and only localStorage carries over.
function reloadPage() {
  cleanup();
  render(<App />);
}

// One window, one tab, one pane — enough for the app to pick a session.
const state: WindowState[] = [{
  id: 'w1',
  isFront: true,
  tabs: [{ index: 1, id: 'w1-1', isSelected: true, currentSessionId: 's1', sessions: [{ id: 's1', name: 'shell' }] }],
}];

type Run = { t: string; u?: string };
const frame = (lines: Run[][]) => ({
  sessionId: 's1', lines, fg: '#ffffff', bg: '#000000',
  firstLine: 0, availableFirstLine: 0, terminalEnd: lines.length, isLatest: true,
});

function showPane(lines: Run[][]) {
  render(<App />);
  act(() => latestSocket().receive('state', state));
  act(() => latestSocket().receive('content', frame(lines)));
  const pane = document.querySelector('pre.terminal-output');
  if (!pane) throw new Error('the terminal pane did not render');
  return pane;
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  fakes.sockets.length = 0;
  window.matchMedia = portrait;
  // A connect renews the auth cookie over the network; keep that off the wire.
  vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('the unsent command', () => {
  it('is back in the box after the page reloads', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(commandBox(), 'git rebase -i origin/mai');

    reloadPage();

    expect(commandBox().value).toBe('git rebase -i origin/mai');
  });

  it('keeps up with every keystroke, not just a blur or submit', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(commandBox(), 'ls');
    expect(localStorage.getItem('remote-iterm-command-draft')).toBe('ls');
    await user.type(commandBox(), ' -la');
    expect(localStorage.getItem('remote-iterm-command-draft')).toBe('ls -la');
  });

  it('is forgotten once it has been sent', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(commandBox(), 'ls -la{Enter}');
    expect(latestSocket().emitted).toContainEqual({
      event: 'execute',
      payload: expect.objectContaining({ command: 'ls -la' }),
    });
    expect(commandBox().value).toBe('');

    reloadPage();

    expect(commandBox().value).toBe('');
  });

  it('is forgotten when the user clears the box themselves', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(commandBox(), 'abc');
    await user.clear(commandBox());

    reloadPage();

    expect(commandBox().value).toBe('');
  });
});

describe('links in terminal output', () => {
  it('renders a linked run as an anchor that opens in a new tab', () => {
    showPane([[{ t: 'see ' }, { t: 'https://example.com/x', u: 'https://example.com/x' }, { t: ' now' }]]);
    const link = screen.getByRole<HTMLAnchorElement>('link', { name: 'https://example.com/x' });
    expect(link.href).toBe('https://example.com/x');
    expect(link.target).toBe('_blank');
    expect(link.rel).toContain('noopener');
  });

  it('points a loopback address at the Mac this page was loaded from', () => {
    showPane([[{ t: 'http://localhost:3000/app', u: 'http://localhost:3000/app' }]]);
    const link = screen.getByRole<HTMLAnchorElement>('link', { name: 'http://localhost:3000/app' });
    expect(link.href).toBe('http://192.168.2.29:3000/app');
  });

  it('leaves the terminal text itself selectable', () => {
    const pane = showPane([[{ t: 'plain text' }]]);
    expect(pane.classList.contains('terminal-output')).toBe(true);
    let ancestor: HTMLElement | null = pane.parentElement;
    while (ancestor) {
      expect(ancestor.classList.contains('select-none')).toBe(false);
      ancestor = ancestor.parentElement;
    }
  });
});

describe('live updates while terminal text is selected', () => {
  const selectAllOf = (element: Element) => {
    const selection = document.getSelection();
    if (!selection) throw new Error('jsdom has no selection');
    const range = document.createRange();
    range.selectNodeContents(element);
    selection.removeAllRanges();
    selection.addRange(range);
    act(() => { document.dispatchEvent(new Event('selectionchange')); });
    return selection;
  };

  it('are held until the selection is gone, then applied', () => {
    const pane = showPane([[{ t: 'first frame' }]]);
    const selection = selectAllOf(pane);
    expect(screen.getByText('PAUSED')).toBeTruthy();

    act(() => latestSocket().receive('content', frame([[{ t: 'second frame' }]])));
    expect(pane.textContent).toContain('first frame');

    selection.removeAllRanges();
    act(() => { document.dispatchEvent(new Event('selectionchange')); });
    expect(pane.textContent).toContain('second frame');
    expect(screen.queryByText('PAUSED')).toBeNull();
  });

  it('do not apply to a pane that switched sessions in the meantime', () => {
    const pane = showPane([[{ t: 'first frame' }]]);
    const selection = selectAllOf(pane);
    act(() => latestSocket().receive('content', frame([[{ t: 'late frame for s1' }]])));

    // The Mac opens a new tab whose pane the app follows.
    const switched: WindowState[] = [{
      id: 'w1',
      isFront: true,
      tabs: [
        { index: 1, id: 'w1-1', isSelected: false, currentSessionId: 's1', sessions: [{ id: 's1', name: 'shell' }] },
        { index: 2, id: 'w1-2', isSelected: true, currentSessionId: 's2', sessions: [{ id: 's2', name: 'other' }] },
      ],
    }];
    act(() => latestSocket().receive('state', switched));
    selection.removeAllRanges();
    act(() => { document.dispatchEvent(new Event('selectionchange')); });

    expect(document.body.textContent).not.toContain('late frame for s1');
  });
});


describe('connection diagnostics', () => {
  const hello = () => latestSocket().emitted.filter((e) => e.event === 'hello').map((e) => e.payload);

  const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;

  it('introduces each connection to the server with the page identity', () => {
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    expect(hello()).toEqual([expect.objectContaining({
      page: expect.any(String), connect: expect.any(Number), navigation: expect.any(String), visibility: 'visible', lastDisconnect: null,
    })]);
  });

  it('reports how the previous connection ended when the same page reconnects', () => {
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('disconnect', 'transport close'));
    act(() => latestSocket().receive('connect', undefined));
    const [first, second] = hello();
    if (!isRecord(first) || !isRecord(second)) throw new Error('expected two hello payloads');
    // Same page, next connection: the counter moves on and the page id does not.
    expect(second.page).toBe(first.page);
    expect(second.connect).toBe(Number(first.connect) + 1);
    expect(second.lastDisconnect).toEqual(expect.objectContaining({ reason: 'transport close', visibility: 'visible' }));
  });

  it('carries the last disconnect across a reload of the same tab', () => {
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('disconnect', 'ping timeout'));

    reloadPage();
    act(() => latestSocket().receive('connect', undefined));

    expect(hello()).toEqual([expect.objectContaining({
      lastDisconnect: expect.objectContaining({ reason: 'ping timeout' }),
    })]);
  });
});
