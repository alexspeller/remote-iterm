import { beforeEach, describe, expect, it } from 'vitest';
import { ACK_TIMEOUT_MS, InputQueue, type InputKind, type PendingInput } from './inputQueue';

// A transport whose socket the test connects, drops and acknowledges by hand.
class FakeTransport {
  connected = false;
  reconnects = 0;
  readonly sent: Array<{ event: InputKind; payload: Record<string, unknown>; ack: (ack: unknown) => void }> = [];
  isConnected() { return this.connected; }
  emit(event: InputKind, payload: Record<string, unknown>, ack: (ack: unknown) => void) {
    this.sent.push({ event, payload, ack });
  }
  reconnect() { this.reconnects += 1; }
  seqs() { return this.sent.map((s) => s.payload.seq); }
}

const ack = (instance: string, delivered = 0, failed: unknown[] = []) => ({ ok: true, instance, delivered, failed });

let transport: FakeTransport;
let now: number;
let failed: Array<{ label: string; reason: string }>;
let uncertain: string[][];
let queue: InputQueue;

// A connection to server `instance`, as the page sees it: connected, then
// told which server it is.
function connectTo(instance: string) {
  transport.connected = true;
  queue.connected();
  queue.serverInstance(instance);
}

function drop() {
  transport.connected = false;
  queue.disconnected();
}

beforeEach(() => {
  transport = new FakeTransport();
  now = 1000;
  failed = [];
  uncertain = [];
  queue = new InputQueue('page-1', transport, {
    changed: () => {},
    failed: (input: PendingInput, reason: string) => failed.push({ label: input.label, reason }),
    uncertain: (inputs: PendingInput[]) => uncertain.push(inputs.map((i) => i.label)),
  }, () => now);
});

