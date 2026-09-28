import { useState, useEffect, useLayoutEffect, useRef, useMemo, memo } from 'react';
import { io, Socket } from 'socket.io-client';
import { Plus, X, Send, Clock, ChevronUp, ChevronDown, ChevronLeft, ChevronRight, CornerDownLeft, Trash2, Keyboard, Terminal, Lock, Unlock, Radio, Bell, Clipboard, Copy, WifiOff, Columns2, Rows2, LayoutGrid, List as ListIcon, KeyRound, ScrollText, SpellCheck2, RotateCw, Hourglass, TriangleAlert } from 'lucide-react';

import type { Session, Tab, WindowState, ScreenSize } from './types';
import { deepLinkSessionId, findSessionLocation, stripDeepLink } from './deepLink';
import { linkHref } from './links';
import { InputQueue, LEGACY_SERVER, parseFailures, type InputKind, type PendingInput } from './inputQueue';

// --- Types ---

// A run of text sharing one style: t=text, f=fg hex, g=bg hex, b=bold, d=dim.
// f/g omitted means "use the pane default" (theme fg/bg).
// u is a link target: an OSC 8 hyperlink, or a web address the server found.
interface Run { t: string; f?: string; g?: string; b?: boolean; d?: boolean; c?: boolean; u?: string }
interface StyledContent {
  lines: Run[][];
  fg: string;
  bg: string;
  firstLine: number;
  availableFirstLine: number;
  terminalEnd: number;
  isLatest: boolean;
}

const ACCENT = '#10b981';
const BROADCAST_COLOR = '#818cf8';
const GLOW = 'rgba(16,185,129,0.25)';
// Reserved for a destructive action that is waiting to be confirmed.
const DANGER = '#fb7185';
const DANGER_GLOW = 'rgba(251,113,133,0.3)';

const SOCKET_URL = window.location.hostname === 'localhost'
  ? 'http://localhost:7291'
  : `http://${window.location.hostname}:7291`;

const HISTORY_KEY = 'iterm-cmd-history';
const ACCESS_KEY_STORAGE_KEY = 'remote-iterm-access-key';
const SMART_TYPING_KEY = 'remote-iterm-smart-typing';
const TYPING_LOG_KEY = 'remote-iterm-typing-log';
const COMMAND_DRAFT_KEY = 'remote-iterm-command-draft';
const LAST_DISCONNECT_KEY = 'remote-iterm-last-disconnect';
const MAX_HISTORY = 100;
const MAX_TYPING_LOG_CHARS = 20000;
// How long a disconnection may last before the page says so. Reconnects
// after a network-path blip complete well inside this, so the banner is
// reserved for an outage the user actually needs to know about.
const RECONNECT_BANNER_DELAY_MS = 2500;
const BOTTOM_THRESHOLD_PX = 4;
// Lines kept per pane. A pane followed live used to keep every line that
// scrolled past for as long as the page was open, and each frame re-rendered
// all of them — hours of a busy pane made the page sluggish enough to drop
// typing. Older lines are fetched again on demand when scrolling up.
const MAX_RETAINED_LINES = 1000;
// Input the server has not confirmed for this long gets a visible counter.
const PENDING_BADGE_DELAY_MS = 800;
// A liveness ping unanswered this long means the socket is dead, whatever it
// claims; it is dropped and reconnected rather than left for Engine.IO's own
// ping timeout (45s) to notice.
const PING_DEAD_MS = 8000;
// Commands sent but not yet taken by the server, kept across a reload.
const UNCONFIRMED_KEY = 'remote-iterm-unconfirmed';
const RESTART_URL = '/api/restart-server';
// How long to wait for a server to say which process it is before taking it
// for one too old to (see inputQueue.ts).
const LEGACY_SERVER_WAIT_MS = 3000;
// Emits a dead socket's buffer must not carry onto the next connection.
const UNBUFFERED_EVENTS = new Set(['execute', 'sendKeys', 'broadcast', 'ping']);
// A restart that has not produced a new server by then is reported.
const RESTART_WAIT_MS = 90_000;
const DIRECT_INPUT_KEYS: Record<string, string> = {
  Enter: '\r',
  Backspace: '\x7f',
  Delete: '\x1b[3~',
  Tab: '\t',
  Escape: '\x1b',
  ArrowUp: '\x1b[A',
  ArrowDown: '\x1b[B',
  ArrowRight: '\x1b[C',
  ArrowLeft: '\x1b[D',
};

function isScrolledToBottom(element: HTMLDivElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= BOTTOM_THRESHOLD_PX;
}

function loadAccessKey(): string {
  const fragmentKey = new URLSearchParams(window.location.hash.slice(1)).get('key')?.trim();
  if (fragmentKey) {
    try { localStorage.setItem(ACCESS_KEY_STORAGE_KEY, fragmentKey); } catch {}
    return fragmentKey;
  }
  try { return localStorage.getItem(ACCESS_KEY_STORAGE_KEY)?.trim() || ''; } catch { return ''; }
}

