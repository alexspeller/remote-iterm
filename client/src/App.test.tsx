// @vitest-environment jsdom
// @vitest-environment-options { "url": "http://192.168.2.29:7292/" }
//
// These tests render the real App and only fake the socket, so the input,
// its state, the localStorage round trip, and the terminal rendering are
// all exercised end to end. The page URL is a LAN address, the way a phone
// loads it, so loopback links can be seen to be rewritten.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import type { WindowState } from './types';

// A socket that records what the app emits, can deliver server events, and
// never connects — a phone whose Mac is unreachable — since the draft must
// be kept regardless.
const fakes = vi.hoisted(() => {
  type Handler = (payload: unknown) => void;
  type Ack = (response: unknown) => void;
  const isAck = (value: unknown): value is Ack => typeof value === 'function';
  class FakeSocket {
    readonly emitted: Array<{ event: string; payload: unknown }> = [];
    /** What socket.io-client holds back for the next connection. */
    sendBuffer: Array<{ data: unknown[] }> = [];
    private readonly acks: Array<{ event: string; ack: Ack }> = [];
    private readonly handlers = new Map<string, Handler[]>();
    get volatile() { return this; }
    on(event: string, handler: Handler) {
      this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
      return this;
    }
    emit(event: string, ...args: unknown[]) {
      this.emitted.push({ event, payload: args[0] });
      const last = args[args.length - 1];
      if (isAck(last)) this.acks.push({ event, ack: last });
      return this;
    }
    /** Answer the oldest unanswered emit of `event`, as the server's ack would. */
    acknowledge(event: string, response: unknown) {
      const index = this.acks.findIndex((a) => a.event === event);
      if (index === -1) throw new Error(`nothing unanswered for ${event}`);
      const [{ ack }] = this.acks.splice(index, 1);
      ack(response);
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
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('serverInfo', { instance: 'A' }));
    act(() => latestSocket().receive('state', state));
    await user.type(commandBox(), 'ls -la{Enter}');
    expect(latestSocket().emitted).toContainEqual({
      event: 'execute',
      payload: expect.objectContaining({ command: 'ls -la', sessionId: 's1' }),
    });
    expect(commandBox().value).toBe('');
    // The server takes it.
    act(() => latestSocket().acknowledge('execute', { ok: true, instance: 'A', delivered: 0 }));

    reloadPage();

    expect(commandBox().value).toBe('');
  });

  it('stays in the box when there is no pane to send it to yet', async () => {
    // Before the first state there is no pane: sending then used to reach
    // whatever pane the Mac had in front.
    const user = userEvent.setup();
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    await user.type(commandBox(), 'rm -rf build{Enter}');
    expect(latestSocket().emitted.filter((e) => e.event === 'execute')).toEqual([]);
    expect(commandBox().value).toBe('rm -rf build');
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

describe('smart typing', () => {
  const smartTypingToggle = (name: 'Turn on autocorrect' | 'Turn off autocorrect') =>
    screen.getByRole('button', { name });

  it('leaves the shell line uncapitalized even with autocorrect on', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(smartTypingToggle('Turn on autocorrect'));

    // Autocorrect and auto-capitalization are separate keyboard traits, and
    // only the first one is wanted: `Git status` is not a command.
    expect(commandBox().getAttribute('autocorrect')).toBe('on');
    expect(commandBox().getAttribute('autocapitalize')).toBe('none');
    expect(commandBox().getAttribute('spellcheck')).toBe('true');
  });

  it('keeps autocorrect off, and capitals off, when smart typing is off', () => {
    render(<App />);
    expect(commandBox().getAttribute('autocorrect')).toBe('off');
    expect(commandBox().getAttribute('autocapitalize')).toBe('none');
    expect(commandBox().getAttribute('spellcheck')).toBe('false');
  });

  it('stays on across a reload, still without capitals', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(smartTypingToggle('Turn on autocorrect'));

    reloadPage();

    expect(smartTypingToggle('Turn off autocorrect')).toBeTruthy();
    expect(commandBox().getAttribute('autocorrect')).toBe('on');
    expect(commandBox().getAttribute('autocapitalize')).toBe('none');
  });
});

describe('the quick action keys', () => {
  const row = (name: string) => screen.getByRole('group', { name });
  const keysIn = (name: string) =>
    within(row(name)).getAllByRole('button').map((b) => b.getAttribute('aria-label'));

  it('are laid out as two rows, split after the arrows', () => {
    render(<App />);
    expect(keysIn('Control and arrow keys')).toEqual([
      'ESC', '^C', '^D', '^Z', '^L', 'Up', 'Down', 'Left', 'Right',
    ]);
    expect(keysIn('Tab, return and clipboard')).toEqual([
      'TAB', 'Return', 'Newline without sending', 'Paste', 'Copy pane', 'Clear line', 'Typing log',
    ]);
  });

  it('never put a key behind a horizontal scroll', () => {
    render(<App />);
    for (const name of ['Control and arrow keys', 'Tab, return and clipboard']) {
      // Neither the row nor anything it sits in may scroll sideways: a key
      // off the edge of a strip is a key nobody knows is there.
      let element: HTMLElement | null = row(name);
      while (element) {
        expect(element.className).not.toContain('overflow-x-auto');
        element = element.parentElement;
      }
      // Each key divides the row instead of claiming a fixed width.
      for (const key of within(row(name)).getAllByRole('button')) {
        expect(key.className).toContain('flex-1');
        expect(key.className).not.toContain('flex-shrink-0');
      }
    }
  });
});

describe('closing a pane from the pane map', () => {
  // Two panes side by side in the one tab, which is what makes the pane
  // switcher — and its map — appear at all.
  const twoPanes: WindowState[] = [{
    id: 'w1',
    isFront: true,
    tabs: [{
      index: 1, id: 'w1-1', isSelected: true, currentSessionId: 's1', aspect: 1.6,
      sessions: [
        { id: 's1', name: 'left', rect: { x: 0, y: 0, w: 0.5, h: 1 } },
        { id: 's2', name: 'right', rect: { x: 0.5, y: 0, w: 0.5, h: 1 } },
      ],
    }],
  }];

  async function openMap() {
    const user = userEvent.setup();
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('state', twoPanes));
    await user.click(screen.getByRole('button', { name: /PANE/ }));
    return user;
  }

  const closeEmits = () => latestSocket().emitted.filter((e) => e.event === 'closePane');

  it('asks before it closes, naming the pane it means', async () => {
    const user = await openMap();
    await user.click(screen.getByRole('button', { name: 'CLOSE PANE 1' }));

    expect(screen.getByText('close pane 1 “left”?')).toBeTruthy();
    expect(closeEmits()).toEqual([]);
  });

  it('closes the selected pane once the confirmation is taken', async () => {
    const user = await openMap();
    await user.click(screen.getByRole('button', { name: 'CLOSE PANE 1' }));
    await user.click(screen.getByRole('button', { name: 'CLOSE' }));

    expect(closeEmits()).toEqual([{ event: 'closePane', payload: { sessionId: 's1' } }]);
  });

  it('closes nothing when the confirmation is declined', async () => {
    const user = await openMap();
    await user.click(screen.getByRole('button', { name: 'CLOSE PANE 1' }));
    await user.click(screen.getByRole('button', { name: 'KEEP' }));

    expect(closeEmits()).toEqual([]);
    // Back to the plain map, still open, still offering the close.
    expect(screen.getByRole('button', { name: 'CLOSE PANE 1' })).toBeTruthy();
  });

  it('backs out of the confirmation when a pane is tapped instead', async () => {
    const user = await openMap();
    await user.click(screen.getByRole('button', { name: 'CLOSE PANE 1' }));
    await user.click(screen.getByRole('button', { name: 'Pane 2 right' }));

    // The tap disarmed rather than switching panes, so nothing was closed and
    // the map still has pane 1 selected.
    expect(closeEmits()).toEqual([]);
    expect(screen.getByRole('button', { name: 'CLOSE PANE 1' })).toBeTruthy();
  });

  it('offers to close whichever pane is selected', async () => {
    const user = await openMap();
    // Tapping pane 2 selects it and dismisses the map; reopening it aims the
    // close at the new selection.
    await user.click(screen.getByRole('button', { name: 'Pane 2 right' }));
    await user.click(screen.getByRole('button', { name: /PANE/ }));
    await user.click(screen.getByRole('button', { name: 'CLOSE PANE 2' }));
    await user.click(screen.getByRole('button', { name: 'CLOSE' }));

    expect(closeEmits()).toEqual([{ event: 'closePane', payload: { sessionId: 's2' } }]);
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

describe('the reconnect banner', () => {
  it('stays hidden for a brief gap and appears only for a real outage', () => {
    vi.useFakeTimers();
    try {
      render(<App />);
      act(() => latestSocket().receive('connect', undefined));
      expect(screen.queryByText('RECONNECTING')).toBeNull();

      act(() => latestSocket().receive('disconnect', 'transport error'));
      act(() => { vi.advanceTimersByTime(1000); });
      expect(screen.queryByText('RECONNECTING')).toBeNull();

      act(() => latestSocket().receive('connect', undefined));
      act(() => { vi.advanceTimersByTime(5000); });
      expect(screen.queryByText('RECONNECTING')).toBeNull();

      act(() => latestSocket().receive('disconnect', 'transport error'));
      act(() => { vi.advanceTimersByTime(3000); });
      expect(screen.getByText('RECONNECTING')).toBeTruthy();

      act(() => latestSocket().receive('connect', undefined));
      expect(screen.queryByText('RECONNECTING')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('never covers the terminal or the command box', () => {
    vi.useFakeTimers();
    try {
      render(<App />);
      act(() => { vi.advanceTimersByTime(5000); });
      const banner = screen.getByRole('status');
      expect(banner.textContent).toContain('RECONNECTING');
      expect(banner.classList.contains('pointer-events-none')).toBe(true);
      expect(banner.classList.contains('inset-0')).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('typing over a connection that cannot be trusted', () => {
  const sent = (event: string) => latestSocket().emitted.filter((e) => e.event === event);
  const seqOf = (payload: unknown) => (isRecord(payload) ? payload.seq : undefined);
  const pageOf = (payload: unknown) => (isRecord(payload) ? payload.page : undefined);
  const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;

  function connected() {
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('serverInfo', { instance: 'A' }));
    act(() => latestSocket().receive('state', state));
  }
  const press = (name: string) => fireEvent.click(screen.getByRole('button', { name }));

  it('sends what was typed while disconnected as soon as it reconnects', () => {
    connected();
    act(() => latestSocket().receive('disconnect', 'transport error'));
    press('^C');
    expect(sent('sendKeys')).toEqual([]);
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('serverInfo', { instance: 'A' }));
    expect(sent('sendKeys').map((e) => e.payload)).toEqual([
      expect.objectContaining({ keys: '\x03', sessionId: 's1', seq: 1 }),
    ]);
  });

  it('sends again what a dead socket swallowed, and nothing the server had', () => {
    connected();
    press('ESC');
    act(() => latestSocket().acknowledge('sendKeys', { ok: true, instance: 'A', delivered: 0 }));
    press('^C');   // written into a socket that was already dead
    act(() => latestSocket().receive('disconnect', 'transport error'));
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('serverInfo', { instance: 'A' }));
    expect(sent('sendKeys').map((e) => seqOf(e.payload))).toEqual([1, 2, 2]);
  });

  it('says so, and puts the command back, when it cannot be delivered', async () => {
    const user = userEvent.setup();
    connected();
    await user.type(commandBox(), 'deploy{Enter}');
    const page = pageOf(sent('execute')[0].payload);
    act(() => latestSocket().acknowledge('execute', { ok: true, instance: 'A', delivered: 0 }));
    act(() => latestSocket().receive('inputDelivered', {
      page, upto: 1, failed: [{ seq: 1, reason: 'that pane has closed' }],
    }));
    expect(screen.getByText('Not delivered: deploy — that pane has closed')).toBeTruthy();
    expect(commandBox().value).toBe('deploy');
  });

  it('never carries input a dead socket was holding onto the next connection', () => {
    connected();
    press('^C');
    // socket.io-client had not noticed the socket was dead and kept the
    // packet to send first on the next connection, ahead of older input.
    latestSocket().sendBuffer.push({ data: ['sendKeys', { keys: '\x03' }] }, { data: ['focus', {}] });
    act(() => latestSocket().receive('disconnect', 'ping timeout'));
    expect(latestSocket().sendBuffer).toEqual([{ data: ['focus', {}] }]);
  });

  it('sends to a server too old to say which it is, after a moment', () => {
    vi.useFakeTimers();
    try {
      render(<App />);
      act(() => latestSocket().receive('connect', undefined));
      act(() => latestSocket().receive('state', state));
      press('^C');
      expect(sent('sendKeys')).toEqual([]);
      act(() => { vi.advanceTimersByTime(3000); });
      expect(sent('sendKeys')).toHaveLength(1);
      act(() => latestSocket().acknowledge('sendKeys', undefined));  // old servers ack with nothing
      act(() => { vi.advanceTimersByTime(10_000); });
      expect(sent('sendKeys')).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('asks rather than resends when a restarted server may not have delivered it', async () => {
    const user = userEvent.setup();
    connected();
    await user.type(commandBox(), 'git push{Enter}');
    act(() => latestSocket().acknowledge('execute', { ok: true, instance: 'A', delivered: 0 }));
    act(() => latestSocket().receive('disconnect', 'transport close'));
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('serverInfo', { instance: 'B' }));

    expect(sent('execute')).toHaveLength(1);
    expect(screen.getByText(/The server restarted before iTerm2 confirmed: git push/)).toBeTruthy();
    expect(commandBox().value).toBe('git push');
  });

  it('shows what is still waiting for iTerm2, until it is delivered', () => {
    vi.useFakeTimers();
    try {
      connected();
      press('^C');
      act(() => latestSocket().acknowledge('sendKeys', { ok: true, instance: 'A', delivered: 0 }));
      act(() => { vi.advanceTimersByTime(1500); });
      expect(screen.getByLabelText('1 waiting for iTerm2')).toBeTruthy();

      const page = pageOf(sent('sendKeys')[0].payload);
      act(() => latestSocket().receive('inputDelivered', { page, upto: 1, failed: [] }));
      expect(screen.queryByLabelText(/waiting for iTerm2/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps a command sent but unconfirmed when the page is reloaded', async () => {
    const user = userEvent.setup();
    connected();
    await user.type(commandBox(), 'make deploy{Enter}');
    expect(commandBox().value).toBe('');

    reloadPage();

    expect(commandBox().value).toBe('make deploy');
    expect(screen.getByText(/Not confirmed before the page reloaded: make deploy/)).toBeTruthy();
  });

  it('can always be sent, connected or not', async () => {
    const user = userEvent.setup();
    connected();
    act(() => latestSocket().receive('disconnect', 'transport error'));
    await user.type(commandBox(), 'ls');
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Send' }).disabled).toBe(false);
  });
});

describe('when iTerm2 stops answering', () => {
  it('says so, and for how long, until it answers again', () => {
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    const now = Date.now();
    act(() => latestSocket().receive('itermStatus', { ok: false, stalledSince: now - 34_000, now }));
    expect(screen.getByRole('status').textContent).toMatch(/ITERM2 NOT RESPONDING · 3[45]s/);

    act(() => latestSocket().receive('itermStatus', { ok: true, stalledSince: null, now }));
    expect(screen.queryByText(/ITERM2 NOT RESPONDING/)).toBeNull();
  });

  it('counts the stall in the phone\'s own clock, whatever the Mac\'s says', () => {
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    const macNow = Date.now() + 3_600_000;   // the Mac's clock is an hour out
    act(() => latestSocket().receive('itermStatus', { ok: false, stalledSince: macNow - 5_000, now: macNow }));
    expect(screen.getByRole('status').textContent).toMatch(/· [56]s/);
  });
});

describe('splitting a pane from the pane map', () => {
  const onePane: WindowState[] = [{
    id: 'w1', isFront: true,
    tabs: [{ index: 1, id: 'w1-1', isSelected: true, currentSessionId: 's1', aspect: 1.6,
      sessions: [{ id: 's1', name: 'shell', rect: { x: 0, y: 0, w: 1, h: 1 } }] }],
  }];
  const afterSplit: WindowState[] = [{
    id: 'w1', isFront: true,
    tabs: [{ index: 1, id: 'w1-1', isSelected: true, currentSessionId: 's1', aspect: 1.6,
      sessions: [
        { id: 's1', name: 'shell', rect: { x: 0, y: 0, w: 0.5, h: 1 } },
        { id: 's2', name: 'new', rect: { x: 0.5, y: 0, w: 0.5, h: 1 } },
      ] }],
  }];

  async function openMap() {
    const user = userEvent.setup();
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('state', onePane));
    await user.click(screen.getByRole('button', { name: /PANE/ }));
    return user;
  }
  const splits = () => latestSocket().emitted.filter((e) => e.event === 'splitPane').map((e) => e.payload);

  it('opens for a tab of one pane, which is where splitting starts', async () => {
    await openMap();
    expect(screen.getByRole('group', { name: 'Split pane 1' })).toBeTruthy();
  });

  it('splits the selected pane either way', async () => {
    const user = await openMap();
    await user.click(screen.getByRole('button', { name: 'Split pane 1 vertically' }));
    await user.click(screen.getByRole('button', { name: 'Split pane 1 horizontally' }));
    expect(splits()).toEqual([
      { sessionId: 's1', vertical: true },
      { sessionId: 's1', vertical: false },
    ]);
  });

  it('selects the new pane once it appears, and stays open to show it', async () => {
    const user = await openMap();
    await user.click(screen.getByRole('button', { name: 'Split pane 1 vertically' }));
    act(() => latestSocket().acknowledge('splitPane', { sessionId: 's2' }));
    act(() => latestSocket().receive('state', afterSplit));

    expect(screen.getByRole('button', { name: 'CLOSE PANE 2' })).toBeTruthy();
    expect(latestSocket().emitted).toContainEqual({ event: 'watch', payload: { sessionIds: ['s2'] } });
  });

  it('says why when iTerm2 would not split it', async () => {
    const user = await openMap();
    await user.click(screen.getByRole('button', { name: 'Split pane 1 horizontally' }));
    act(() => latestSocket().acknowledge('splitPane', { error: 'pane too small' }));
    expect(screen.getByText('Could not split: pane too small')).toBeTruthy();
  });
});

describe('restarting the server from the phone', () => {
  let requests: Array<{ url: string; init?: RequestInit }>;

  beforeEach(() => {
    requests = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      return new Response(JSON.stringify({ ok: true }), { status: url === '/api/restart-server' ? 202 : 204 });
    }));
    localStorage.setItem('remote-iterm-access-key', 'the-key');
  });
  const restarts = () => requests.filter((r) => r.url === '/api/restart-server');

  it('asks first, then asks the Mac to restart it', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole('button', { name: 'Restart the server' }));
    expect(screen.getByRole('dialog', { name: 'Restart the server' })).toBeTruthy();
    expect(restarts()).toEqual([]);

    await user.click(screen.getByRole('button', { name: 'RESTART' }));

    expect(restarts()).toHaveLength(1);
    expect(restarts()[0].init?.method).toBe('POST');
    expect(restarts()[0].init?.body).toBe(JSON.stringify({ key: 'the-key' }));
    expect(await screen.findByText(/Restarting the server/)).toBeTruthy();
  });

  it('does nothing when the question is declined', async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole('button', { name: 'Restart the server' }));
    await user.click(screen.getByRole('button', { name: 'CANCEL' }));
    expect(restarts()).toEqual([]);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('says so if no restarted server has appeared after a while', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<App />);
      act(() => latestSocket().receive('connect', undefined));
      act(() => latestSocket().receive('serverInfo', { instance: 'A' }));
      await user.click(screen.getByRole('button', { name: 'Restart the server' }));
      await user.click(screen.getByRole('button', { name: 'RESTART' }));
      await act(async () => { await vi.advanceTimersByTimeAsync(91_000); });
      expect(screen.getByText(/The server has not come back yet/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('says the server restarted once a new one connects', async () => {
    const user = userEvent.setup();
    render(<App />);
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('serverInfo', { instance: 'A' }));
    await user.click(screen.getByRole('button', { name: 'Restart the server' }));
    await user.click(screen.getByRole('button', { name: 'RESTART' }));
    await screen.findByText(/Restarting the server/);
    act(() => latestSocket().receive('disconnect', 'transport close'));
    act(() => latestSocket().receive('connect', undefined));
    act(() => latestSocket().receive('serverInfo', { instance: 'B' }));
    expect(screen.getByText('Server restarted')).toBeTruthy();
  });

  it('tells the user when the Mac refuses', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'a restart is already under way' }), { status: 409 })));
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole('button', { name: 'Restart the server' }));
    await user.click(screen.getByRole('button', { name: 'RESTART' }));
    expect(await screen.findByText('Restart refused: a restart is already under way')).toBeTruthy();
  });
});

describe('frames the server held from earlier', () => {
  it('draw a pane at once, but never over newer content', () => {
    render(<App />);
    act(() => latestSocket().receive('state', state));
    act(() => latestSocket().receive('content', { ...frame([[{ t: 'from before' }]]), cached: true }));
    const pane = () => document.querySelector('pre.terminal-output');
    expect(pane()?.textContent).toContain('from before');

    act(() => latestSocket().receive('content', frame([[{ t: 'live' }]])));
    act(() => latestSocket().receive('content', { ...frame([[{ t: 'from before' }]]), cached: true }));
    expect(pane()?.textContent).toContain('live');
    expect(pane()?.textContent).not.toContain('from before');
  });
});

describe('a pane followed for a long time', () => {
  // terminalEnd is where the terminal ends, not where this page does: a page
  // of history carries the same terminalEnd as the live tail it sits above.
  const manyLines = (count: number, first = 0, terminalEnd = first + count) => ({
    sessionId: 's1', fg: '#fff', bg: '#000', isLatest: true,
    lines: Array.from({ length: count }, (_, i) => [{ t: `line ${first + i}` }]),
    firstLine: first, availableFirstLine: 0, terminalEnd,
  });

  it('keeps only its recent lines while being followed', () => {
    render(<App />);
    act(() => latestSocket().receive('state', state));
    act(() => latestSocket().receive('content', manyLines(1500)));
    const pane = document.querySelector('pre.terminal-output');
    expect(pane?.children.length).toBe(1000);
    expect(pane?.textContent).toContain('line 1499');
    expect(pane?.textContent).not.toContain('line 499\n');
  });

  it('keeps everything while it is being read back', () => {
    render(<App />);
    act(() => latestSocket().receive('state', state));
    act(() => latestSocket().receive('content', manyLines(250, 1250)));
    const scroller = document.querySelector('pre.terminal-output')?.parentElement;
    if (!scroller) throw new Error('no scroller');
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 5000 });
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 500 });
    scroller.scrollTop = 1000;
    fireEvent.scroll(scroller);
    act(() => latestSocket().receive('historyContent', { ...manyLines(1250, 0, 1500), isLatest: false }));
    act(() => latestSocket().receive('content', manyLines(250, 1260)));
    expect(document.querySelector('pre.terminal-output')?.children.length).toBe(1510);
  });

  it('drops an older page that no longer joins what is kept', () => {
    render(<App />);
    act(() => latestSocket().receive('state', state));
    act(() => latestSocket().receive('content', manyLines(1500, 9000)));   // kept: 9500..10500
    // A page asked for earlier, before the tail moved on.
    act(() => latestSocket().receive('historyContent', { ...manyLines(500, 8000, 10500), isLatest: false }));
    const pane = document.querySelector('pre.terminal-output');
    expect(pane?.children.length).toBe(1000);
    expect(pane?.textContent).toContain('line 10499');
  });
});
