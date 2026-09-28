// Everything typed on the phone, numbered and kept until the server says
// iTerm2 has it.
//
// Socket.IO delivers an emit at most once: one written into a WebSocket that
// has just died — which on a phone happens every minute or so — is gone
// without a trace. That was "I pressed send and the text was ignored". So
// input is not simply emitted. Each item carries this page's id and a
// sequence number, and waits here through two acknowledgements:
//
//   received   the Socket.IO ack, which the server sends as soon as it has
//              queued the item. One that does not come back means the socket
//              is dead, so it is dropped and reconnected at once instead of
//              waiting out Engine.IO's ping timeout.
//   delivered  `inputDelivered`, once iTerm2 has actually taken the text —
//              which, while iTerm2 is stalled, is whenever the stall ends.
//
// Anything not yet received is sent again after a reconnect; the server
// never delivers a sequence number twice. Nothing is sent on a connection
// until its server has said which process it is (`serverInfo`): input sent to
// a server that has since been replaced — acknowledged or not, since the ack
// may have been lost after the text reached iTerm2 — cannot be known either
// way, so it is neither resent nor forgotten: it is reported to the user.
//
// A server too old to number input acknowledges with nothing; that is taken
// as delivery, as it was before numbering existed, rather than resent forever.

export type InputKind = 'execute' | 'sendKeys' | 'broadcast';

export interface PendingInput {
  seq: number;
  event: InputKind;
  payload: Record<string, unknown>;
  /** What the user would recognise it by, for a notice. */
  label: string;
  /** unsent: not on the wire, or lost with a socket. sent: awaiting the
   *  server's ack. received: the server has it queued for iTerm2. */
  state: 'unsent' | 'sent' | 'received';
  queuedAt: number;
  sentAt: number;
  /** The server instance it was last sent to, acknowledged or not. */
  sentTo: string | null;
}

export interface InputTransport {
  isConnected(): boolean;
  emit(event: InputKind, payload: Record<string, unknown>, onAck: (ack: unknown) => void): void;
  /** Drop a socket that has stopped answering, so it reconnects now. */
  reconnect(): void;
}

export interface InputQueueListener {
  changed(): void;
  failed(input: PendingInput, reason: string): void;
  /** Inputs a replaced server had taken but may never have delivered. */
  uncertain(inputs: PendingInput[]): void;
}

export interface DeliveryFailure { seq: number; reason: string }

// The instance recorded for a server that never says which one it is.
export const LEGACY_SERVER = 'legacy';

// The server acknowledges on receipt, without waiting for iTerm2, so this
// only has to cover a slow network.
export const ACK_TIMEOUT_MS = 5000;

function parseAck(ack: unknown): { instance: string; delivered: number; failures: DeliveryFailure[] } | null {
  if (typeof ack !== 'object' || ack === null) return null;
  if (!('ok' in ack) || ack.ok !== true) return null;
  if (!('instance' in ack) || typeof ack.instance !== 'string') return null;
  if (!('delivered' in ack) || typeof ack.delivered !== 'number') return null;
  const failures = 'failed' in ack ? parseFailures(ack.failed) : [];
  return { instance: ack.instance, delivered: ack.delivered, failures };
}

export function parseFailures(value: unknown): DeliveryFailure[] {
  if (!Array.isArray(value)) return [];
  const failures: DeliveryFailure[] = [];
  for (const entry of value) {
    if (typeof entry !== 'object' || entry === null) continue;
    if (!('seq' in entry) || typeof entry.seq !== 'number') continue;
    if (!('reason' in entry) || typeof entry.reason !== 'string') continue;
    failures.push({ seq: entry.seq, reason: entry.reason });
  }
  return failures;
}

export class InputQueue {
  private seq = 0;
  private items: PendingInput[] = [];
  // The server on the current connection, once it has said which it is.
  private server: string | null = null;

  constructor(
    readonly page: string,
    private readonly transport: InputTransport,
    private readonly listener: InputQueueListener,
    private readonly now: () => number = Date.now,
  ) {}

  get pending(): readonly PendingInput[] {
    return this.items;
  }

  enqueue(event: InputKind, payload: Record<string, unknown>, label: string): void {
    this.seq += 1;
    const item: PendingInput = {
      seq: this.seq, event, payload, label,
      state: 'unsent', queuedAt: this.now(), sentAt: 0, sentTo: null,
    };
    this.items.push(item);
    if (this.canSend()) this.send(item);
    this.listener.changed();
  }

  /** A connection is up. Nothing is sent on it until its server says which
   *  process it is (serverInstance). */
  connected(): void {
    this.server = null;
    this.listener.changed();
  }

  /** The connection dropped: anything not acknowledged has to go again. */
  disconnected(): void {
    this.server = null;
    for (const item of this.items) {
      if (item.state === 'sent') item.state = 'unsent';
    }
    this.listener.changed();
  }

  /** The server on this connection has said which process it is: report
   *  what a different one may have taken, then send the rest in order. */
  serverInstance(instance: string): void {
    this.server = instance;
    const orphaned = this.items.filter((item) => item.sentTo !== null && item.sentTo !== instance);
    if (orphaned.length) {
      this.items = this.items.filter((item) => !orphaned.includes(item));
      this.listener.uncertain(orphaned);
    }
    if (this.canSend()) {
      for (const item of this.items) {
        if (item.state === 'unsent') this.send(item);
      }
    }
    this.listener.changed();
  }

  /** The server's cumulative report: everything up to `upto` is handled —
   *  including input this page thinks is unsent, because its ack was lost
   *  with a socket after the server had already taken it. */
  delivered(upto: number, failures: DeliveryFailure[]): void {
    const done = this.items.filter((item) => item.seq <= upto);
    if (done.length === 0) return;
    this.items = this.items.filter((item) => !done.includes(item));
    for (const item of done) {
      const failure = failures.find((f) => f.seq === item.seq);
      if (failure) this.listener.failed(item, failure.reason);
    }
    this.listener.changed();
  }

  /** Call periodically: a socket that swallows input without acknowledging
   *  it is dead, whatever it claims. */
  checkAcknowledgements(): void {
    if (!this.canSend()) return;
    const cutoff = this.now() - ACK_TIMEOUT_MS;
    if (this.items.some((item) => item.state === 'sent' && item.sentAt <= cutoff)) {
      this.transport.reconnect();
    }
  }

  /** The page was suspended: time spent hidden says nothing about the
   *  socket, so acknowledgements outstanding get a fresh ACK_TIMEOUT_MS. */
  resumed(): void {
    const now = this.now();
    for (const item of this.items) {
      if (item.state === 'sent') item.sentAt = now;
    }
  }

  private canSend(): boolean {
    return this.transport.isConnected() && this.server !== null;
  }

  private send(item: PendingInput): void {
    item.state = 'sent';
    item.sentAt = this.now();
    item.sentTo = this.server;
    this.transport.emit(item.event, { ...item.payload, page: this.page, seq: item.seq }, (ack) => {
      this.acknowledged(item, ack);
    });
  }

  private acknowledged(item: PendingInput, ack: unknown): void {
    if (ack === undefined || ack === null) {
      // A server that does not number input: it has typed it, once.
      if (!this.items.includes(item)) return;
      this.items = this.items.filter((other) => other !== item);
      this.listener.changed();
      return;
    }
    const parsed = parseAck(ack);
    if (!parsed) return;
    if (this.items.includes(item) && item.state === 'sent') item.state = 'received';
    if (parsed.instance !== this.server) this.serverInstance(parsed.instance);
    this.delivered(parsed.delivered, parsed.failures);
    this.listener.changed();
  }
}
