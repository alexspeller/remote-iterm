// @vitest-environment jsdom
//
// The command box has to survive a page reload the user did not ask for:
// iOS evicts a background page (and relaunches a home-screen web app)
// without warning, and the half-typed command was the state that hurt most
// to lose. These tests render the real App and only fake the socket, so
// the input, its state, and the localStorage round trip are all exercised.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';

// A socket that records what the app emits and never connects — a phone
// whose Mac is unreachable — since the draft must be kept regardless.
const fakes = vi.hoisted(() => {
  class FakeSocket {
    readonly emitted: Array<{ event: string; payload: unknown }> = [];
    get volatile() { return this; }
    on() { return this; }
    emit(event: string, ...args: unknown[]) {
      this.emitted.push({ event, payload: args[0] });
      return this;
    }
    disconnect() { return this; }
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

describe('the unsent command', () => {
  beforeEach(() => {
    localStorage.clear();
    fakes.sockets.length = 0;
    window.matchMedia = portrait;
  });
  afterEach(cleanup);

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