function rememberAccessKey(key: string) {
  try { localStorage.setItem(ACCESS_KEY_STORAGE_KEY, key); } catch {}
  const hash = new URLSearchParams({ key }).toString();
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${hash}`);
}

// Ask the server to (re)issue its HttpOnly auth cookie (POST /auth). The key
// proves who we are the first time; after that the cookie itself does, so this
// also renews a cookie-only session and its expiry slides forward with use.
// That cookie is what keeps a notification deep link working after Safari has
// purged this page's localStorage. Best-effort: the live connection is already
// up when this runs and does not depend on it.
async function refreshKeyCookie(key: string) {
  try {
    await fetch(`${SOCKET_URL}/auth`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(key ? { key } : {}),
    });
  } catch {}
}

// Off by default, matching prior behavior — this only ever loosens the
// buffered command box; direct/live input always forces these off (see the
// input's props below), since autocorrecting mid-keystroke would corrupt
// whatever is being typed character-by-character into the terminal.
function loadSmartTyping(): boolean {
  try { return localStorage.getItem(SMART_TYPING_KEY) === '1'; } catch { return false; }
}

function loadTypingLog(): string {
  try { return localStorage.getItem(TYPING_LOG_KEY) || ''; } catch { return ''; }
}

// The buffered command box is where anything substantial gets composed, and
// a page reload is not always the user's doing: iOS evicts a background page
// (and relaunches a home-screen web app) without warning. Whatever has been
// typed but not yet sent therefore lives in localStorage keystroke by
// keystroke, and is put back in the box on the next load.
function loadCommandDraft(): string {
  try { return localStorage.getItem(COMMAND_DRAFT_KEY) || ''; } catch { return ''; }
}

// --- Connection diagnostics ---
// Each connection introduces itself to the server (`hello`) with what only
// the page can know: whether this is a fresh load or the same page
// reconnecting, how the page saw its previous connection end, and what it
// could see of the network. The server only logs it; it exists so that a
// phone that keeps showing RECONNECTING can be diagnosed from the Mac.
const PAGE_ID = Math.random().toString(36).slice(2, 8);
let connectCount = 0;

interface LastDisconnect { reason: string; at: number; visibility: string }

function isLastDisconnect(value: unknown): value is LastDisconnect {
  return typeof value === 'object' && value !== null
    && 'reason' in value && typeof value.reason === 'string'
    && 'at' in value && typeof value.at === 'number'
    && 'visibility' in value && typeof value.visibility === 'string';
}

// What the transport saw: the raw WebSocket close (its code tells a
// protocol or data error from a plain lost connection) and how long the
// socket had been idle in each direction when it died.
let lastSendAt = 0;
let lastReceiveAt = 0;
let lastSocketClose: { code: number; reason: string; wasClean: boolean; at: number } | null = null;

function observeTransport(socket: Socket) {
  const engine = socket.io?.engine;
  if (!engine) return;
  engine.on('packetCreate', () => { lastSendAt = Date.now(); });
  engine.on('packet', () => { lastReceiveAt = Date.now(); });
  const watchWebSocket = (transport: unknown) => {
    const ws: unknown = typeof transport === 'object' && transport !== null ? Reflect.get(transport, 'ws') : null;
    if (!(ws instanceof WebSocket)) return;
    ws.addEventListener('close', (event) => {
      lastSocketClose = { code: event.code, reason: event.reason, wasClean: event.wasClean, at: Date.now() };
      // Socket.IO reports its disconnect before the WebSocket's own close
      // event arrives, so attach the code to the record just written.
      try {
        const raw = sessionStorage.getItem(LAST_DISCONNECT_KEY);
        const parsed: unknown = raw ? JSON.parse(raw) : null;
        if (isLastDisconnect(parsed) && Date.now() - parsed.at < 5000) {
          sessionStorage.setItem(LAST_DISCONNECT_KEY, JSON.stringify({ ...parsed, socketClose: lastSocketClose }));
        }
      } catch {}
    });
  };
  watchWebSocket(engine.transport);
  engine.on('upgrade', watchWebSocket);
}

// sessionStorage: survives a reload of this tab, not a new tab, which is
// exactly the distinction wanted.
function rememberDisconnect(reason: string) {
  const now = Date.now();
  try {
    sessionStorage.setItem(LAST_DISCONNECT_KEY, JSON.stringify({
      reason,
      at: now,
      visibility: document.visibilityState,
      sinceSendMs: lastSendAt ? now - lastSendAt : null,
      sinceReceiveMs: lastReceiveAt ? now - lastReceiveAt : null,
      socketClose: lastSocketClose && now - lastSocketClose.at < 5000 ? lastSocketClose : null,
    }));
  } catch {}
}

function takeLastDisconnect(): LastDisconnect | null {
  try {
    const raw = sessionStorage.getItem(LAST_DISCONNECT_KEY);
    sessionStorage.removeItem(LAST_DISCONNECT_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isLastDisconnect(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function navigationType(): string {
  const entry = performance.getEntriesByType('navigation')[0];
  return typeof PerformanceNavigationTiming !== 'undefined' && entry instanceof PerformanceNavigationTiming
    ? entry.type
    : 'unknown';
}

function connectionHello() {
  const last = takeLastDisconnect();
  connectCount += 1;
  return {
    page: PAGE_ID,
    connect: connectCount,
    navigation: navigationType(),
    visibility: document.visibilityState,
    online: navigator.onLine,
    host: window.location.host,
    lastDisconnect: last ? { ...last, agoMs: Date.now() - last.at } : null,
  };
}

// A single rolling string rather than an array of entries: a buffered
// command is a complete unit and starts its own line, while direct-input
// text arrives one keystroke (or IME commit) at a time and should
// concatenate onto the current line exactly as it was typed.
function appendTypingLog(current: string, text: string, newLine: boolean): string {
  if (!text) return current;
  let next = !current || newLine ? (current ? `${current}\n${text}` : text) : current + text;
  if (next.length > MAX_TYPING_LOG_CHARS) {
    next = next.slice(next.length - MAX_TYPING_LOG_CHARS);
    // Land on a clean line boundary instead of a chopped-off first line.
    const firstBreak = next.indexOf('\n');
    if (firstBreak !== -1) next = next.slice(firstBreak + 1);
  }
  return next;
}

function sameRuns(a: Run[], b: Run[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  return a.every((run, i) => {
    const other = b[i];
    return run.t === other.t && run.f === other.f && run.g === other.g && run.b === other.b
      && run.d === other.d && run.c === other.c && run.u === other.u;
  });
}

function mergeStyledContent(current: StyledContent | undefined, incoming: StyledContent): StyledContent {
  if (!current) return incoming;

  const currentEnd = current.firstLine + current.lines.length;
  const incomingEnd = incoming.firstLine + incoming.lines.length;
  // A gap means output advanced beyond the live-tail window before this client
  // received an update. Use the authoritative tail; the gap remains available
  // through normal upward pagination.
  if (incoming.firstLine > currentEnd || current.firstLine > incomingEnd) return incoming;

  const firstLine = Math.max(
    incoming.availableFirstLine,
    Math.min(current.firstLine, incoming.firstLine),
  );
  const endLine = Math.min(
    incoming.terminalEnd,
    Math.max(currentEnd, incomingEnd),
  );
  const lines: Run[][] = Array.from({ length: Math.max(0, endLine - firstLine) }, () => []);

  const copy = (source: Run[][], sourceFirst: number, removeCursor: boolean, keepEqual: boolean) => {
    source.forEach((runs, index) => {
      const target = sourceFirst + index - firstLine;
      if (target < 0 || target >= lines.length) return;
      const existing = lines[target];
      if (keepEqual && existing.length && sameRuns(existing, runs)) return;
      lines[target] = removeCursor && runs.some(run => run.c) ? runs.filter(run => !run.c) : runs;
    });
  };

  // A live update carries the current cursor, so remove the stale cursor from
  // any preserved history before overwriting the recent tail. Every line that
  // did not change keeps the array it had — a line without the old cursor,
  // and an incoming line equal to the one already held — so its rendered row
  // (a memoized TerminalLine) is not redone.
  copy(current.lines, current.firstLine, incoming.isLatest, false);
  copy(incoming.lines, incoming.firstLine, false, true);

  return {
    lines,
    fg: incoming.fg,
    bg: incoming.bg,
    firstLine,
    availableFirstLine: incoming.availableFirstLine,
    terminalEnd: incoming.terminalEnd,
    isLatest: current.isLatest || incoming.isLatest,
  };
}

// Keep only a pane's most recent lines. Nothing earlier is lost for good:
// scrolling to the top of what is kept pages older lines back in.
function trimStyledContent(content: StyledContent, maxLines: number): StyledContent {
  const excess = content.lines.length - maxLines;
  if (excess <= 0) return content;
  return { ...content, lines: content.lines.slice(excess), firstLine: content.firstLine + excess };
}

const KEY_NAMES: Record<string, string> = {
  '\r': '⏎', '\n': '⇧⏎', '\t': 'Tab', '\x1b': 'Esc', '\x7f': '⌫', '\x1b[3~': 'Del',
  '\x03': '^C', '\x04': '^D', '\x1a': '^Z', '\x0c': '^L', '\x15': '^U',
  '\x1b[A': '↑', '\x1b[B': '↓', '\x1b[C': '→', '\x1b[D': '←',
};

// How a keystroke is named in a notice about it.
function describeKeys(keys: string): string {
  const named = KEY_NAMES[keys];
  if (named) return named;
  const text = keys.replace(/[\x00-\x1f\x7f]/g, ' ').trim();
  return text.length > 40 ? `${text.slice(0, 40)}…` : text || 'keys';
}

function saveUnconfirmed(commands: string[]) {
  try {
    if (commands.length) localStorage.setItem(UNCONFIRMED_KEY, JSON.stringify(commands));
    else localStorage.removeItem(UNCONFIRMED_KEY);
  } catch {}
}

// Commands this page had sent but the server had not confirmed when it was
// last unloaded (iOS evicting a background page does that without asking).
function takeUnconfirmed(): string[] {
  try {
    const raw = localStorage.getItem(UNCONFIRMED_KEY);
    localStorage.removeItem(UNCONFIRMED_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((c): c is string => typeof c === 'string') : [];
  } catch {
    return [];
  }
}

function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function loadHistory(): string[] {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch { return []; }
}
function saveHistory(h: string[]) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(h.slice(-MAX_HISTORY)));
}
function getSelectedTab(win?: WindowState): Tab | undefined {
  return win?.tabs.find(t => t.isSelected) || win?.tabs[0];
}
// The pane to show when a tab is selected: the one iTerm has focused, else the first.
function tabDefaultSession(tab?: Tab): string | undefined {
  return tab?.currentSessionId || tab?.sessions[0]?.id;
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [accessKey, setAccessKey] = useState(loadAccessKey);
  const [accessKeyInput, setAccessKeyInput] = useState('');
  const [authError, setAuthError] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [state, setState] = useState<WindowState[]>([]);
  const [screenSize, setScreenSize] = useState<ScreenSize | null>(null);
  const [content, setContent] = useState<StyledContent | null>(null);
  const [command, setCommand] = useState(loadCommandDraft);
  const [directInputMode, setDirectInputMode] = useState(false);
  const [directInputValue, setDirectInputValue] = useState('');
  const [smartTyping, setSmartTyping] = useState(loadSmartTyping);
  const [typingLog, setTypingLog] = useState(loadTypingLog);
  const [showTypingLog, setShowTypingLog] = useState(false);
  const [selectedWinId, setSelectedWinId] = useState<string | null>(null);
  const [selectedTabId, setSelectedTabId] = useState<string | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [showMap, setShowMap] = useState(false);
  const [scrollLocked, setScrollLocked] = useState(false);
  const [broadcastMode, setBroadcastMode] = useState(false);
  const [broadcastTargets, setBroadcastTargets] = useState<Set<string>>(new Set());
  const [alertDone, setAlertDone] = useState(false);
  const [latency, setLatency] = useState<number | null>(null);
  const [isLandscape, setIsLandscape] = useState(window.matchMedia('(orientation: landscape)').matches);
  const [splitMode, setSplitMode] = useState(false);
  const [splitSessionId, setSplitSessionId] = useState<string | null>(null);
  const [splitContent, setSplitContent] = useState<StyledContent | null>(null);
  const [splitRatio, setSplitRatio] = useState(0.5);
  const [focusedPane, setFocusedPane] = useState<'primary' | 'split'>('primary');
  const [splitWinId, setSplitWinId] = useState<string | null>(null);
  const [splitTabId, setSplitTabId] = useState<string | null>(null);
  const [showSplitMap, setShowSplitMap] = useState(false);
  const [showPaneMap, setShowPaneMap] = useState(false);
  // Closing a pane kills whatever is running in it, so the map's close button
  // arms a confirmation rather than acting on the first tap.
  const [paneCloseArmed, setPaneCloseArmed] = useState(false);
  const [tabListTarget, setTabListTarget] = useState<'tabBar' | 'split' | null>(null);
  // Bumped to re-render the pane map's content thumbnails when cached pane
  // content arrives (cache lives in a ref, so it can't trigger renders itself).
  const [, setPreviewTick] = useState(0);
  const socketRef = useRef<Socket | null>(null);
  const commandInputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const splitContentRef = useRef<HTMLDivElement>(null);
  const primaryAtBottomRef = useRef(true);
  const splitAtBottomRef = useRef(true);
  const primaryHistoryLoadingRef = useRef(false);
  const splitHistoryLoadingRef = useRef(false);
  const primaryPrependRef = useRef<{ height: number; top: number } | null>(null);
  const splitPrependRef = useRef<{ height: number; top: number } | null>(null);
  const splitContainerRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef(state);
  const winIdRef = useRef(selectedWinId);
  const tabIdRef = useRef(selectedTabId);
  const selectedSessionIdRef = useRef(selectedSessionId);
  const prevFrontRef = useRef<string | null>(null);
  const historyRef = useRef<string[]>(loadHistory());
  const historyIdxRef = useRef(-1);
  const savedCommandRef = useRef('');
  const lastContentTimeRef = useRef(0);
  const contentChangingRef = useRef(false);
  const alertTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const contentCacheRef = useRef<Map<string, StyledContent>>(new Map());
  const splitSessionIdRef = useRef(splitSessionId);
  const focusedPaneRef = useRef(focusedPane);
  const splitWinIdRef = useRef(splitWinId);
  const splitTabIdRef = useRef(splitTabId);
  const paneMapOpenRef = useRef(false);
  // The pane a notification deep link asks for, until the first state tells us
  // where it is (or that it is gone). Survives a key prompt in between.
  const pendingDeepLinkRef = useRef<string | null>(deepLinkSessionId(window.location.hash));
  // Live updates for a pane are held back while a finger is on it or text in
  // it is selected, and applied once both have ended. iOS abandons a
  // long-press text selection if the DOM under it changes mid-gesture, and a
  // selection that does survive drifts as lines are replaced beneath it; a
  // pane running a spinner redraws several times a second, which made
  // selecting anything in it impossible. The held frame remembers which
  // session it belongs to, so switching panes mid-selection cannot flush
  // one pane's output into another.
  const touchingRef = useRef({ primary: false, split: false });
  const holdRef = useRef({ primary: false, split: false });
  const heldContentRef = useRef<{
    primary: { sessionId: string; content: StyledContent } | null;
    split: { sessionId: string; content: StyledContent } | null;
  }>({ primary: null, split: null });
  const [selectionHold, setSelectionHold] = useState(false);
  // Input on its way to iTerm2 (see inputQueue.ts), mirrored into state so
  // the command bar can show what is still waiting.
  const [pendingInputs, setPendingInputs] = useState<PendingInput[]>([]);
  // When iTerm2 stopped answering, as the server reports it; null while it does.
  const [itermStalledSince, setItermStalledSince] = useState<number | null>(null);
  // A clock for the counters above, ticking only while one of them is shown.
  const [clock, setClock] = useState(() => Date.now());
  const [showRestartConfirm, setShowRestartConfirm] = useState(false);
  const connectedRef = useRef(false);
  const scrollLockedRef = useRef(scrollLocked);
  useEffect(() => { scrollLockedRef.current = scrollLocked; }, [scrollLocked]);
  const restartTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (restartTimerRef.current) clearTimeout(restartTimerRef.current); }, []);
  const restartRequestedRef = useRef(false);
  const serverInstanceRef = useRef<string | null>(null);
  // A pane just split from the phone, selected once a state that includes it
  // arrives — the way iTerm focuses a new split on the Mac.
  const pendingSplitRef = useRef<{ sessionId: string; pane: 'primary' | 'split' } | null>(null);

  // Puts a command that did not make it back in the box, unless something
  // else has been typed there since.
  // Only a command for one pane: putting back a broadcast some panes took
  // would invite sending it to them twice.
  const restoreCommand = (input: PendingInput) => {
    const text = input.payload.command;
    if (input.event !== 'execute' || typeof text !== 'string' || !text) return;
    setCommand(current => (current.trim() ? current : text));
  };

  const queueRef = useRef<InputQueue | null>(null);
  if (queueRef.current === null) {
    const queue = new InputQueue(PAGE_ID, {
      isConnected: () => connectedRef.current,
      emit: (event: InputKind, payload, onAck) => { socketRef.current?.emit(event, payload, onAck); },
      reconnect: () => { socketRef.current?.io?.engine?.close(); },
    }, {
      changed: () => {
        setPendingInputs(queue.pending.map(input => ({ ...input })));
        saveUnconfirmed(queue.pending
          .filter(input => input.event !== 'sendKeys' && input.state !== 'received')
          .map(input => input.label));
      },
      failed: (input, reason) => {
        setNotice(`Not delivered: ${input.label} — ${reason}`);
        restoreCommand(input);
      },
      uncertain: (inputs) => {
        setNotice(`The server restarted before iTerm2 confirmed: ${inputs.map(i => i.label).join(' · ')} — check the pane before sending again`);
        const last = inputs[inputs.length - 1];
        if (last) restoreCommand(last);
      },
    });
    queueRef.current = queue;
  }
  const inputQueue = queueRef.current;

  useEffect(() => { stateRef.current = state; }, [state]);
  useEffect(() => { winIdRef.current = selectedWinId; }, [selectedWinId]);
  useEffect(() => { tabIdRef.current = selectedTabId; }, [selectedTabId]);
  useEffect(() => { selectedSessionIdRef.current = selectedSessionId; }, [selectedSessionId]);
  useEffect(() => { splitSessionIdRef.current = splitSessionId; }, [splitSessionId]);
  useEffect(() => { focusedPaneRef.current = focusedPane; }, [focusedPane]);
  useEffect(() => { splitWinIdRef.current = splitWinId; }, [splitWinId]);
  useEffect(() => { splitTabIdRef.current = splitTabId; }, [splitTabId]);
  useEffect(() => { paneMapOpenRef.current = showPaneMap; }, [showPaneMap]);
  // An armed confirmation is only ever about the pane on screen right now:
  // drop it if the map closes, or if the pane it named stops being the
  // selected one (the Mac can move the selection under us).
  useEffect(() => { setPaneCloseArmed(false); }, [showPaneMap, selectedSessionId]);
  useEffect(() => {
    try { localStorage.setItem(TYPING_LOG_KEY, typingLog); } catch {}
  }, [typingLog]);
  useEffect(() => {
    try {
      if (command) localStorage.setItem(COMMAND_DRAFT_KEY, command);
      else localStorage.removeItem(COMMAND_DRAFT_KEY);
    } catch {}
  }, [command]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), notice.length > 60 ? 9000 : 4000);
    return () => clearTimeout(timer);
  }, [notice]);

  // A command this page sent but had no confirmation for when it was last
  // unloaded: back in the box, never resent on its own.
  useEffect(() => {
    const unconfirmed = takeUnconfirmed();
    if (!unconfirmed.length) return;
    const last = unconfirmed[unconfirmed.length - 1];
    setCommand(current => (current.trim() ? current : last));
    setNotice(`Not confirmed before the page reloaded: ${unconfirmed.join(' · ')} — check the pane before sending again`);
  }, []);

  // Input still waiting, and iTerm2 stalls, are shown with how long they
  // have lasted; the clock only ticks while either is on screen.
  const hasPending = pendingInputs.length > 0;
  useEffect(() => {
    if (!hasPending && itermStalledSince === null) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [hasPending, itermStalledSince]);

  // A socket that swallows input without acknowledging it is dropped and
  // reconnected; the queue decides when that has happened.
  useEffect(() => {
    const timer = setInterval(() => inputQueue.checkAcknowledgements(), 1000);
    return () => clearInterval(timer);
  }, [inputQueue]);

  // The reconnect banner waits out the brief gaps; the top-bar dot goes red
  // at once for anyone who wants to see them.
  const [reconnectBanner, setReconnectBanner] = useState(false);
  useEffect(() => {
    if (connected) {
      setReconnectBanner(false);
      return;
    }
    const timer = setTimeout(() => setReconnectBanner(true), RECONNECT_BANNER_DELAY_MS);
    return () => clearTimeout(timer);
  }, [connected]);

  // --- Socket init ---
  useEffect(() => {
    setConnected(false);

    // Connect even without a key: the server also accepts its HttpOnly auth
    // cookie (sent automatically with credentials), so a phone whose
    // localStorage was purged still gets in. Only a refusal shows the prompt.
    const s = io(SOCKET_URL, {
      auth: accessKey ? { key: accessKey } : {},
      withCredentials: true,
      reconnection: true,
      reconnectionAttempts: Infinity,
      // A phone's WebSocket dies whenever its network path so much as
      // flickers (the `hello` diagnostics showed a visible, online page
      // losing its socket to a transport error every 10-60s over Tailscale).
      // Reconnecting at once, and straight to WebSocket rather than through
      // a polling handshake first, keeps the gap short enough that the
      // banner below never needs to appear for one of those.
      reconnectionDelay: 250,
      reconnectionDelayMax: 2000,
      randomizationFactor: 0.3,
      rememberUpgrade: true,
      timeout: 10000,
    });
    socketRef.current = s;

    // Input waits on each connection until the server says which process it
    // is (`serverInfo`, sent as soon as it connects); a server too old to say
    // is assumed after LEGACY_SERVER_WAIT_MS.
    let legacyTimer: ReturnType<typeof setTimeout> | undefined;
    s.on('connect', () => {
      connectedRef.current = true;
      setAuthError(false);
      setConnected(true);
      observeTransport(s);
      s.emit('hello', connectionHello());
      inputQueue.connected();
      clearTimeout(legacyTimer);
      legacyTimer = setTimeout(() => inputQueue.serverInstance(LEGACY_SERVER), LEGACY_SERVER_WAIT_MS);
      void refreshKeyCookie(accessKey);
    });
    s.on('disconnect', (reason) => {
      connectedRef.current = false;
      setConnected(false);
      rememberDisconnect(reason);
      clearTimeout(legacyTimer);
      // Socket.IO keeps what was emitted into a socket it had not yet noticed
      // was dead, and sends it first thing on the next connection — ahead of
      // older input the queue resends, which would put input out of order.
      // The queue resends it all itself, in order; pings are simply redone.
      if (Array.isArray(s.sendBuffer)) {
        s.sendBuffer = s.sendBuffer.filter(packet =>
          !(Array.isArray(packet.data) && typeof packet.data[0] === 'string' && UNBUFFERED_EVENTS.has(packet.data[0])));
      }
      inputQueue.disconnected();
    });
    s.on('connect_error', (error) => {
      connectedRef.current = false;
      setConnected(false);
      if (error.message === 'invalid access key') {
        setAuthError(true);
        s.disconnect();
      }
    });
    s.on('serverInfo', (info: unknown) => {
      if (!isRecord(info) || typeof info.instance !== 'string') return;
      const previous = serverInstanceRef.current;
      serverInstanceRef.current = info.instance;
      clearTimeout(legacyTimer);
      inputQueue.serverInstance(info.instance);
      if (restartRequestedRef.current && previous !== null && previous !== info.instance) {
        restartRequestedRef.current = false;
        setNotice('Server restarted');
      }
    });
    s.on('inputDelivered', (data: unknown) => {
      if (!isRecord(data) || data.page !== PAGE_ID || typeof data.upto !== 'number') return;
      inputQueue.delivered(data.upto, parseFailures(data.failed));
    });
    s.on('itermStatus', (status: unknown) => {
      if (!isRecord(status)) return;
      if (status.ok !== false || typeof status.stalledSince !== 'number') {
        setItermStalledSince(null);
        return;
      }
      // Counted in this phone's clock: the Mac's may differ from it.
      const serverNow = typeof status.now === 'number' ? status.now : status.stalledSince;
      setItermStalledSince(Date.now() - Math.max(0, serverNow - status.stalledSince));
    });

    // Latency every 5s (and at once on connect), and a liveness check: a
    // ping still unanswered after PING_DEAD_MS means a dead socket, which is
    // dropped so it reconnects now rather than after Engine.IO's 45s. Only
    // time the page is visible counts — a suspended page answers nothing.
    let pingInterval: ReturnType<typeof setInterval>;
    let pingSentAt: number | null = null;
    let lastPingAt = 0;
    const doPing = () => {
      const now = Date.now();
      if (pingSentAt !== null) {
        if (now - pingSentAt > PING_DEAD_MS) {
          pingSentAt = null;
          s.io?.engine?.close();
        }
        return;
      }
      if (now - lastPingAt < 5000) return;
      lastPingAt = now;
      pingSentAt = now;
      s.emit('ping', () => {
        // Only this ping's answer clears it; a stale one says nothing now.
        if (pingSentAt === now) pingSentAt = null;
        setLatency(Date.now() - now);
      });
    };
    const forgetPing = () => {
      if (document.visibilityState !== 'visible') return;
      pingSentAt = null;
      inputQueue.resumed();
    };
    document.addEventListener('visibilitychange', forgetPing);
    s.on('connect', () => {
      pingSentAt = null;
      lastPingAt = 0;
      doPing();
      pingInterval = setInterval(doPing, 2000);
    });
    s.on('disconnect', () => clearInterval(pingInterval));
    s.on('screenSize', (size: ScreenSize) => setScreenSize(size));

    s.on('state', (newState: WindowState[]) => {
      setState(newState);
      // Also here, synchronously: a split's acknowledgement may look for its
      // new pane before the render that would update the ref.
      stateRef.current = newState;
      if (newState.length === 0) return;
      applyPendingSplit(newState);

      const frontWin = newState.find(w => w.isFront);

      // A notification deep link names a pane. Every state is a complete
      // snapshot, so the pane is either in this one or already closed.
      if (applyPendingDeepLink(newState)) return;

      // First load — pick front window
      if (!winIdRef.current) {
        const win = frontWin || newState[0];
        setSelectedWinId(win.id);
        const tab = getSelectedTab(win);
        if (tab) { setSelectedTabId(tab.id); setSelectedSessionId(tabDefaultSession(tab) || null); }
        prevFrontRef.current = frontWin?.id || null;
      }
      // Front window changed on Mac — follow it
      else if (frontWin && frontWin.id !== prevFrontRef.current) {
        prevFrontRef.current = frontWin.id;
        setSelectedWinId(frontWin.id);
        const tab = getSelectedTab(frontWin);
        if (tab) { setSelectedTabId(tab.id); setSelectedSessionId(tabDefaultSession(tab) || null); }
      }
      // Same window — sync tab if Mac switched it
      else {
        const win = newState.find(w => w.id === winIdRef.current);
        if (win) {
          const macTab = win.tabs.find(t => t.isSelected);
          if (macTab && macTab.id !== tabIdRef.current) {
            setSelectedTabId(macTab.id);
            setSelectedSessionId(tabDefaultSession(macTab) || null);
          }
          if (!win.tabs.find(t => t.id === tabIdRef.current)) {
            const fb = getSelectedTab(win);
            if (fb) { setSelectedTabId(fb.id); setSelectedSessionId(tabDefaultSession(fb) || null); }
          }
        }
      }

    });

    s.on('content', (data: { sessionId?: string; cached?: boolean } & StyledContent) => {
      const styled: StyledContent = {
        lines: data.lines,
        fg: data.fg,
        bg: data.bg,
        firstLine: data.firstLine,
        availableFirstLine: data.availableFirstLine,
        terminalEnd: data.terminalEnd,
        isLatest: data.isLatest,
      };
      if (data.sessionId) {
        // A frame the server held from earlier, sent to draw something at
        // once: of no use to a page that already has this pane.
        if (data.cached === true && contentCacheRef.current.has(data.sessionId)) return;
        const full = mergeStyledContent(contentCacheRef.current.get(data.sessionId), styled);
        // Not while someone is reading back through it, has it scroll-locked,
        // or is waiting on an older page for it: dropping lines above them
        // would move what they are reading, or orphan the page on its way.
        const primary = data.sessionId === selectedSessionIdRef.current;
        const split = data.sessionId === splitSessionIdRef.current;
        const readingBack = (primary && (!primaryAtBottomRef.current || primaryHistoryLoadingRef.current))
          || (split && (!splitAtBottomRef.current || splitHistoryLoadingRef.current))
          || ((primary || split) && scrollLockedRef.current);
        const merged = readingBack ? full : trimStyledContent(full, MAX_RETAINED_LINES);
        contentCacheRef.current.set(data.sessionId, merged);
        // Refresh the pane map's thumbnails while it's open (cached content for
        // non-viewed panes doesn't otherwise trigger a render).
        if (paneMapOpenRef.current) setPreviewTick(t => t + 1);
        // Update split pane if this is the split session
        if (data.sessionId === splitSessionIdRef.current) {
          if (holdRef.current.split) heldContentRef.current.split = { sessionId: data.sessionId, content: merged };
          else setSplitContent(merged);
        }
        // Only update main content if it's the pane we're viewing (which may be
        // any pane in the selected tab, not just the first).
        if (data.sessionId !== selectedSessionIdRef.current) return;
        if (holdRef.current.primary) heldContentRef.current.primary = { sessionId: data.sessionId, content: merged };
        else setContent(merged);
      } else {
        setContent(styled);
      }
      // Track content changes for long-running command alerts
      lastContentTimeRef.current = Date.now();
      contentChangingRef.current = true;
    });

    s.on('historyContent', (data: { sessionId: string } & StyledContent) => {
      const current = contentCacheRef.current.get(data.sessionId);
      if (current && data.firstLine + data.lines.length < current.firstLine) {
        // It no longer joins what is kept (live output moved on meanwhile);
        // merged, it would replace everything. Scrolling up asks again.
        if (data.sessionId === selectedSessionIdRef.current) primaryHistoryLoadingRef.current = false;
        if (data.sessionId === splitSessionIdRef.current) splitHistoryLoadingRef.current = false;
        return;
      }
      const merged = mergeStyledContent(current, data);
      contentCacheRef.current.set(data.sessionId, merged);

      if (data.sessionId === selectedSessionIdRef.current) {
        const element = contentRef.current;
        if (element) {
          primaryPrependRef.current = { height: element.scrollHeight, top: element.scrollTop };
        }
        primaryHistoryLoadingRef.current = false;
        setContent(merged);
      }
      if (data.sessionId === splitSessionIdRef.current) {
        const element = splitContentRef.current;
        if (element) {
          splitPrependRef.current = { height: element.scrollHeight, top: element.scrollTop };
        }
        splitHistoryLoadingRef.current = false;
        setSplitContent(merged);
      }
    });

    return () => {
      clearInterval(pingInterval);
      clearTimeout(legacyTimer);
      document.removeEventListener('visibilitychange', forgetPing);
      s.disconnect();
    };
  }, [accessKey]);

  // Select the pane a split from this page created, once a state has it.
  // Only touches refs and setters, so the copy the state handler captured
  // stays correct.
  const applyPendingSplit = (snapshot: WindowState[]) => {
    const split = pendingSplitRef.current;
    if (!split) return;
    const exists = snapshot.some(w => w.tabs.some(t => t.sessions.some(p => p.id === split.sessionId)));
    if (!exists) return;
    pendingSplitRef.current = null;
    if (split.pane === 'split') {
      setSplitSessionId(split.sessionId);
      setSplitContent(contentCacheRef.current.get(split.sessionId) || null);
    } else {
      setSelectedSessionId(split.sessionId);
      setContent(contentCacheRef.current.get(split.sessionId) || null);
    }
  };

  // Apply the pane a deep link asked for, against a complete state snapshot.
  // Returns true when the selection was taken over (the caller then skips its
  // own front-window logic). Only touches refs and state setters, so the copy
  // captured by a long-lived socket handler stays correct.
  const applyPendingDeepLink = (snapshot: WindowState[]): boolean => {
    const wanted = pendingDeepLinkRef.current;
    if (!wanted || snapshot.length === 0) return false;
    pendingDeepLinkRef.current = null;
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${stripDeepLink(window.location.hash)}`);
    const target = findSessionLocation(snapshot, wanted);
    if (!target) {
      setNotice('That pane is gone; showing the front window instead.');
      return false;
    }
    setSelectedWinId(target.windowId);
    setSelectedTabId(target.tabId);
    setSelectedSessionId(target.sessionId);
    // Treat its window as the one already being followed, so the front-window
    // sync in the state handler doesn't reset the pane to the tab's default
    // once the focus requested here lands on the Mac.
    prevFrontRef.current = target.windowId;
    socketRef.current?.emit('focus', { windowId: target.windowId, tabIndex: target.tabIndex, sessionId: target.sessionId });
    return true;
  };

  // A deep link can also land on a page that is already open: Safari reuses a
  // tab whose URL differs only by fragment (a hashchange, no reload), and a
  // page restored from the back/forward cache comes back with its old state.
  // Re-read the fragment on both and apply it against the state in hand, or
  // leave it pending for the next state if none has arrived yet.
  useEffect(() => {
    const consume = () => {
      const wanted = deepLinkSessionId(window.location.hash);
      if (!wanted) return;
      pendingDeepLinkRef.current = wanted;
      applyPendingDeepLink(stateRef.current);
    };
    window.addEventListener('hashchange', consume);
    window.addEventListener('pageshow', consume);
    return () => {
      window.removeEventListener('hashchange', consume);
      window.removeEventListener('pageshow', consume);
    };
  }, []);

  const hasSelectionIn = (element: HTMLElement | null): boolean => {
    const selection = document.getSelection();
    if (!element || !selection || selection.isCollapsed || selection.rangeCount === 0) return false;
    return element.contains(selection.anchorNode) && element.contains(selection.focusNode);
  };

  // Recompute whether a pane's live updates are on hold, and apply whatever
  // arrived meanwhile once nothing holds it any more. Only touches refs and
  // state setters, so the copy captured by the listeners below stays correct.
  const reconcileHold = (pane: 'primary' | 'split') => {
    const element = pane === 'primary' ? contentRef.current : splitContentRef.current;
    const hold = touchingRef.current[pane] || hasSelectionIn(element);
    holdRef.current[pane] = hold;
    if (hold) return;
    const held = heldContentRef.current[pane];
    if (!held) return;
    heldContentRef.current[pane] = null;
    const viewing = pane === 'primary' ? selectedSessionIdRef.current : splitSessionIdRef.current;
    if (held.sessionId !== viewing) return;
    (pane === 'primary' ? setContent : setSplitContent)(held.content);
  };

  const beginTouch = (pane: 'primary' | 'split') => {
    touchingRef.current[pane] = true;
    holdRef.current[pane] = true;
  };
  const endTouch = (pane: 'primary' | 'split') => {
    touchingRef.current[pane] = false;
    reconcileHold(pane);
  };

  useEffect(() => {
    const onSelectionChange = () => {
      reconcileHold('primary');
      reconcileHold('split');
      setSelectionHold(hasSelectionIn(contentRef.current) || hasSelectionIn(splitContentRef.current));
    };
    document.addEventListener('selectionchange', onSelectionChange);
    return () => document.removeEventListener('selectionchange', onSelectionChange);
  }, []);

  // Keep screen awake
  useEffect(() => {
    let wakeLock: WakeLockSentinel | null = null;
    const request = async () => {
      try { wakeLock = await navigator.wakeLock.request('screen'); } catch {}
    };
    request();
    const onVisibility = () => { if (document.visibilityState === 'visible') request(); };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      wakeLock?.release();
    };
  }, []);

  // Track orientation
  useEffect(() => {
    const mq = window.matchMedia('(orientation: landscape)');
    const handler = (e: MediaQueryListEvent) => setIsLandscape(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);

  // Keep the viewed pane valid: if it vanishes (closed or unsplit on the Mac),
  // fall back to the selected tab's default pane.
  useEffect(() => {
    const win = state.find(w => w.id === selectedWinId);
    const tab = win?.tabs.find(t => t.id === selectedTabId);
    if (!tab) return;
    if (!selectedSessionId || !tab.sessions.some(sess => sess.id === selectedSessionId)) {
      setSelectedSessionId(tabDefaultSession(tab) || null);
    }
  }, [state, selectedWinId, selectedTabId, selectedSessionId]);

  // Tell the server which sessions we're viewing (main + split) so it streams
  // exactly those, regardless of which pane iTerm has focused on the Mac.
  useEffect(() => {
    const s = socketRef.current;
    if (!s || !connected) return;
    const ids: string[] = [];
    if (selectedSessionId) ids.push(selectedSessionId);
    if (splitMode && splitSessionId) ids.push(splitSessionId);
    s.emit('watch', { sessionIds: ids });
  }, [connected, selectedSessionId, splitMode, splitSessionId]);

  // A newly selected pane starts at its latest output. Thereafter, follow new
  // output only while that pane is already at the bottom, so scrolling up is
  // not undone by a live content update.
  useLayoutEffect(() => {
    primaryAtBottomRef.current = true;
    primaryHistoryLoadingRef.current = false;
  }, [selectedSessionId]);

  useLayoutEffect(() => {
    splitAtBottomRef.current = true;
    splitHistoryLoadingRef.current = false;
  }, [splitSessionId]);

  useLayoutEffect(() => {
    const element = contentRef.current;
    const prepend = primaryPrependRef.current;
    if (element && prepend) {
      element.scrollTop = prepend.top + element.scrollHeight - prepend.height;
      primaryPrependRef.current = null;
    } else if (!scrollLocked && primaryAtBottomRef.current && element) {
      element.scrollTop = element.scrollHeight;
    }
  }, [content, scrollLocked]);

  useLayoutEffect(() => {
    const element = splitContentRef.current;
    const prepend = splitPrependRef.current;
    if (element && prepend) {
      element.scrollTop = prepend.top + element.scrollHeight - prepend.height;
      splitPrependRef.current = null;
    } else if (!scrollLocked && splitAtBottomRef.current && element) {
      element.scrollTop = element.scrollHeight;
    }
  }, [splitContent, scrollLocked]);

  // Long-running command alert: if content was changing and stops for 4s, vibrate
  useEffect(() => {
    if (!alertDone) return;
    if (alertTimerRef.current) clearInterval(alertTimerRef.current);

    alertTimerRef.current = setInterval(() => {
      if (contentChangingRef.current && Date.now() - lastContentTimeRef.current > 4000) {
        contentChangingRef.current = false;
        if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
      }
    }, 1000);

    return () => { if (alertTimerRef.current) clearInterval(alertTimerRef.current); };
  }, [alertDone]);

  // --- Helpers ---
  const getActiveSessionId = () => {
    if (focusedPaneRef.current === 'split' && splitSessionIdRef.current) {
      return splitSessionIdRef.current;
    }
    if (selectedSessionIdRef.current) return selectedSessionIdRef.current;
    const win = stateRef.current.find(w => w.id === winIdRef.current);
    const tab = win?.tabs.find(t => t.id === tabIdRef.current);
    return tab?.sessions[0]?.id;
  };

  // --- Handlers ---
  // Everything typed goes through the input queue: sent now if connected,
  // resent after a reconnect if the server never got it, never twice.
  const handleSend = () => {
    if (!command.trim()) return;
    const broadcasting = broadcastMode && broadcastTargets.size > 0;
    const sessionId = getActiveSessionId();
    if (!broadcasting && !sessionId) {
      // Nothing to send it to yet (no state has arrived). The command stays
      // in the box rather than going to whatever the Mac has in front.
      setNotice('No pane to send to yet');
      return;
    }
    // Save to history
    const h = historyRef.current;
    if (h[h.length - 1] !== command.trim()) h.push(command.trim());
    saveHistory(h);
    historyIdxRef.current = -1;
    savedCommandRef.current = '';
    setTypingLog(prev => appendTypingLog(prev, command.trim(), true));

    if (broadcasting) {
      // Broadcast to all selected windows' active sessions
      const sessionIds: string[] = [];
      for (const winId of broadcastTargets) {
        const win = stateRef.current.find(w => w.id === winId);
        const tab = getSelectedTab(win);
        const sid = tab?.sessions[0]?.id;
        if (sid) sessionIds.push(sid);
      }
      inputQueue.enqueue('broadcast', { command, sessionIds }, command.trim());
    } else {
      inputQueue.enqueue('execute', { sessionId, command }, command.trim());
    }
    // Mark content as "about to change" for alert detection
    contentChangingRef.current = true;
    lastContentTimeRef.current = Date.now();
    setCommand('');
  };

  const handleHistoryNav = (dir: 'up' | 'down') => {
    const h = historyRef.current;
    if (h.length === 0) return;
    if (dir === 'up') {
      if (historyIdxRef.current === -1) savedCommandRef.current = command;
      const next = historyIdxRef.current === -1 ? h.length - 1 : Math.max(0, historyIdxRef.current - 1);
      historyIdxRef.current = next;
      setCommand(h[next]);
    } else {
      if (historyIdxRef.current === -1) return;
      const next = historyIdxRef.current + 1;
      if (next >= h.length) {
        historyIdxRef.current = -1;
        setCommand(savedCommandRef.current);
      } else {
        historyIdxRef.current = next;
        setCommand(h[next]);
      }
    }
  };

  const toggleBroadcastTarget = (winId: string) => {
    setBroadcastTargets(prev => {
      const next = new Set(prev);
      if (next.has(winId)) next.delete(winId); else next.add(winId);
      return next;
    });
  };

  const handleTabClick = (winId: string, tab: Tab) => {
    if (focusedPane === 'split') {
      setSplitWinId(winId);
      setSplitTabId(tab.id);
      const sid = tabDefaultSession(tab);
      if (sid) {
        setSplitSessionId(sid);
        const cached = contentCacheRef.current.get(sid);
        if (cached) setSplitContent(cached);
      }
    } else {
      setSelectedWinId(winId);
      setSelectedTabId(tab.id);
      const sid = tabDefaultSession(tab);
      if (sid) {
        setSelectedSessionId(sid);
        const cached = contentCacheRef.current.get(sid);
        if (cached) setContent(cached);
      }
      socketRef.current?.emit('focus', { windowId: winId, tabIndex: tab.index });
    }
  };

  const handleWindowChange = (winId: string) => {
    if (focusedPane === 'split') {
      setSplitWinId(winId);
      setShowMap(false);
      const win = state.find(w => w.id === winId);
      const tab = getSelectedTab(win);
      if (tab) {
        setSplitTabId(tab.id);
        const sid = tabDefaultSession(tab);
        if (sid) {
          setSplitSessionId(sid);
          const cached = contentCacheRef.current.get(sid);
          if (cached) setSplitContent(cached);
        }
      }
    } else {
      setSelectedWinId(winId);
      setShowMap(false);
      const win = state.find(w => w.id === winId);
      const tab = getSelectedTab(win);
      if (tab) {
        setSelectedTabId(tab.id);
        const sid = tabDefaultSession(tab);
        if (sid) {
          setSelectedSessionId(sid);
          const cached = contentCacheRef.current.get(sid);
          if (cached) setContent(cached);
        }
        socketRef.current?.emit('focus', { windowId: winId, tabIndex: tab.index });
      }
    }
  };

  // The restart endpoint is served by the web client's own process, the one
  // that loaded this page, so it answers even when the Python server is
  // wedged or gone (client/server/restartEndpoint.ts).
  const handleRestartServer = async () => {
    setShowRestartConfirm(false);
    try {
      const response = await fetch(RESTART_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(accessKey ? { key: accessKey } : {}),
      });
      if (response.status === 202) {
        restartRequestedRef.current = true;
        setNotice('Restarting the server — back in a few seconds');
        if (restartTimerRef.current) clearTimeout(restartTimerRef.current);
        restartTimerRef.current = setTimeout(() => {
          if (!restartRequestedRef.current) return;
          restartRequestedRef.current = false;
          setNotice('The server has not come back yet — iTerm2 may not be answering; see .iterm-server.log on the Mac');
        }, RESTART_WAIT_MS);
        return;
      }
      let reason = `HTTP ${response.status}`;
      try {
        const body: unknown = await response.json();
        if (isRecord(body) && typeof body.error === 'string') reason = body.error;
      } catch {}
      setNotice(`Restart refused: ${reason}`);
    } catch {
      setNotice('Could not reach the Mac to restart the server');
    }
  };

  const handleNewTab = () => socketRef.current?.emit('newTab');
  const handleCloseTab = () => socketRef.current?.emit('closeTab');
  // vertical=true divider (side-by-side panes) mirrors iTerm2's own Cmd+D
  // "Split Vertically"; vertical=false (stacked panes) mirrors Cmd+Shift+D
  // "Split Horizontally".
  //
  // The server answers with the new pane, which is then selected in whichever
  // view the split was made from.
  const handleSplitPane = (sessionId: string | undefined, vertical: boolean, pane: 'primary' | 'split' = 'primary') => {
    const s = socketRef.current;
    if (!s || !sessionId) return;
    s.emit('splitPane', { sessionId, vertical }, (ack: unknown) => {
      if (!isRecord(ack)) return;
      if (typeof ack.sessionId === 'string') {
        pendingSplitRef.current = { sessionId: ack.sessionId, pane };
        applyPendingSplit(stateRef.current);
      } else if (typeof ack.error === 'string') {
        setNotice(`Could not split: ${ack.error}`);
      }
    });
  };

  const handleSplitWindowChange = (winId: string) => {
    setSplitWinId(winId);
    setShowSplitMap(false);
    const win = state.find(w => w.id === winId);
    const tab = getSelectedTab(win);
    if (tab) {
      setSplitTabId(tab.id);
      const sid = tabDefaultSession(tab);
      if (sid) {
        setSplitSessionId(sid);
        setSplitContent(contentCacheRef.current.get(sid) || null);
      }
    }
    setFocusedPane('split');
  };

  const handleSplitTabClick = (tab: Tab) => {
    setSplitTabId(tab.id);
    const sid = tabDefaultSession(tab);
    if (sid) {
      setSplitSessionId(sid);
      const cached = contentCacheRef.current.get(sid);
      if (cached) setSplitContent(cached);
    }
    setFocusedPane('split');
  };

  // Open the spatial pane map and request a content snapshot for every pane so
  // the thumbnails populate (panes we aren't actively viewing aren't streamed).
  const handleOpenPaneMap = () => {
    setShowPaneMap(true);
    const ids = primaryPanes.map(p => p.id).filter(Boolean);
    if (ids.length) socketRef.current?.emit('getAllContent', { sessionIds: ids });
  };

  // Closes the pane the map has selected — the one drawn in the confirm
  // colour. The map is dismissed straight after: the tab's layout is about to
  // change under it, and the server's next state picks the replacement pane.
  const handleClosePane = () => {
    const sessionId = selectedSessionId;
    setPaneCloseArmed(false);
    setShowPaneMap(false);
    if (!sessionId) return;
    socketRef.current?.emit('closePane', { sessionId });
  };

  const loadEarlierContent = (
    pane: 'primary' | 'split',
    sessionId: string | null,
    styled: StyledContent | null,
    element: HTMLDivElement,
  ) => {
    if (!sessionId || !styled || element.scrollTop > 80) return;
    if (styled.firstLine <= styled.availableFirstLine) return;
    const loadingRef = pane === 'primary' ? primaryHistoryLoadingRef : splitHistoryLoadingRef;
    if (loadingRef.current) return;
    loadingRef.current = true;
    socketRef.current?.emit('getEarlierContent', {
      sessionId,
      beforeLine: styled.firstLine,
    });
  };

  // Switch which pane (split session) of the current tab fills the primary view.
  const handlePaneSelect = (sessionId: string) => {
    setSelectedSessionId(sessionId);
    setFocusedPane('primary');
    setShowPaneMap(false);
    setContent(contentCacheRef.current.get(sessionId) || null);
  };

  const longPressRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handleTabLongPress = (tab: Tab) => {
    const sid = tab.sessions[0]?.id;
    if (!sid) return;
    const name = prompt('Rename tab:', tab.sessions[0]?.name || '');
    if (name !== null && name.trim()) {
      socketRef.current?.emit('renameSession', { sessionId: sid, name: name.trim() });
    }
  };

  const sendSpecialKey = (keys: string) => {
    const sessionId = getActiveSessionId();
    if (!sessionId) {
      setNotice('No pane to send to yet');
      return;
    }
    inputQueue.enqueue('sendKeys', { sessionId, keys }, describeKeys(keys));
  };

  const toggleDirectInput = () => {
    if (directInputMode) {
      setDirectInputMode(false);
      setDirectInputValue('');
      commandInputRef.current?.blur();
      return;
    }

    // The input is already mounted, so this focus remains inside the tap event.
    // iOS otherwise may refuse to open its keyboard after a state-driven render.
    setDirectInputMode(true);
    setDirectInputValue('');
    commandInputRef.current?.focus({ preventScroll: true });
  };

  const toggleSmartTyping = () => {
    setSmartTyping(prev => {
      const next = !prev;
      try { localStorage.setItem(SMART_TYPING_KEY, next ? '1' : '0'); } catch {}
      return next;
    });
  };

  const handleInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    if (!directInputMode) {
      setCommand(event.currentTarget.value);
      historyIdxRef.current = -1;
      return;
    }

    const inputEvent = event.nativeEvent as InputEvent;
    const value = event.currentTarget.value;
    if (inputEvent.isComposing) {
      // Keep the in-progress value so terminal refreshes do not cancel iOS
      // composition (accented characters, dictation, and non-Latin input).
      setDirectInputValue(value);
      return;
    }

    if (!value) {
      // The box is reset to empty after every forwarded key, so it's
      // normally already empty — backspacing/deleting on an already-empty
      // box changes nothing, leaving no value to infer a delete from.
      // inputType is the only signal available for that case.
      if (inputEvent.inputType === 'deleteContentBackward') {
        sendSpecialKey(DIRECT_INPUT_KEYS.Backspace);
      } else if (inputEvent.inputType === 'deleteContentForward') {
        sendSpecialKey(DIRECT_INPUT_KEYS.Delete);
      }
      return;
    }
    sendSpecialKey(value);
    setTypingLog(prev => appendTypingLog(prev, value, false));
    setDirectInputValue('');
  };

  const handleInputKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!directInputMode) {
      if (event.key === 'Enter') handleSend();
      else if (event.key === 'ArrowUp') { event.preventDefault(); handleHistoryNav('up'); }
      else if (event.key === 'ArrowDown') { event.preventDefault(); handleHistoryNav('down'); }
      return;
    }

    if (event.nativeEvent.isComposing) return;
    const keys = DIRECT_INPUT_KEYS[event.key];
    if (!keys) return;
    event.preventDefault();
    sendSpecialKey(keys);
    setDirectInputValue('');
  };

  const handlePaste = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        sendSpecialKey(text);
        setTypingLog(prev => appendTypingLog(prev, text, true));
      }
    } catch {}
  };

  const handleSplitDrag = (e: React.TouchEvent) => {
    const container = splitContainerRef.current;
    if (!container) return;
    const touch = e.touches[0];
    const rect = container.getBoundingClientRect();
    let ratio: number;
    if (isLandscape) {
      ratio = (touch.clientX - rect.left) / rect.width;
    } else {
      ratio = (touch.clientY - rect.top) / rect.height;
    }
    setSplitRatio(Math.min(0.8, Math.max(0.2, ratio)));
  };

  const handleCopy = async () => {
    try {
      const styled = focusedPane === 'split' ? splitContent : content;
      const text = styled ? styled.lines.map(runs => runs.map(r => r.t).join('')).join('\n') : '';
      await navigator.clipboard.writeText(text);
    } catch {}
  };

  const activeWindow = state.find(w => w.id === selectedWinId);
  const splitWindow = state.find(w => w.id === splitWinId);
  const tabBarWindow = (splitMode && focusedPane === 'split') ? splitWindow : activeWindow;
  const tabBarSelectedTabId = (splitMode && focusedPane === 'split') ? splitTabId : selectedTabId;
  const tabCount = tabBarWindow?.tabs.length || 0;
  const splitTabCount = splitWindow?.tabs.length || 0;
  // Panes (split sessions) of the primary tab — drives the per-tab pane switcher.
  const primaryTab = activeWindow?.tabs.find(t => t.id === selectedTabId);
  const primaryPanes = primaryTab?.sessions || [];
  // The pane the map's close button acts on, identified to the user the same
  // way the map labels it: by its position in the grid.
  const selectedPaneIndex = primaryPanes.findIndex(pane => pane.id === selectedSessionId);
  const selectedPaneNumber = selectedPaneIndex >= 0 ? selectedPaneIndex + 1 : null;
  // Pane names are whatever the running program set the title to, which can
  // be a whole command line; the question only needs enough to recognise it.
  const selectedPaneName = (selectedPaneIndex >= 0 ? primaryPanes[selectedPaneIndex].name || '' : '').slice(0, 40);

  // All other sessions for split picker (exclude current active)
  const allSessions = useMemo(() => {
    const activeTab = activeWindow?.tabs.find(t => t.id === selectedTabId);
    const activeSid = activeTab?.sessions[0]?.id;
    return state.flatMap(w => w.tabs.map(t => ({
      sessionId: t.sessions[0]?.id,
      name: t.title || t.sessions[0]?.name || `Tab ${t.index}`,
    }))).filter(s => s.sessionId && s.sessionId !== activeSid);
  }, [state, activeWindow, selectedTabId]);

  const oldestPending = pendingInputs.length ? Math.min(...pendingInputs.map(input => input.queuedAt)) : null;
  const showPendingBadge = oldestPending !== null && clock - oldestPending >= PENDING_BADGE_DELAY_MS;
  const itermStalled = itermStalledSince !== null;

  const handleAccessKeySubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const key = accessKeyInput.trim();
    if (!key) return;
    try { rememberAccessKey(key); } catch {}
    setState([]);
    setContent(null);
    setSplitContent(null);
    contentCacheRef.current.clear();
    setAuthError(false);
    setAccessKeyInput('');
    setAccessKey(key);
  };

  return (
    <div className="flex flex-col bg-[#0a0a0a] font-mono overflow-hidden relative pt-safe pb-safe-root pl-safe pr-safe" style={{ height: '100dvh' }}>

      {/* ── Access Key Overlay ── */}
      {authError && (
        <div
          className="fixed inset-0 z-[110] flex items-center justify-center px-5 select-none"
          style={{ backgroundColor: 'rgba(0,0,0,0.94)', backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)' }}
        >
          <form
            onSubmit={handleAccessKeySubmit}
            className="w-full max-w-sm rounded-xl border border-zinc-800 bg-[#111113] p-5 shadow-2xl"
          >
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-emerald-500/30 bg-emerald-500/10">
                <KeyRound className="h-5 w-5 text-emerald-400" />
              </div>
              <div>
                <div className="text-[12px] font-bold tracking-[0.14em] text-zinc-200">ACCESS KEY REQUIRED</div>
                <div className="mt-1 text-[10px] text-zinc-600">
                  {accessKey ? 'That key was not accepted.' : 'Scan the server QR code or enter its key.'}
                </div>
              </div>
            </div>
            <input
              type="password"
              value={accessKeyInput}
              onChange={(event) => setAccessKeyInput(event.target.value)}
              placeholder="Shared access key"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              className="h-12 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 text-[12px] text-zinc-200 outline-none placeholder:text-zinc-700 focus:border-emerald-500/60"
            />
            <button
              type="submit"
              disabled={!accessKeyInput.trim()}
              className="mt-3 h-11 w-full rounded-lg bg-emerald-500 text-[11px] font-bold tracking-[0.14em] text-black disabled:bg-zinc-800 disabled:text-zinc-600"
            >
              CONNECT
            </button>
          </form>
        </div>
      )}

      {/* ── Reconnect banner ── */}
      {/* Non-blocking on purpose: the terminal stays readable and keystrokes
          typed meanwhile are queued by Socket.IO and sent on reconnect. */}
      {!authError && reconnectBanner && (
        <div
          role="status"
          className="fixed left-1/2 z-[100] flex -translate-x-1/2 items-center gap-2 rounded-full border border-red-500/40 bg-zinc-900/95 px-4 py-1.5 text-[10px] font-bold tracking-[0.15em] text-red-300 shadow-xl select-none pointer-events-none"
          style={{ top: 'calc(env(safe-area-inset-top, 0px) + 3.25rem)' }}
        >
          <WifiOff className="h-3.5 w-3.5 animate-pulse" />
          RECONNECTING
        </div>
      )}

      {/* ── iTerm2 stall banner ── */}
      {/* The server reports when iTerm2 stops answering its API (a Mac short
          of memory has stalled it for over a minute). Said plainly, because
          the alternative looks like a broken phone: output frozen and typing
          apparently ignored. Typing is queued meanwhile, not lost. */}
      {!authError && connected && itermStalled && (
        <div
          role="status"
          className="fixed left-1/2 z-[100] flex -translate-x-1/2 items-center gap-2 rounded-full border border-amber-500/40 bg-zinc-900/95 px-4 py-1.5 text-[10px] font-bold tracking-[0.15em] text-amber-300 shadow-xl select-none pointer-events-none whitespace-nowrap"
          style={{ top: 'calc(env(safe-area-inset-top, 0px) + 3.25rem)' }}
        >
          <TriangleAlert className="h-3.5 w-3.5 animate-pulse" />
          ITERM2 NOT RESPONDING · {formatElapsed(clock - (itermStalledSince ?? clock))}
          {pendingInputs.length > 0 && <span className="text-amber-400/70">· TYPING QUEUED</span>}
        </div>
      )}

      {notice && (
        <div
          className="fixed left-1/2 z-[120] -translate-x-1/2 max-w-[92vw] rounded-2xl border border-zinc-700 bg-zinc-900/95 px-4 py-2 text-center text-[11px] text-zinc-300 shadow-xl select-none"
          style={{ top: 'calc(env(safe-area-inset-top, 0px) + 3.25rem)' }}
        >
          {notice}
        </div>
      )}

      {/* ── Restart confirmation ── */}
      {showRestartConfirm && (
        <div
          className="fixed inset-0 z-[105] flex items-center justify-center px-5 select-none"
          onClick={() => setShowRestartConfirm(false)}
          style={{ backgroundColor: 'rgba(0,0,0,0.85)', backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)' }}
        >
          <div
            role="dialog"
            aria-label="Restart the server"
            className="w-full max-w-sm rounded-xl border border-zinc-800 bg-[#111113] p-5 shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-3 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-amber-500/30 bg-amber-500/10">
                <RotateCw className="h-5 w-5 text-amber-400" />
              </div>
              <div className="text-[12px] font-bold tracking-[0.14em] text-zinc-200">RESTART THE SERVER?</div>
            </div>
            <p className="text-[11px] leading-relaxed text-zinc-400">
              Phones reconnect within a few seconds and anything typed meanwhile is sent once it is back. Nothing running in iTerm2 is touched.
            </p>
            {itermStalled && (
              <p className="mt-2 text-[11px] leading-relaxed text-amber-400/90">
                iTerm2 itself is not answering right now. A restart will not fix that, and the server cannot come back until iTerm2 answers.
              </p>
            )}
            <div className="mt-4 flex gap-2">
              <button
                onClick={() => setShowRestartConfirm(false)}
                className="h-11 flex-1 rounded-lg border border-zinc-700 text-[11px] font-bold tracking-[0.14em] text-zinc-400 active:scale-95"
              >
                CANCEL
              </button>
              <button
                onClick={() => { void handleRestartServer(); }}
                className="h-11 flex-1 rounded-lg bg-amber-500 text-[11px] font-bold tracking-[0.14em] text-black active:scale-95"
              >
                RESTART
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Top Bar ── */}
      <div className={`flex items-center justify-between bg-zinc-900/80 border-b border-zinc-800 z-50 flex-shrink-0 select-none ${isLandscape ? 'px-2 h-8' : 'px-4 h-11'}`}>
        <div className="flex items-center gap-2.5">
          <div className="relative">
            <div
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: connected ? '#34d399' : '#ef4444' }}
            />
            {connected && (
              <div className="absolute inset-0 w-2 h-2 rounded-full bg-emerald-400 animate-ping opacity-25" />
            )}
          </div>
          <span className="text-[11px] font-bold tracking-[0.2em] text-zinc-500">iTERM</span>
          {connected && latency !== null && (
            <span
              className="text-[10px] font-bold tabular-nums"
              style={{ color: latency < 50 ? '#34d399' : latency < 150 ? '#fbbf24' : '#f87171' }}
            >
              {latency}ms
            </span>
          )}
          {selectionHold && (
            <span
              className="text-[8px] font-bold tracking-[0.16em] text-amber-400"
              title="Live updates are paused while terminal text is selected"
            >
              PAUSED
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {/* Server restart */}
          <button
            onClick={() => setShowRestartConfirm(true)}
            aria-label="Restart the server"
            title="Restart the server"
            className={`flex flex-col items-center gap-0.5 rounded-md border transition-all active:scale-95 ${isLandscape ? 'p-1' : 'p-1.5'}`}
            style={{
              color: itermStalled || !connected ? '#fbbf24' : '#52525b',
              borderColor: '#3f3f46',
              backgroundColor: 'transparent',
            }}
          >
            <RotateCw className={isLandscape ? 'w-3 h-3' : 'w-3.5 h-3.5'} />
          </button>
          {/* Alert toggle */}
          <button
            onClick={() => setAlertDone(!alertDone)}
            aria-label="Alert when output stops"
            aria-pressed={alertDone}
            className={`flex flex-col items-center gap-0.5 rounded-md border transition-all active:scale-95 ${isLandscape ? 'p-1' : 'p-1.5'}`}
            style={{
              color: alertDone ? '#fbbf24' : '#52525b',
              borderColor: alertDone ? '#fbbf2440' : '#3f3f46',
              backgroundColor: alertDone ? '#fbbf2415' : 'transparent',
            }}
          >
            <Bell className={isLandscape ? 'w-3 h-3' : 'w-3.5 h-3.5'} />
            {!isLandscape && alertDone && <span className="text-[7px] font-bold leading-none tracking-wider">ALERT</span>}
          </button>
          {/* Scroll lock */}
          <button
            onClick={() => setScrollLocked(!scrollLocked)}
            aria-label="Lock scroll position"
            aria-pressed={scrollLocked}
            className={`flex flex-col items-center gap-0.5 rounded-md border transition-all active:scale-95 ${isLandscape ? 'p-1' : 'p-1.5'}`}
            style={{
              color: scrollLocked ? '#f87171' : '#52525b',
              borderColor: scrollLocked ? '#f8717140' : '#3f3f46',
              backgroundColor: scrollLocked ? '#f8717115' : 'transparent',
            }}
          >
            {scrollLocked ? <Lock className={isLandscape ? 'w-3 h-3' : 'w-3.5 h-3.5'} /> : <Unlock className={isLandscape ? 'w-3 h-3' : 'w-3.5 h-3.5'} />}
            {!isLandscape && scrollLocked && <span className="text-[7px] font-bold leading-none tracking-wider">SCROLL</span>}
          </button>
          {/* Broadcast mode */}
          <button
            onClick={() => {
              if (broadcastMode) { setBroadcastMode(false); setBroadcastTargets(new Set()); }
              else { setBroadcastMode(true); setBroadcastTargets(new Set(state.map(w => w.id))); }
            }}
            aria-label="Broadcast to windows"
            aria-pressed={broadcastMode}
            className={`flex flex-col items-center gap-0.5 rounded-md border transition-all active:scale-95 ${isLandscape ? 'p-1' : 'p-1.5'}`}
            style={{
              color: broadcastMode ? BROADCAST_COLOR : '#52525b',
              borderColor: broadcastMode ? BROADCAST_COLOR + '50' : '#3f3f46',
              backgroundColor: broadcastMode ? BROADCAST_COLOR + '15' : 'transparent',
            }}
          >
            <Radio className={isLandscape ? 'w-3 h-3' : 'w-3.5 h-3.5'} />
            {!isLandscape && broadcastMode && <span className="text-[7px] font-bold leading-none tracking-wider">CAST</span>}
          </button>
          {/* Split pane toggle */}
          <button
            onClick={() => {
              if (splitMode) { setSplitMode(false); setSplitSessionId(null); setSplitContent(null); setFocusedPane('primary'); setSplitWinId(null); setSplitTabId(null); }
              else if (allSessions.length > 0) {
                // Find the first session that's not the primary active one
                const firstOther = allSessions[0];
                const sid = firstOther.sessionId!;
                // Find which window/tab owns this session
                let foundWin: string | null = null;
                let foundTab: string | null = null;
                for (const w of state) {
                  for (const t of w.tabs) {
                    if (t.sessions[0]?.id === sid) { foundWin = w.id; foundTab = t.id; break; }
                  }
                  if (foundWin) break;
                }
                setSplitMode(true);
                setSplitSessionId(sid);
                setSplitWinId(foundWin);
                setSplitTabId(foundTab);
                setSplitContent(contentCacheRef.current.get(sid) || null);
              }
            }}
            aria-label="Show a second pane"
            aria-pressed={splitMode}
            className={`flex flex-col items-center gap-0.5 rounded-md border transition-all active:scale-95 ${isLandscape ? 'p-1' : 'p-1.5'}`}
            style={{
              color: splitMode ? '#38bdf8' : '#52525b',
              borderColor: splitMode ? '#38bdf840' : '#3f3f46',
              backgroundColor: splitMode ? '#38bdf815' : 'transparent',
              opacity: allSessions.length === 0 && !splitMode ? 0.3 : 1,
            }}
          >
            <Columns2 className={isLandscape ? 'w-3 h-3' : 'w-3.5 h-3.5'} />
            {!isLandscape && splitMode && <span className="text-[7px] font-bold leading-none tracking-wider">SPLIT</span>}
          </button>
          <button
            onClick={() => state.length > 1 && screenSize && setShowMap(!showMap)}
            aria-label="Choose window"
            className="text-[10px] font-bold tracking-wide px-2.5 py-1 rounded-md border transition-all active:scale-95"
            style={{
              color: showMap ? '#000' : '#71717a',
              backgroundColor: showMap ? ACCENT : 'transparent',
              borderColor: showMap ? ACCENT : '#3f3f46',
              opacity: state.length <= 1 ? 0.3 : 1,
            }}
          >
            {state.length} win
          </button>
        </div>
      </div>

      {/* ── Window Map (fullscreen overlay) ── */}
      {showMap && state.length > 1 && screenSize && (
        <div
          className="fixed inset-0 z-50 flex flex-col items-center justify-center select-none"
          onClick={() => setShowMap(false)}
          style={{ backgroundColor: 'rgba(0,0,0,0.85)', backdropFilter: 'blur(12px)', WebkitBackdropFilter: 'blur(12px)' }}
        >
          <span className="text-[11px] font-bold tracking-[0.2em] text-zinc-500 mb-4">
            {broadcastMode ? 'SELECT BROADCAST TARGETS' : 'SELECT WINDOW'}
          </span>
          <div
            className="relative rounded-xl bg-zinc-800/20 border border-zinc-700/30 overflow-hidden w-[85vw]"
            onClick={(e) => e.stopPropagation()}
            style={{
              aspectRatio: `${screenSize.width} / ${screenSize.height}`,
              maxHeight: '45vh',
            }}
          >
            {state.map((win, idx) => {
              if (!win.bounds) return null;
              const isSelected = broadcastMode ? broadcastTargets.has(win.id) : win.id === selectedWinId;
              return (
                <button
                  key={win.id}
                  onClick={() => broadcastMode ? toggleBroadcastTarget(win.id) : handleWindowChange(win.id)}
                  className="absolute rounded-[4px] border transition-all active:opacity-70 flex items-center justify-center"
                  style={{
                    left: `${(win.bounds.x / screenSize.width) * 100}%`,
                    top: `${(win.bounds.y / screenSize.height) * 100}%`,
                    width: `${(win.bounds.w / screenSize.width) * 100}%`,
                    height: `${(win.bounds.h / screenSize.height) * 100}%`,
                    backgroundColor: isSelected ? (broadcastMode ? BROADCAST_COLOR + '25' : ACCENT + '25') : 'rgba(39,39,42,0.5)',
                    borderColor: isSelected ? (broadcastMode ? BROADCAST_COLOR : ACCENT) : '#3f3f46',
                    borderWidth: isSelected ? '2px' : '1px',
                    boxShadow: isSelected ? `0 0 16px ${broadcastMode ? 'rgba(129,140,248,0.25)' : GLOW}` : 'none',
                  }}
                >
                  <span
                    className="font-bold leading-none"
                    style={{ color: isSelected ? (broadcastMode ? BROADCAST_COLOR : ACCENT) : '#71717a', fontSize: '14px' }}
                  >
                    {idx + 1}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Split Window Map (fullscreen overlay) ── */}
      {showSplitMap && state.length > 1 && screenSize && (
        <div
          className="fixed inset-0 z-50 flex flex-col items-center justify-center select-none"
          onClick={() => setShowSplitMap(false)}
          style={{ backgroundColor: 'rgba(0,0,0,0.85)', backdropFilter: 'blur(12px)', WebkitBackdropFilter: 'blur(12px)' }}
        >
          <span className="text-[11px] font-bold tracking-[0.2em] text-zinc-500 mb-4">
            SELECT SPLIT WINDOW
          </span>
          <div
            className="relative rounded-xl bg-zinc-800/20 border border-zinc-700/30 overflow-hidden w-[85vw]"
            onClick={(e) => e.stopPropagation()}
            style={{
              aspectRatio: `${screenSize.width} / ${screenSize.height}`,
              maxHeight: '45vh',
            }}
          >
            {state.map((win, idx) => {
              if (!win.bounds) return null;
              const isSelected = win.id === splitWinId;
              return (
                <button
                  key={win.id}
                  onClick={() => handleSplitWindowChange(win.id)}
                  className="absolute rounded-[4px] border transition-all active:opacity-70 flex items-center justify-center"
                  style={{
                    left: `${(win.bounds.x / screenSize.width) * 100}%`,
                    top: `${(win.bounds.y / screenSize.height) * 100}%`,
                    width: `${(win.bounds.w / screenSize.width) * 100}%`,
                    height: `${(win.bounds.h / screenSize.height) * 100}%`,
                    backgroundColor: isSelected ? '#38bdf825' : 'rgba(39,39,42,0.5)',
                    borderColor: isSelected ? '#38bdf8' : '#3f3f46',
                    borderWidth: isSelected ? '2px' : '1px',
                    boxShadow: isSelected ? '0 0 16px rgba(56,189,248,0.25)' : 'none',
                  }}
                >
                  <span
                    className="font-bold leading-none"
                    style={{ color: isSelected ? '#38bdf8' : '#71717a', fontSize: '14px' }}
                  >
                    {idx + 1}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Pane Layout Map (spatial pane switcher) ── */}
      {showPaneMap && primaryPanes.length > 0 && (
        <div
          className="fixed inset-0 z-[60] flex flex-col items-center justify-center select-none"
          onClick={() => setShowPaneMap(false)}
          style={{ backgroundColor: 'rgba(0,0,0,0.85)', backdropFilter: 'blur(12px)', WebkitBackdropFilter: 'blur(12px)' }}
        >
          <div className="mb-4 px-6 text-center">
            <div className="text-[11px] font-bold tracking-[0.2em] text-zinc-500">
              SELECT PANE — {primaryTab?.title || `Tab ${primaryTab?.index}`}
            </div>
            {primaryTab?.maximized && (
              <div className="text-[9px] text-amber-500/70 tracking-wider mt-1">
                a pane is maximized in iTerm — shown as a grid
              </div>
            )}
          </div>
          <div
            className="relative rounded-xl bg-zinc-800/20 border border-zinc-700/30 overflow-hidden"
            onClick={(e) => e.stopPropagation()}
            style={{
              // Preserve the tab's real aspect ratio while fitting both axes:
              // width is capped so height (= width / aspect) never exceeds ~56vh.
              aspectRatio: `${primaryTab?.aspect || 1.6}`,
              width: `min(88vw, ${(56 * (primaryTab?.aspect || 1.6)).toFixed(1)}vh)`,
            }}
          >
            {primaryPanes.map((pane, idx) => {
              const r = pane.rect;
              if (!r) return null;
              const isSel = pane.id === selectedSessionId;
              const doomed = isSel && paneCloseArmed;
              const mark = doomed ? DANGER : ACCENT;
              return (
                <button
                  key={pane.id}
                  // While a close is armed the grid stops being a switcher:
                  // tapping anywhere in it backs out, so a stray tap cannot
                  // leave the confirmation hanging over a different pane.
                  onClick={() => paneCloseArmed ? setPaneCloseArmed(false) : handlePaneSelect(pane.id)}
                  aria-label={`Pane ${idx + 1}${pane.name ? ` ${pane.name}` : ''}`}
                  className="absolute rounded-[5px] border transition-all active:opacity-80 overflow-hidden"
                  style={{
                    left: `calc(${r.x * 100}% + 2px)`,
                    top: `calc(${r.y * 100}% + 2px)`,
                    width: `calc(${r.w * 100}% - 4px)`,
                    height: `calc(${r.h * 100}% - 4px)`,
                    backgroundColor: 'rgba(39,39,42,0.6)',
                    borderColor: isSel ? mark : '#3f3f46',
                    borderWidth: isSel ? '2px' : '1px',
                    boxShadow: isSel ? `0 0 16px ${doomed ? DANGER_GLOW : GLOW}` : 'none',
                  }}
                >
                  <PanePreview content={contentCacheRef.current.get(pane.id)} />
                  {isSel && <div className="absolute inset-0 pointer-events-none" style={{ backgroundColor: mark + '20' }} />}
                  <span
                    className="absolute top-0.5 left-0.5 z-10 flex items-center gap-1 max-w-[calc(100%-4px)] px-1 py-0.5 rounded"
                    style={{ backgroundColor: isSel ? mark : 'rgba(0,0,0,0.65)' }}
                  >
                    <span className="font-bold leading-none" style={{ fontSize: '11px', color: isSel ? '#000' : ACCENT }}>
                      {idx + 1}
                    </span>
                    {pane.name && (
                      <span className="leading-none truncate" style={{ fontSize: '8px', color: isSel ? '#000' : '#d4d4d8' }}>
                        {pane.name}
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
          {/* The footer is inside the dismiss-on-tap backdrop, so it has to
              stop its own clicks from bubbling out and closing the map. */}
          <div className="mt-3 flex flex-col items-center gap-2 px-4" onClick={(e) => e.stopPropagation()}>
            {paneCloseArmed ? (
              <>
                <span className="text-[10px] tracking-wider text-center" style={{ color: DANGER }}>
                  close pane {selectedPaneNumber}{selectedPaneName ? ` “${selectedPaneName}”` : ''}?
                </span>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setPaneCloseArmed(false)}
                    className="px-4 h-9 rounded-lg border border-zinc-700 text-[10px] font-bold tracking-wider text-zinc-400 whitespace-nowrap active:scale-95 transition-all"
                  >
                    KEEP
                  </button>
                  <button
                    onClick={handleClosePane}
                    className="px-4 h-9 rounded-lg border text-[10px] font-bold tracking-wider whitespace-nowrap active:scale-95 transition-all"
                    style={{ borderColor: DANGER, backgroundColor: DANGER + '20', color: DANGER }}
                  >
                    CLOSE
                  </button>
                </div>
              </>
            ) : (
              <>
                <span className="text-[10px] text-zinc-600 tracking-wider">
                  {primaryPanes.length > 1 ? 'tap a pane · tap outside to close' : 'tap outside to close'}
                </span>
                {/* New panes from the phone: split the selected pane the way
                    iTerm's own Split Vertically (side by side, ⌘D) and Split
                    Horizontally (stacked, ⇧⌘D) do. The new pane is selected
                    when it appears, and the map stays open to show it. */}
                {selectedPaneNumber !== null && (
                  <div className="flex flex-col items-center gap-1.5" role="group" aria-label={`Split pane ${selectedPaneNumber}`}>
                    <span className="text-[9px] font-bold tracking-[0.18em] text-zinc-500">SPLIT PANE {selectedPaneNumber}</span>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleSplitPane(selectedSessionId ?? undefined, true)}
                        aria-label={`Split pane ${selectedPaneNumber} vertically`}
                        title="Side by side (iTerm's Split Vertically)"
                        className="flex items-center gap-1.5 px-4 h-9 rounded-lg border text-[10px] font-bold tracking-wider whitespace-nowrap active:scale-95 transition-all"
                        style={{ borderColor: ACCENT + '40', backgroundColor: ACCENT + '12', color: ACCENT }}
                      >
                        <Columns2 className="w-3.5 h-3.5 flex-shrink-0" />
                        VERTICAL
                      </button>
                      <button
                        onClick={() => handleSplitPane(selectedSessionId ?? undefined, false)}
                        aria-label={`Split pane ${selectedPaneNumber} horizontally`}
                        title="Stacked (iTerm's Split Horizontally)"
                        className="flex items-center gap-1.5 px-4 h-9 rounded-lg border text-[10px] font-bold tracking-wider whitespace-nowrap active:scale-95 transition-all"
                        style={{ borderColor: ACCENT + '40', backgroundColor: ACCENT + '12', color: ACCENT }}
                      >
                        <Rows2 className="w-3.5 h-3.5 flex-shrink-0" />
                        HORIZONTAL
                      </button>
                    </div>
                  </div>
                )}
                {selectedPaneNumber !== null && (
                  <button
                    onClick={() => setPaneCloseArmed(true)}
                    className="flex items-center gap-1.5 px-4 h-9 rounded-lg border text-[10px] font-bold tracking-wider whitespace-nowrap active:scale-95 transition-all"
                    style={{ borderColor: DANGER + '40', backgroundColor: DANGER + '12', color: DANGER }}
                  >
                    <X className="w-3 h-3 flex-shrink-0" />
                    CLOSE PANE {selectedPaneNumber}
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Tab Bar ── */}
      <div className="flex items-center bg-[#0f0f0f] border-b border-zinc-800/60 flex-shrink-0 overflow-x-auto no-scrollbar select-none">
        <button
          onClick={() => setTabListTarget(tabListTarget === 'tabBar' ? null : 'tabBar')}
          className={`sticky left-0 z-10 flex flex-shrink-0 items-center justify-center border-r border-zinc-700/50 bg-[#0f0f0f] text-zinc-500 active:text-zinc-200 ${isLandscape ? 'w-9 min-h-[32px]' : 'w-11 min-h-[44px]'}`}
          aria-label="Show tabs as a list"
          title="Show tabs as a list"
        >
          <ListIcon className="w-4 h-4" />
        </button>
        {tabBarWindow?.tabs.map((tab) => {
          const isActive = tabBarSelectedTabId === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => handleTabClick(tabBarWindow.id, tab)}
              onTouchStart={() => { longPressRef.current = setTimeout(() => handleTabLongPress(tab), 500); }}
              onTouchEnd={() => { if (longPressRef.current) clearTimeout(longPressRef.current); }}
              onTouchMove={() => { if (longPressRef.current) clearTimeout(longPressRef.current); }}
              className={`flex items-center gap-2 text-[11px] tracking-wider transition-all relative flex-shrink-0 active:opacity-70 border-r border-zinc-700/50 ${isLandscape ? 'px-3 min-h-[32px]' : 'px-5 min-h-[44px]'}`}
              style={{
                color: isActive ? (splitMode && focusedPane === 'split' ? '#38bdf8' : ACCENT) : '#52525b',
                backgroundColor: isActive ? (splitMode && focusedPane === 'split' ? '#38bdf810' : ACCENT + '10') : 'transparent',
              }}
            >
              {isActive && (
                <div
                  className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                  style={{ backgroundColor: splitMode && focusedPane === 'split' ? '#38bdf8' : ACCENT }}
                />
              )}
              <span className="font-semibold whitespace-nowrap">
                {tab.title || tab.sessions[0]?.name || `Tab ${tab.index}`}
              </span>
              {isActive && (
                <div
                  className="absolute bottom-0 left-3 right-3 h-[2px] rounded-full"
                  style={{ backgroundColor: splitMode && focusedPane === 'split' ? '#38bdf8' : ACCENT }}
                />
              )}
            </button>
          );
        })}

        <div className="flex items-center flex-shrink-0 ml-auto border-l border-zinc-800/60">
          <span className="text-[10px] text-zinc-600 font-bold px-2.5 tabular-nums">
            {tabCount}
          </span>
          <button
            onClick={() => handleSplitPane(selectedSessionId ?? undefined, true)}
            className={`flex items-center justify-center w-11 text-zinc-600 active:text-zinc-400 transition-colors ${isLandscape ? 'min-h-[32px]' : 'min-h-[44px]'}`}
            aria-label="Split pane vertically"
            title="Split vertically (side by side)"
          >
            <Columns2 className="w-4 h-4" />
          </button>
          <button
            onClick={() => handleSplitPane(selectedSessionId ?? undefined, false)}
            className={`flex items-center justify-center w-11 text-zinc-600 active:text-zinc-400 transition-colors ${isLandscape ? 'min-h-[32px]' : 'min-h-[44px]'}`}
            aria-label="Split pane horizontally"
            title="Split horizontally (stacked)"
          >
            <Rows2 className="w-4 h-4" />
          </button>
          <button
            onClick={handleCloseTab}
            disabled={tabCount <= 1}
            className={`flex items-center justify-center w-11 text-zinc-600 active:text-red-400 transition-colors disabled:opacity-20 ${isLandscape ? 'min-h-[32px]' : 'min-h-[44px]'}`}
          >
            <X className="w-4 h-4" />
          </button>
          <button
            onClick={handleNewTab}
            className={`flex items-center justify-center w-11 text-zinc-600 active:text-zinc-400 transition-colors ${isLandscape ? 'min-h-[32px]' : 'min-h-[44px]'}`}
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* ── Broadcast indicator ── */}
      {broadcastMode && (
        <div
          className="flex items-center gap-2 px-4 py-1.5 flex-shrink-0 select-none"
          style={{ backgroundColor: BROADCAST_COLOR + '10', borderBottom: `1px solid ${BROADCAST_COLOR}30` }}
        >
          <Radio className="w-3 h-3 text-indigo-400" />
          <span className="text-[10px] text-indigo-400 font-bold tracking-wider">
            BROADCAST TO {broadcastTargets.size} WINDOW{broadcastTargets.size !== 1 ? 'S' : ''}
          </span>
          <button
            onClick={() => setShowMap(true)}
            className="text-[10px] text-indigo-300/60 font-bold ml-auto active:text-indigo-300"
          >
            EDIT
          </button>
        </div>
      )}

      {/* ── Terminal Content (colorized) ── */}
      <div
        ref={splitContainerRef}
        className={`flex-1 overflow-hidden flex min-h-0 min-w-0 ${splitMode ? (isLandscape ? 'flex-row' : 'flex-col') : 'flex-col'}`}
      >
        {/* Primary pane */}
        <div
          className="overflow-hidden relative min-h-0 min-w-0 flex flex-col"
          onClick={() => splitMode && setFocusedPane('primary')}
          style={{
            borderLeft: `3px solid ${splitMode && focusedPane === 'primary' ? ACCENT : ACCENT + '30'}`,
            ...(splitMode
              ? isLandscape
                ? { width: `${splitRatio * 100}%`, flexShrink: 0 }
                : { height: `${splitRatio * 100}%`, flexShrink: 0 }
              : { flex: 1 }),
          }}
        >
          {primaryPanes.length > 0 && (
            <PaneSwitcher
              panes={primaryPanes}
              selectedId={selectedSessionId}
              onSelect={handlePaneSelect}
              onOpenMap={handleOpenPaneMap}
            />
          )}
          <div
            ref={contentRef}
            onScroll={(event) => {
              primaryAtBottomRef.current = isScrolledToBottom(event.currentTarget);
              loadEarlierContent('primary', selectedSessionId, content, event.currentTarget);
            }}
            onTouchStart={() => beginTouch('primary')}
            onTouchEnd={() => endTouch('primary')}
            onTouchCancel={() => endTouch('primary')}
            className="flex-1 overflow-auto relative min-h-0 min-w-0"
          >
            {content && content.lines.length ? (
              <pre className="terminal-output p-4 text-[12px] leading-none whitespace-pre-wrap break-words min-h-full" style={{ color: content.fg, backgroundColor: content.bg }}>
                {content.lines.map((runs, i) => (
                  <TerminalLine
                    key={content.firstLine + i}
                    runs={runs}
                    last={i === content.lines.length - 1}
                    cursorColor={focusedPane === 'primary' ? ACCENT : null}
                  />
                ))}
              </pre>
            ) : (
              <div className="flex flex-col items-center justify-center h-full gap-3">
                <Clock className="w-5 h-5 text-zinc-700 animate-pulse" />
                <span className="text-[11px] text-zinc-700 tracking-wider">
                  {connected && itermStalled ? 'WAITING FOR ITERM2 TO ANSWER' : 'WAITING FOR OUTPUT'}
                </span>
              </div>
            )}
          </div>
        </div>

        {/* Drag divider */}
        {splitMode && (
          <div
            className={`flex-shrink-0 flex items-center justify-center touch-none select-none ${isLandscape ? 'w-3 cursor-col-resize' : 'h-3 cursor-row-resize'}`}
            style={{ backgroundColor: '#18181b' }}
            onTouchMove={handleSplitDrag}
          >
            <div
              className={`rounded-full bg-zinc-600 ${isLandscape ? 'w-1 h-8' : 'h-1 w-8'}`}
            />
          </div>
        )}

        {/* Split pane */}
        {splitMode && (
          <div
            className="flex-1 flex flex-col min-h-0 min-w-0"
            onClick={() => setFocusedPane('split')}
          >
            {/* Split pane tab bar */}
            <div className="flex items-center bg-[#0f0f0f] border-b border-zinc-800/60 flex-shrink-0 overflow-x-auto no-scrollbar select-none">
              <button
                onClick={(event) => {
                  event.stopPropagation();
                  setTabListTarget(tabListTarget === 'split' ? null : 'split');
                }}
                className="sticky left-0 z-10 flex w-8 min-h-[30px] flex-shrink-0 items-center justify-center border-r border-zinc-700/50 bg-[#0f0f0f] text-zinc-500 active:text-zinc-200"
                aria-label="Show split-pane tabs as a list"
                title="Show tabs as a list"
              >
                <ListIcon className="w-3.5 h-3.5" />
              </button>
              {splitWindow?.tabs.map((tab) => {
                const isActive = splitTabId === tab.id;
                return (
                  <button
                    key={tab.id}
                    onClick={(e) => { e.stopPropagation(); handleSplitTabClick(tab); }}
                    onTouchStart={() => { longPressRef.current = setTimeout(() => handleTabLongPress(tab), 500); }}
                    onTouchEnd={() => { if (longPressRef.current) clearTimeout(longPressRef.current); }}
                    onTouchMove={() => { if (longPressRef.current) clearTimeout(longPressRef.current); }}
                    className={`flex items-center gap-2 text-[10px] tracking-wider transition-all relative flex-shrink-0 active:opacity-70 border-r border-zinc-700/50 px-3 min-h-[30px]`}
                    style={{
                      color: isActive ? '#38bdf8' : '#52525b',
                      backgroundColor: isActive ? '#38bdf810' : 'transparent',
                    }}
                  >
                    {isActive && (
                      <div className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ backgroundColor: '#38bdf8' }} />
                    )}
                    <span className="font-semibold whitespace-nowrap">
                      {tab.title || tab.sessions[0]?.name || `Tab ${tab.index}`}
                    </span>
                    {isActive && (
                      <div className="absolute bottom-0 left-3 right-3 h-[2px] rounded-full" style={{ backgroundColor: '#38bdf8' }} />
                    )}
                  </button>
                );
              })}
              <div className="flex items-center flex-shrink-0 ml-auto border-l border-zinc-800/60">
                <span className="text-[10px] text-zinc-600 font-bold px-2 tabular-nums">{splitTabCount}</span>
                <button
                  onClick={(e) => { e.stopPropagation(); handleSplitPane(splitSessionId ?? undefined, true, 'split'); }}
                  className="flex items-center justify-center w-8 min-h-[30px] text-zinc-600 active:text-zinc-400 transition-colors"
                  aria-label="Split pane vertically"
                  title="Split vertically (side by side)"
                >
                  <Columns2 className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); handleSplitPane(splitSessionId ?? undefined, false, 'split'); }}
                  className="flex items-center justify-center w-8 min-h-[30px] text-zinc-600 active:text-zinc-400 transition-colors"
                  aria-label="Split pane horizontally"
                  title="Split horizontally (stacked)"
                >
                  <Rows2 className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); socketRef.current?.emit('closeTab'); }}
                  disabled={splitTabCount <= 1}
                  className="flex items-center justify-center w-8 min-h-[30px] text-zinc-600 active:text-red-400 transition-colors disabled:opacity-20"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); socketRef.current?.emit('newTab'); }}
                  className="flex items-center justify-center w-8 min-h-[30px] text-zinc-600 active:text-zinc-400 transition-colors"
                >
                  <Plus className="w-3.5 h-3.5" />
                </button>
                {state.length > 1 && (
                  <button
                    onClick={(e) => { e.stopPropagation(); setShowSplitMap(!showSplitMap); }}
                    className="text-[9px] font-bold tracking-wide px-2 py-0.5 rounded-md border transition-all active:scale-95 mr-1"
                    style={{
                      color: '#38bdf8',
                      backgroundColor: 'transparent',
                      borderColor: '#38bdf840',
                    }}
                  >
                    {state.length} win
                  </button>
                )}
              </div>
            </div>
            <div
              ref={splitContentRef}
              onScroll={(event) => {
                splitAtBottomRef.current = isScrolledToBottom(event.currentTarget);
                loadEarlierContent('split', splitSessionId, splitContent, event.currentTarget);
              }}
              onTouchStart={() => beginTouch('split')}
              onTouchEnd={() => endTouch('split')}
              onTouchCancel={() => endTouch('split')}
              className="flex-1 overflow-auto relative min-h-0 min-w-0"
              style={{ borderLeft: `3px solid ${focusedPane === 'split' ? '#38bdf8' : '#38bdf830'}` }}
            >
              {splitContent && splitContent.lines.length ? (
                <pre className="terminal-output p-4 text-[12px] leading-none whitespace-pre-wrap break-words min-h-full" style={{ color: splitContent.fg, backgroundColor: splitContent.bg }}>
                  {splitContent.lines.map((runs, i) => (
                    <TerminalLine
                      key={splitContent.firstLine + i}
                      runs={runs}
                      last={i === splitContent.lines.length - 1}
                      cursorColor={focusedPane === 'split' ? '#38bdf8' : null}
                    />
                  ))}
                </pre>
              ) : (
                <div className="flex flex-col items-center justify-center h-full gap-3">
                  <span className="text-[11px] text-zinc-700 tracking-wider">SELECT A SESSION</span>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {tabListTarget === 'tabBar' && tabBarWindow && (
        <TabListOverlay
          tabs={tabBarWindow.tabs}
          selectedId={tabBarSelectedTabId}
          accent={splitMode && focusedPane === 'split' ? '#38bdf8' : ACCENT}
          onSelect={(tab) => {
            handleTabClick(tabBarWindow.id, tab);
            setTabListTarget(null);
          }}
          onClose={() => setTabListTarget(null)}
        />
      )}

      {tabListTarget === 'split' && splitWindow && (
        <TabListOverlay
          tabs={splitWindow.tabs}
          selectedId={splitTabId}
          accent="#38bdf8"
          onSelect={(tab) => {
            handleSplitTabClick(tab);
            setTabListTarget(null);
          }}
          onClose={() => setTabListTarget(null)}
        />
      )}

      {showTypingLog && (
        <TypingLogOverlay log={typingLog} onClose={() => setShowTypingLog(false)} />
      )}

      {/* ── Quick Actions ── */}
      {/* Two fixed rows rather than one scrolling strip. A horizontally
          scrolled row hides keys behind an edge with nothing to suggest they
          are there, and swiping it is easy to start by accident while aiming
          for a key. Every button is flex-1 with a zero basis, so a row divides
          whatever width the phone has and both rows always fit, portrait or
          landscape, without a scroll. */}
      <div className={`flex flex-col bg-zinc-950/60 border-t border-zinc-800/40 flex-shrink-0 select-none ${isLandscape ? 'gap-0.5 px-2 py-1' : 'gap-1 px-3 py-2'}`}>
        <div className="flex items-center gap-1" role="group" aria-label="Control and arrow keys">
          <QuickBtn label="ESC" onClick={() => sendSpecialKey('\x1b')} color="#f87171" />
          <QuickBtn label="^C" onClick={() => sendSpecialKey('\x03')} color="#f87171" />
          <QuickBtn label="^D" onClick={() => sendSpecialKey('\x04')} color="#fbbf24" />
          <QuickBtn label="^Z" onClick={() => sendSpecialKey('\x1a')} color="#fbbf24" />
          <QuickBtn label="^L" onClick={() => sendSpecialKey('\x0c')} color="#38bdf8" />
          <div className="w-px h-5 bg-zinc-800 mx-0.5 flex-shrink-0" />
          <QuickBtn title="Up" icon={<ChevronUp className="w-3.5 h-3.5" />} onClick={() => sendSpecialKey('\x1b[A')} color="#a78bfa" />
          <QuickBtn title="Down" icon={<ChevronDown className="w-3.5 h-3.5" />} onClick={() => sendSpecialKey('\x1b[B')} color="#a78bfa" />
          <QuickBtn title="Left" icon={<ChevronLeft className="w-3.5 h-3.5" />} onClick={() => sendSpecialKey('\x1b[D')} color="#a78bfa" />
          <QuickBtn title="Right" icon={<ChevronRight className="w-3.5 h-3.5" />} onClick={() => sendSpecialKey('\x1b[C')} color="#a78bfa" />
        </div>
        <div className="flex items-center gap-1" role="group" aria-label="Tab, return and clipboard">
          <QuickBtn label="TAB" onClick={() => sendSpecialKey('\t')} color="#818cf8" />
          {/* Real Return (CR, 0x0D): submits in shells AND raw-mode TUIs like Claude Code */}
          <QuickBtn title="Return" icon={<CornerDownLeft className="w-3.5 h-3.5" />} onClick={() => sendSpecialKey('\r')} color="#34d399" />
          {/* Newline (LF, 0x0A): the Shift+Enter equivalent — inserts a line without submitting */}
          <QuickBtn title="Newline without sending" label="⇧↵" onClick={() => sendSpecialKey('\n')} color="#14b8a6" />
          <div className="w-px h-5 bg-zinc-800 mx-0.5 flex-shrink-0" />
          <QuickBtn title="Paste" icon={<Clipboard className="w-3.5 h-3.5" />} onClick={handlePaste} color="#38bdf8" />
          <QuickBtn title="Copy pane" icon={<Copy className="w-3.5 h-3.5" />} onClick={handleCopy} color="#71717a" />
          <QuickBtn title="Clear line" icon={<Trash2 className="w-3.5 h-3.5" />} onClick={() => sendSpecialKey('\x15')} color="#fb7185" />
          <QuickBtn title="Typing log" icon={<ScrollText className="w-3.5 h-3.5" />} onClick={() => setShowTypingLog(true)} color="#facc15" />
        </div>
      </div>

      {/* ── Buffered command / native direct input ── */}
      <div className={`bg-zinc-950/90 border-t border-zinc-800/60 flex-shrink-0 ${isLandscape ? 'p-1.5' : 'p-3'}`}>
        <div className="flex items-center gap-2">
          <button
            onClick={toggleDirectInput}
            className="w-10 h-10 flex items-center justify-center rounded-xl border transition-all active:scale-90 flex-shrink-0"
            style={{
              color: directInputMode ? '#09090b' : '#a78bfa',
              backgroundColor: directInputMode ? '#a78bfa' : '#a78bfa10',
              borderColor: directInputMode ? '#a78bfa' : '#3f3f46',
            }}
            aria-label={directInputMode ? 'Exit direct terminal input' : 'Use iOS keyboard for direct terminal input'}
            title={directInputMode ? 'Exit direct input' : 'Direct terminal input'}
          >
            {directInputMode ? <Terminal className="w-4 h-4" /> : <Keyboard className="w-4 h-4" />}
          </button>
          {!directInputMode && (
            <button
              onClick={toggleSmartTyping}
              className="w-10 h-10 flex items-center justify-center rounded-xl border transition-all active:scale-90 flex-shrink-0"
              style={{
                color: smartTyping ? '#09090b' : '#71717a',
                backgroundColor: smartTyping ? ACCENT : '#27272a10',
                borderColor: smartTyping ? ACCENT : '#3f3f46',
              }}
              aria-label={smartTyping ? 'Turn off autocorrect' : 'Turn on autocorrect'}
              title={smartTyping ? 'Autocorrect on' : 'Autocorrect off'}
            >
              <SpellCheck2 className="w-4 h-4" />
            </button>
          )}
          <div
            className="flex-1 flex items-center gap-2 bg-zinc-900/60 border rounded-2xl pl-4 pr-1.5 py-1 transition-colors"
            style={{
              borderColor: directInputMode
                ? '#a78bfa80'
                : broadcastMode
                  ? BROADCAST_COLOR + '50'
                  : command.trim() ? ACCENT + '50' : '#27272a',
            }}
          >
            <span
              className="text-[14px] font-bold flex-shrink-0"
              style={{ color: directInputMode ? '#a78bfa' : broadcastMode ? BROADCAST_COLOR : ACCENT }}
            >
              {directInputMode ? '›' : broadcastMode ? '>' : '$'}
            </span>
            <input
              ref={commandInputRef}
              type="text"
              className="flex-1 bg-transparent border-none outline-none text-[16px] text-zinc-100 placeholder:text-zinc-700 font-mono min-h-[42px] min-w-0"
              aria-label={directInputMode ? 'Direct terminal input' : 'Command'}
              placeholder={directInputMode ? 'type directly to terminal…' : broadcastMode ? `broadcast to ${broadcastTargets.size}...` : 'command...'}
              value={directInputMode ? directInputValue : command}
              onChange={handleInputChange}
              onKeyDown={handleInputKeyDown}
              enterKeyHint={directInputMode ? 'enter' : 'send'}
              // Live/direct input always forces these off: autocorrecting
              // mid-keystroke would corrupt whatever is being typed
              // character-by-character straight into the terminal. Only the
              // buffered command box honors the smartTyping toggle.
              //
              // Auto-capitalization stays off either way. It is a separate
              // keyboard trait from autocorrection (WebKit carries them as
              // independent autocapitalizeType / isAutocorrect fields), so
              // smart typing can have one without the other — and a shell
              // line starting with a capital is wrong far more often than
              // it is right.
              autoCapitalize="none"
              autoComplete="off"
              autoCorrect={!directInputMode && smartTyping ? 'on' : 'off'}
              spellCheck={!directInputMode && smartTyping}
            />
            {showPendingBadge && (
              <span
                aria-live="polite"
                aria-label={`${pendingInputs.length} waiting for iTerm2`}
                title="Sent, waiting for iTerm2 to take it"
                className="flex items-center gap-1 px-1 text-[10px] font-bold tabular-nums text-amber-400 flex-shrink-0 select-none"
              >
                <Hourglass className="w-3 h-3 animate-pulse" />
                {pendingInputs.length}
              </span>
            )}
            {directInputMode ? (
              <span className="px-2 text-[9px] font-bold tracking-[0.16em] text-violet-400 flex-shrink-0">
                LIVE
              </span>
            ) : (
              <button
                onClick={handleSend}
                aria-label="Send"
                // Not disabled while disconnected: what is sent then waits and
                // goes as soon as the connection is back.
                disabled={!command.trim()}
                className="w-10 h-10 flex items-center justify-center rounded-xl transition-all active:scale-90 disabled:opacity-20 flex-shrink-0"
                style={{
                  backgroundColor: command.trim() ? (broadcastMode ? BROADCAST_COLOR : ACCENT) : '#27272a',
                  color: command.trim() ? '#000' : '#52525b',
                  boxShadow: command.trim() ? `0 4px 20px ${broadcastMode ? 'rgba(129,140,248,0.25)' : GLOW}` : 'none',
                }}
              >
                <Send className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      </div>

      <style>{`
        .pt-safe { padding-top: env(safe-area-inset-top); }
        .pb-safe-root { padding-bottom: max(12px, env(safe-area-inset-bottom, 34px)); }
        .pl-safe { padding-left: env(safe-area-inset-left); }
        .pr-safe { padding-right: env(safe-area-inset-right); }
        @supports (padding: max(0px)) {
          .pl-safe { padding-left: max(0px, env(safe-area-inset-left)); }
          .pr-safe { padding-right: max(0px, env(safe-area-inset-right)); }
        }
        .terminal-link { text-decoration: underline; text-decoration-color: currentColor; text-underline-offset: 2px; cursor: pointer; }
        .terminal-link:active { opacity: 0.6; }
        .cursor-blink { animation: blink 1s step-end infinite; }
        .cursor-cell { display: inline-block; position: relative; width: 0; z-index: 1; }
        @keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0; } }
        .no-scrollbar::-webkit-scrollbar { display: none; }
        ::-webkit-scrollbar { width: 3px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #27272a; border-radius: 2px; }
      `}</style>
    </div>
  );
}

function TabListOverlay({ tabs, selectedId, accent, onSelect, onClose }: {
  tabs: Tab[];
  selectedId: string | null;
  accent: string;
  onSelect: (tab: Tab) => void;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center px-4 pt-[max(5rem,env(safe-area-inset-top))] select-none"
      onClick={onClose}
      style={{ backgroundColor: 'rgba(0,0,0,0.82)', backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)' }}
    >
      <div
        className="w-full max-w-md overflow-hidden rounded-xl border border-zinc-700/60 bg-[#111113] shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <span className="text-[10px] font-bold tracking-[0.2em] text-zinc-500">
            SELECT TAB · {tabs.length}
          </span>
          <button
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-600 active:bg-zinc-800 active:text-zinc-300"
            aria-label="Close tab list"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="max-h-[65vh] overflow-y-auto p-2">
          {tabs.map((tab) => {
            const isActive = tab.id === selectedId;
            return (
              <button
                key={tab.id}
                onClick={() => onSelect(tab)}
                className="flex min-h-[48px] w-full items-center gap-3 rounded-lg px-3 text-left transition-colors active:bg-zinc-800"
                style={{ backgroundColor: isActive ? accent + '12' : 'transparent' }}
              >
                <span
                  className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-md text-[10px] font-bold tabular-nums"
                  style={{ color: isActive ? '#09090b' : '#71717a', backgroundColor: isActive ? accent : '#27272a' }}
                >
                  {tab.index}
                </span>
                <span className="min-w-0 flex-1">
                  <span
                    className="block truncate text-[12px] font-semibold tracking-wide"
                    style={{ color: isActive ? accent : '#a1a1aa' }}
                  >
                    {tab.title || tab.sessions[0]?.name || `Tab ${tab.index}`}
                  </span>
                  <span className="mt-0.5 block text-[9px] text-zinc-600">
                    {tab.sessions.length} pane{tab.sessions.length === 1 ? '' : 's'}
                  </span>
                </span>
                {isActive && <span className="h-2 w-2 flex-shrink-0 rounded-full" style={{ backgroundColor: accent }} />}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// --- Recent typing log: an editable scratchpad for reviewing/correcting/
// copying what was actually sent to the terminal, independent of whether it
// came from the buffered command box or direct/live input. Edits here are
// local to the textarea only — they don't rewrite the persisted rolling log,
// which keeps accumulating verbatim as ground truth for next time.
function TypingLogOverlay({ log, onClose }: { log: string; onClose: () => void }) {
  const [draft, setDraft] = useState(log);
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (copyTimerRef.current) clearTimeout(copyTimerRef.current); }, []);

  const handleCopyDraft = async () => {
    try {
      await navigator.clipboard.writeText(draft);
      setCopied(true);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
    } catch {}
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center px-4 pt-[max(3rem,env(safe-area-inset-top))] pb-[max(3rem,env(safe-area-inset-bottom))]"
      onClick={onClose}
      style={{ backgroundColor: 'rgba(0,0,0,0.82)', backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)' }}
    >
      <div
        className="flex w-full max-w-lg flex-col overflow-hidden rounded-xl border border-zinc-700/60 bg-[#111113] shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3 flex-shrink-0 select-none">
          <span className="text-[10px] font-bold tracking-[0.2em] text-zinc-500">
            RECENT TYPING
          </span>
          <button
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-600 active:bg-zinc-800 active:text-zinc-300"
            aria-label="Close typing log"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.currentTarget.value)}
          placeholder="Nothing typed yet."
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          className="h-[45vh] w-full resize-none bg-transparent p-4 text-[13px] leading-relaxed text-zinc-100 placeholder:text-zinc-700 font-mono outline-none"
        />
        <div className="flex items-center justify-end gap-2 border-t border-zinc-800 px-3 py-2 flex-shrink-0">
          <button
            onClick={handleCopyDraft}
            className="flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[11px] font-bold tracking-wide transition-all active:scale-95"
            style={{
              color: copied ? '#09090b' : '#facc15',
              backgroundColor: copied ? '#facc15' : '#facc1510',
              borderColor: '#facc1530',
            }}
          >
            <Copy className="h-3.5 w-3.5" />
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
    </div>
  );
}

// --- One terminal line ---
// Keyed by its absolute line number and memoized on its runs: a line that did
// not change between frames keeps the same runs array (mergeStyledContent
// keeps the one it had when an incoming line is equal to it), so a live frame
// re-renders only the lines that did.
const TerminalLine = memo(function TerminalLine({ runs, last, cursorColor }: {
  runs: Run[];
  last: boolean;
  /** The cursor's colour, or null to hide it (its pane is not focused). */
  cursorColor: string | null;
}) {
  return (
    <span>
      {runs.map((r, j) => (
        r.c ? (
          cursorColor !== null && (
            <span key={j} className="cursor-blink cursor-cell" style={{ color: cursorColor }} aria-hidden="true">█</span>
          )
        ) : (
          <TerminalRun key={j} run={r} />
        )
      ))}
      {last ? '' : '\n'}
    </span>
  );
});

// --- One styled run of terminal text ---
// A run carrying a link — an OSC 8 hyperlink, or a web address the server
// recognised — is an anchor that opens in a new tab, with loopback hosts
// pointed back at the Mac so a dev server's printed URL works from the phone.
function TerminalRun({ run }: { run: Run }) {
  const style: React.CSSProperties = {
    color: run.f,
    fontWeight: run.b ? 600 : 400,
    opacity: run.d ? 0.55 : 1,
    ...(run.g ? { backgroundColor: run.g } : null),
  };
  if (!run.u) return <span style={style}>{run.t}</span>;
  return (
    <a
      href={linkHref(run.u, window.location.hostname)}
      target="_blank"
      rel="noopener noreferrer"
      className="terminal-link"
      style={style}
    >
      {run.t}
    </a>
  );
}

// --- Tiny terminal thumbnail shown inside each pane-map cell ---
// Renders the tail of a pane's cached content in miniature, faithful colors.
const PanePreview = memo(function PanePreview({ content }: { content?: StyledContent }) {
  if (!content || !content.lines.length) return null;
  const lines = content.lines.slice(-28);
  return (
    <div className="absolute inset-0 overflow-hidden flex flex-col justify-end" style={{ backgroundColor: content.bg }}>
      <pre
        className="whitespace-pre px-1 pb-0.5"
        style={{ color: content.fg, fontSize: '6px', lineHeight: 1.1, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}
      >
        {lines.map((runs, i) => (
          <div key={i}>
            {runs.length
              ? runs.map((r, j) => (
                  <span key={j} style={{ color: r.f, fontWeight: r.b ? 600 : 400, opacity: r.d ? 0.55 : 1, ...(r.g ? { backgroundColor: r.g } : null) }}>
                    {r.t}
                  </span>
                ))
              : ' '}
          </div>
        ))}
      </pre>
    </div>
  );
});

// --- Per-tab pane switcher ---
// Shown for every tab, one pane or many: tapping a pane makes it fill the
// primary view (and the client starts live-streaming it), and PANE opens the
// map, which is also where a pane is split or closed.
function PaneSwitcher({ panes, selectedId, onSelect, onOpenMap }: {
  panes: Session[];
  selectedId: string | null;
  onSelect: (sessionId: string) => void;
  onOpenMap: () => void;
}) {
  return (
    <div className="flex items-center gap-1 px-2 py-1 bg-[#0c0c0c] border-b border-zinc-800/60 overflow-x-auto no-scrollbar flex-shrink-0 select-none">
      <button
        onClick={(e) => { e.stopPropagation(); onOpenMap(); }}
        className="flex items-center gap-1 text-[8px] font-bold tracking-[0.15em] text-zinc-500 hover:text-zinc-300 active:text-zinc-200 pr-1 flex-shrink-0 transition-colors"
        title="Show pane layout"
      >
        <LayoutGrid className="w-2.5 h-2.5" />
        PANE
      </button>
      {panes.map((s, i) => {
        const active = s.id === selectedId;
        return (
          <button
            key={s.id}
            onClick={(e) => { e.stopPropagation(); onSelect(s.id); }}
            className="flex items-center gap-1 px-2 h-6 rounded border text-[10px] font-bold transition-all active:scale-95 flex-shrink-0"
            style={{
              color: active ? '#000' : '#a1a1aa',
              backgroundColor: active ? ACCENT : 'transparent',
              borderColor: active ? ACCENT : '#3f3f46',
            }}
          >
            <span>{i + 1}</span>
            {s.name && <span className="font-medium opacity-80 max-w-[90px] truncate">{s.name}</span>}
          </button>
        );
      })}
    </div>
  );
}

// --- Quick Action Button ---
function QuickBtn({ label, icon, title, onClick, color }: {
  label?: string;
  icon?: React.ReactNode;
  /** Names an icon-only key for VoiceOver and for a desktop tooltip. */
  title?: string;
  onClick: () => void;
  color: string;
}) {
  return (
    <button
      // Tapping a button normally moves focus to it before the click fires,
      // which blurs the command input and closes the mobile keyboard. These
      // buttons are meant to be used *while* typing (direct or buffered), so
      // preventing the mousedown's default focus change keeps the input —
      // and its keyboard — focused; the click still fires normally.
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
      title={title}
      aria-label={title || label}
      // flex-1 over a fixed width: the row divides the phone's width between
      // its keys instead of overflowing it. basis-0 keeps every key in a row
      // the same size regardless of how wide its label is.
      className="flex items-center justify-center flex-1 basis-0 min-w-0 h-[36px] px-1 rounded-lg border transition-all active:scale-90 overflow-hidden"
      style={{
        borderColor: color + '30',
        backgroundColor: color + '10',
        color,
      }}
    >
      {icon || <span className="text-[11px] font-bold tracking-wide">{label}</span>}
    </button>
  );
}