describe('the input queue', () => {
  it('numbers each input and names the page it came from', () => {
    connectTo('A');
    queue.enqueue('sendKeys', { sessionId: 's1', keys: 'a' }, 'a');
    queue.enqueue('execute', { sessionId: 's1', command: 'ls' }, 'ls');
    expect(transport.sent.map((s) => s.payload)).toEqual([
      { sessionId: 's1', keys: 'a', page: 'page-1', seq: 1 },
      { sessionId: 's1', command: 'ls', page: 'page-1', seq: 2 },
    ]);
  });

  it('holds input typed while disconnected and sends it once the server is known', () => {
    queue.enqueue('execute', { command: 'make' }, 'make');
    transport.connected = true;
    queue.connected();
    expect(transport.sent).toEqual([]);   // not before the server says who it is
    queue.serverInstance('A');
    expect(transport.seqs()).toEqual([1]);
  });

  it('sends again what a dropped socket never acknowledged, and only that', () => {
    connectTo('A');
    queue.enqueue('sendKeys', { keys: 'a' }, 'a');
    queue.enqueue('sendKeys', { keys: 'b' }, 'b');
    transport.sent[0].ack(ack('A'));        // the server has "a"
    drop();                                   // "b" died with the socket
    transport.sent.length = 0;
    connectTo('A');
    expect(transport.seqs()).toEqual([2]);
  });

  it('forgets input once the server reports it delivered', () => {
    connectTo('A');
    queue.enqueue('sendKeys', { keys: 'a' }, 'a');
    queue.enqueue('sendKeys', { keys: 'b' }, 'b');
    transport.sent[0].ack(ack('A'));
    transport.sent[1].ack(ack('A'));
    expect(queue.pending).toHaveLength(2);
    queue.delivered(2, []);
    expect(queue.pending).toHaveLength(0);
  });

  it('counts input the server delivered even if its ack was lost', () => {
    connectTo('A');
    queue.enqueue('execute', { command: 'deploy' }, 'deploy');
    drop();                     // no ack ever came back
    queue.delivered(1, []);     // but the server reports it delivered
    connectTo('A');
    expect(transport.seqs()).toEqual([1]);  // sent once, never again
    expect(queue.pending).toHaveLength(0);
  });

  it('reports input iTerm2 could not take', () => {
    connectTo('A');
    queue.enqueue('execute', { command: 'deploy' }, 'deploy');
    transport.sent[0].ack(ack('A'));
    queue.delivered(1, [{ seq: 1, reason: 'that pane has closed' }]);
    expect(failed).toEqual([{ label: 'deploy', reason: 'that pane has closed' }]);
  });

  it('reports a failure even when a later ack counts it first', () => {
    // An ack can overtake the delivery report that names the failure.
    connectTo('A');
    queue.enqueue('execute', { command: 'deploy' }, 'deploy');
    queue.enqueue('sendKeys', { keys: 'q' }, 'q');
    transport.sent[1].ack(ack('A', 1, [{ seq: 1, reason: 'that pane has closed' }]));
    expect(failed).toEqual([{ label: 'deploy', reason: 'that pane has closed' }]);
  });

  it('asks rather than resends what a replaced server had taken', () => {
    connectTo('A');
    queue.enqueue('execute', { command: 'git push' }, 'git push');
    transport.sent[0].ack(ack('A'));   // A queued it; iTerm2 had not taken it yet
    drop();
    queue.enqueue('execute', { command: 'echo later' }, 'echo later');
    transport.sent.length = 0;
    connectTo('B');

    expect(uncertain).toEqual([['git push']]);
    expect(transport.seqs()).toEqual([2]);  // never sent to A, so it goes to B
    expect(queue.pending.map((i) => i.label)).toEqual(['echo later']);
  });

  it('asks too about input a replaced server may have taken without acknowledging', () => {
    // The ack may have been lost after the text had reached iTerm2.
    connectTo('A');
    queue.enqueue('execute', { command: 'rm -rf dist' }, 'rm -rf dist');
    drop();
    transport.sent.length = 0;
    connectTo('B');
    expect(uncertain).toEqual([['rm -rf dist']]);
    expect(transport.sent).toEqual([]);
  });

  it('keeps acknowledged input across a reconnect to the same server', () => {
    connectTo('A');
    queue.enqueue('execute', { command: 'sleep 60' }, 'sleep 60');
    transport.sent[0].ack(ack('A'));
    drop();
    transport.sent.length = 0;
    connectTo('A');
    expect(uncertain).toEqual([]);
    expect(transport.sent).toEqual([]);
    expect(queue.pending).toHaveLength(1);
  });

  it('takes an empty ack from a server too old to number input as delivery', () => {
    connectTo('legacy');
    queue.enqueue('execute', { command: 'make' }, 'make');
    transport.sent[0].ack(undefined);
    expect(queue.pending).toHaveLength(0);
    now += ACK_TIMEOUT_MS * 2;
    queue.checkAcknowledgements();
    expect(transport.reconnects).toBe(0);
    expect(transport.seqs()).toEqual([1]);
  });

  it('drops a socket that swallows input without acknowledging it', () => {
    connectTo('A');
    queue.enqueue('sendKeys', { keys: 'x' }, 'x');
    now += ACK_TIMEOUT_MS - 1;
    queue.checkAcknowledgements();
    expect(transport.reconnects).toBe(0);
    now += 1;
    queue.checkAcknowledgements();
    expect(transport.reconnects).toBe(1);
  });

  it('gives an acknowledgement its full time again after the page was suspended', () => {
    connectTo('A');
    queue.enqueue('sendKeys', { keys: 'x' }, 'x');
    now += ACK_TIMEOUT_MS * 10;   // the phone was locked
    queue.resumed();
    queue.checkAcknowledgements();
    expect(transport.reconnects).toBe(0);
  });

  it('ignores an acknowledgement it cannot read', () => {
    connectTo('A');
    queue.enqueue('sendKeys', { keys: 'x' }, 'x');
    transport.sent[0].ack({ ok: true });
    transport.sent[0].ack('nonsense');
    expect(queue.pending[0].state).toBe('sent');
  });
});
