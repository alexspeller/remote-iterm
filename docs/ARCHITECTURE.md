# Architecture

remote-iterm is a local, three-part system:

```text
Phone browser / PWA
        │
        │ Socket.IO over the local network (:7291)
        ▼
Python asyncio service
        │
        │ iTerm2 native Python API
        ▼
iTerm2 windows → tabs → sessions (panes)

The phone loads the React client — a static production build — from a separate port (:7292).
```

This architecture is the largest departure from the [upstream project](https://github.com/mammadovziya/remote-iterm). Upstream used a Node.js Socket.IO server that invoked AppleScript with `osascript` for iTerm state, terminal content, and commands. It polled content every 150 ms and window state every second. This fork removes that server and talks to iTerm2 through its native Python API from a single `asyncio` event loop.

## Components

### Launcher (`iterm-server`)

The shell launcher owns the local process lifecycle:

1. Finds a working Python 3.8 or newer rather than accepting the macOS Command Line Tools shim.
2. Creates `server/.venv` on first use.
3. Hashes `server/requirements.txt` and installs dependencies only when that hash changes.
4. Creates a 256-bit shared key on first launch and reuses it from `~/Library/Application Support/remote-iterm/access-key`.
5. Builds the web client when anything under `client/` is newer than `client/dist`, starts the Python service on port 7291, and serves the built client on port 7292 with `vite preview`.
6. Stores both process IDs, prints a key-bearing QR code, and exposes `start`, `stop`, `restart`, and `restart-server` commands. `restart-server` restarts only the phone server (7291), leaving the web client and the snapshotter running; it is what the phone's restart button runs.
7. Refuses the client server's fallback-port behavior, and on stop waits for both fixed ports to be released before a restart.
8. Runs the Python service without initializing AppKit, so it remains a background process instead of appearing in the macOS application switcher.

**A build, not the dev server.** The phone is served the production bundle because the Vite dev server's HMR client reloads the page on its own: when its WebSocket closes uncleanly it polls the server and, as soon as the page is visible and the server answers, calls `location.reload()`. On a phone that WebSocket closes every time the screen locks or the page goes to the background, so every return to the page was a reload, and a reload throws away the half-typed command, the pane in view, and the mobile keyboard. The built bundle carries no HMR client, so only the user (or iOS evicting the page) reloads it. The launcher rebuilds when a client source is newer than `client/dist` and keeps serving the previous bundle if that build fails, so a type error cannot take the phone down; the cost is that client changes need `iterm-server restart`, the contract the Python server already had.

### Python service (`server/server.py`)

The backend runs `python-socketio` on `aiohttp` alongside one iTerm2 API connection. Its responsibilities are:

- Serialize iTerm windows, tabs, sessions, focus, titles, and screen geometry for the client.
- Reconstruct nested split-pane rectangles by walking iTerm2's splitter tree.
- Subscribe to layout and focus notifications.
- Run one screen-stream task per session watched by at least one connected client.
- Read a bounded live tail for watched sessions — incrementally, re-reading only what can have changed — and encode it as compact styled runs.
- Keep the last window/tab state and the last frame of recently viewed panes, so a phone that connects or reloads draws at once instead of waiting on iTerm2.
- Report to every client whether iTerm2 is answering its API.
- Deliver each page's keystrokes and commands in order, acknowledged, and never twice (see [Input semantics](#input-semantics)).
- Page older scrollback from iTerm only when the client approaches the top of its currently loaded history.
- Resolve terminal colors through the session's profile and the xterm 256-color palette.
- Mark links: OSC 8 hyperlinks arrive from iTerm2 per cell; any other web address is found by pattern, after joining soft-wrapped rows so an address that wrapped is one link.
- Route commands and raw key bytes to a specific session.
- Log, for every client, the peer address on connect and the Socket.IO disconnect reason and transport on disconnect, plus the client's own `hello` report, so a phone that keeps reconnecting can be diagnosed from the Mac (see [Diagnosing reconnects](#diagnosing-reconnects)).

State changes are pushed when iTerm2 reports them. A synchronization loop covers job-title changes and pure window moves that do not have a suitable notification: every two seconds while a browser page is connected, every ten while only programmatic clients are (aveiro keeps one connected permanently, so without that distinction an idle Mac was re-read around the clock). Serialized state is compared with the last value before anything is emitted, and the last state is kept to hand a newly connected client at once, ahead of a fresh build.

Screen streaming is based on the union of every client's `watch` list. This means two phones watching the same pane share one backend stream task, while an unwatched background pane consumes no continuous screen-reading work. Each live update contains only the most recent 250 lines; pane-map previews request 40 lines, and older history loads in 500-line pages. This prevents large or unlimited iTerm histories from being read and retransmitted on every screen change while still making all retained scrollback reachable.

**Incremental live reads (`server/live_tail.py`).** The 250 lines a client receives are not re-read from iTerm2 on every frame. Rows that have scrolled off the screen into scrollback are immutable — a program can only write to the screen — so each read fetches only the rows that were on screen last time plus whatever has scrolled in since, and reuses the rest. iTerm2 itself does rewrite scrollback in two cases, reflowing it when a pane's width changes and removing it when it is cleared. A changed grid size or screen height, or a buffer that shrank, is answered with a full read. A clear refilled before the next read (`clear`, or Claude Code redrawing everything) leaves iTerm2's line counts exactly where they were, so every incremental read also re-reads the four kept rows nearest the new ones and compares them, text and styles, with the cache; a difference, or an overlap too blank to prove anything, means a full read. A full read also happens every 30 seconds whatever happened. The cursor comes from the same response (iTerm2 includes it in every buffer response), so there is no second read of the screen. A pane idling at a prompt costs one screen's worth of lines per frame instead of 250 plus a screen, and each read waits in proportion to how long the previous one took (twice as long, up to two seconds), so a slow iTerm2 is asked less of rather than more. The health line's `lines_read` is the number of lines streams fetched in the last minute.

A shared stream task emits its first frame only when it is *created*, so a client that starts watching a pane already streamed for another connection must be seeded explicitly. `watch` therefore hands the requesting client the latest frame the server holds for each pane in its list — the running stream's, or one kept from earlier in a bounded cache of the 48 most recently read panes — without reading anything from iTerm2 itself. Without this, a phone reconnecting after sleep would sit on "waiting for output" until the pane next changed, and one reconnecting during an iTerm2 stall would wait out the stall. A pane with no frame at hand is covered by its stream's own first read, which fans out to all current watchers. Frames drawn from the cache are marked `cached`, and a page that already holds content for the pane ignores them, so an old frame never replaces a newer one. The cache, and the per-pane colour palette, are pruned of closed panes on every state push. `getContent` is never answered from the cache: it is how aveiro reads a pane before typing into it, and it takes the first frame it receives as the pane's current state. The phone does not use it.

A stream is cancelled when nobody watches its pane any more, and its teardown unsubscribes from iTerm2 — an RPC like any other, so during a stall it takes as long as the stall. Cancelled streams therefore finish in the background: the reconcile no longer waits for them while holding its lock, which had made one phone's pane switch hold up every other client's `watch` until iTerm2 answered.

Terminal changes are coalesced to at most 20 snapshots per second per watched session. Each connected client has a latest-wins application outbox: a newer live snapshot replaces an undelivered one for the same pane. The delivery task also stops feeding Engine.IO while that socket's outbound queue is full. Consequently a sleeping phone retains bounded work instead of growing an unbounded chain of serialized terminal screens. Live content is routed only to clients watching that session, and cancelled stream tasks are awaited before their references are discarded. A periodic health line records peak RSS, client and stream counts, pending events, and the largest transport queue for diagnosis.

Display geometry is read directly from CoreGraphics. The server deliberately does not import AppKit or create an `NSApplication`, which keeps its Python process out of Cmd-Tab while preserving the screen dimensions needed for the spatial window map.

### React client (`client/`)

The Vite/React client maintains the selected window, tab, and session separately. It:

- Automatically follows focus changes made on the Mac while preserving explicit pane selection.
- Caches styled output by session for fast switching and pane thumbnails.
- Follows live output only while a pane is already scrolled to the bottom, preserving the reader's position when they scroll back.
- Tells the backend exactly which primary and secondary sessions need live updates.
- Renders a tab's real split geometry as a spatial picker.
- Offers both horizontal tab strips and vertical tab lists for fast selection.
- Sends all input to the currently focused mobile pane.
- Provides a direct-input mode that keeps the native mobile keyboard open and forwards text and terminal keys immediately, while retaining the separate buffered command field.
- Stores command history, and the not-yet-sent command, only in browser `localStorage`, so a reload the phone did on its own (iOS evicting a background page, or relaunching the home-screen app) does not lose what was being typed.
- Renders linked runs as anchors that open in a new tab, pointing loopback hosts at the Mac's address (`client/src/links.ts`) so a dev server's printed `http://localhost:3000` works from the phone.
- Keeps terminal text selectable and holds a pane's live updates while a finger is on it or text in it is selected, applying the latest frame once both have ended (see [Links and text selection](#links-and-text-selection)).
- Sends everything typed through an input queue (`client/src/inputQueue.ts`) that keeps it until iTerm2 has it, resends what a dead socket swallowed, and never sends anything twice (see [Input semantics](#input-semantics)). An hourglass counter in the command bar shows input still waiting.
- Shows ITERM2 NOT RESPONDING, with how long for, while the server reports that iTerm2 has stopped answering its API; typing meanwhile is queued, not lost.
- Offers a server restart button in the top bar (with a confirmation), served by the web client's own process so it works when the Python server is wedged or gone.
- Keeps at most 1000 lines per pane while the pane is followed at its bottom (a pane followed for hours used to keep every line that scrolled past, and re-render all of them each frame), and renders lines memoized and keyed by absolute line number so a live frame redraws only the lines that changed.
- Splits the selected pane from the pane map — vertically or horizontally, like iTerm's own ⌘D and ⇧⌘D — and selects the new pane once it appears. The pane switcher and map are shown for single-pane tabs too, since that is where splitting starts.
- Reconnects indefinitely — at once, and straight to WebSocket (`rememberUpgrade`) — and measures Socket.IO round-trip latency. A liveness ping unanswered for 8 seconds while the page is visible drops the socket and reconnects, rather than waiting out Engine.IO's 45-second ping timeout. A disconnection shows as a red dot immediately and as a small non-blocking RECONNECTING banner only after 2.5 s, so the sub-second gaps a phone's network path produces never interrupt reading or typing; Socket.IO queues keystrokes typed meanwhile and sends them on reconnect.
- Introduces every connection with a `hello` event carrying what only the page knows: a per-load page id and connection counter (a reload versus the same page reconnecting), how the page saw its previous connection end (Socket.IO reason, the raw WebSocket close code, page visibility, time since the last packet each way — kept in `sessionStorage` across a reload), and `navigator.onLine`. The server logs it beside the disconnect reason, transport, and peer address it records itself.
- Reads the shared key from the URL fragment, remembers it in browser `localStorage`, and sends it in the Socket.IO authentication payload — or connects on the server's HttpOnly cookie alone when it has no key, showing the key prompt only after a refusal. Every successful connection posts to `/auth` to renew that cookie.
- Resolves a notification deep link (`#session=<id>`, see `client/src/deepLink.ts`) against the first complete state: selects the pane's window, tab, and pane, asks the Mac to focus it, and strips the parameter from the URL so a reload does not jump back.

The client is an installable PWA, but it is still a web application served by the Mac. There is no cloud relay or hosted control plane.

### State snapshotter (`server/snapshot.py`)

An independent process — deliberately separate from the phone server so a bug in one cannot silence the other, and so snapshots keep flowing whether or not any phone is connected. It holds its own iTerm2 API connection and, on layout/focus notifications (debounced) plus a periodic heartbeat, serializes every window → tab → pane: the split tree, each pane's cwd / foreground job / command line, and a tail of its output (plain and coloured). It reuses `server/geometry.py` for the split geometry and `server/ascii_layout.py` to draw the layout map. It writes an always-current `latest/` snapshot (with full per-pane content), archives the previous session into `sessions/` at startup so a crash+reopen can't clobber it, and appends a change-deduplicated, 14-day-pruned `history/` log of layout + metadata only. See [State snapshots and restore](#state-snapshots-and-restore).

A pane's tail is re-read only when its buffer has visibly moved since the last read — output scrolled, the cursor moved, or the pane was resized or cleared, which a line-count request and a one-line read show — or once it is five minutes old regardless. A read that fails is not kept, so it is retried at the next build rather than leaving the pane blank for five minutes. At any moment most panes are idle, and each tail read is about 200 styled lines that iTerm2 renders as several hundred kilobytes of text on its main thread (see [Why iTerm2 stalls](#why-iterm2-stalls)), so re-reading all of them every heartbeat was the largest constant load the stack put on iTerm2 when nobody was using the phone. A program that redraws in place without scrolling is caught by the five-minute ceiling. Its caches are pruned of closed panes and tabs after every build.

### AutoLaunch supervisor (`autolaunch/remote-iterm.py`)

A stdlib-only iTerm2 AutoLaunch script (installed via `iterm-snapshot install`) that starts the whole stack — phone server, web client, and snapshotter — through the existing `iterm-server` launcher whenever iTerm2 launches, and stops it when iTerm2 quits. It intentionally does not import the iTerm2 API (which would consume the single AutoLaunch `ITERM2_COOKIE` the child processes need) and runs the children through the user's login shell so their real `PATH` (for `npx vite`) is present. iTerm2 quit is detected via a signal handler and a parent-pid watch.

**Replacing a running supervisor.** iTerm2 starts the script once at launch, so an edit is not picked up until the next launch — unless the running copy is replaced: `kill -9` its Python process (SIGKILL, because its SIGTERM handler would stop the whole stack), then `osascript -e 'tell application "iTerm2" to launch API script named "remote-iterm"'` starts a fresh one under iTerm2 with the same parent chain the AutoLaunch mechanism gives it.

**Restart evidence.** The supervisor restarts the phone server only after its port has failed `SERVER_DOWN_POLLS` consecutive probes (15 s at the 5 s interval, each probe allowed 2 s), because a restart drops every connected phone and one failed half-second connect is not a dead server: on 2026-09-19 a single miss ran `start`, whose orphan sweep then killed a server whose event loop was demonstrably healthy. The snapshotter check stays immediate, since it is pid-based rather than a network probe.

**Startup contract.** `iterm-server start` is slow by design: it holds its lock until the children it spawned are actually listening, so the supervisor's 5s poll queues behind a start in progress instead of racing it. Three rules keep that from turning into a livelock, all of which have failed in practice at least once:

1. A `server.py` that exists but has not bound 7291 yet is *adopted and waited for*, not killed — it is usually still handshaking with iTerm2, which takes minutes on a loaded Mac. Respawning instead means no attempt ever gets long enough to finish, since the next poll arrives every 5s. Only once a process passes `SERVER_START_SECONDS` is it treated as stuck and reaped.
2. The supervisor's `START_TIMEOUT` must stay comfortably above `iterm-server`'s worst case (`LOCK_WAIT_SECONDS + SERVER_START_SECONDS + CLIENT_START_SECONDS`). If the supervisor's kill lands first, the shell dies without releasing the lock.
3. The lock records its holder's pid and any run may reclaim it once that holder is gone, because SIGKILL cannot be trapped. Without this a single mistimed kill wedges every subsequent start.

## Socket.IO contract

The protocol intentionally evolves the upstream event names where possible so the UI and backend remain loosely coupled.

| Direction | Event | Purpose |
| --- | --- | --- |
| server → client | `serverInfo` | `{instance}`: identifies this server process, so a page can tell a reconnect from a restart |
| server → client | `itermStatus` | `{ok, stalledSince, latencyMs, now}`: whether iTerm2 is answering its API, sent on connect and when that changes |
| server → client | `inputDelivered` | `{page, upto, failed}`: everything this page sent up to sequence `upto` has been handled; `failed` lists recent inputs iTerm2 could not take, and why |
| server → client | `state` | Complete window, tab, pane, focus, and geometry snapshot |
| server → client | `screenSize` | Mac display dimensions used by the spatial window map |
| server → client | `content` | Styled live-tail or preview lines for one session |
| server → client | `historyContent` | An older page of styled terminal lines to prepend |
| client → server | `watch` | Replace the client's set of live-streamed session IDs |
| client → server | `getContent` | Request one immediate session snapshot |
| client → server | `getAllContent` | Request snapshots used for background previews |
| client → server | `getEarlierContent` | Request the page before the oldest currently loaded line |
| client → server | `execute` | Send a command followed by carriage return to one session; acknowledged when it carries `page` and `seq` |
| client → server | `sendKeys` | Send raw characters or terminal escape/control bytes; acknowledged likewise |
| client → server | `broadcast` | Execute a command in a list of sessions; acknowledged likewise |
| client → server | `splitPane` | Split a session vertically or horizontally; acknowledged with `{sessionId}` of the new pane, or `{error}` |
| client → server | `closePane` | Close one session, without the Mac's confirmation alert |
| client → server | `newTab`, `closeTab` | Change the active iTerm window's tabs |
| client → server | `renameSession` | Rename one iTerm session |
| client → server | `focus` | Activate an iTerm window and tab on the Mac, or a specific pane when `sessionId` is given |
| bidirectional ack | `ping` | Measure application-level round-trip latency |

Styled terminal content is run-length encoded. Each run uses `t` for text and may include `f` (foreground), `g` (background), `b` (bold), `d` (faint), `c` (cursor), or `u` (the address the run links to). Omitted colors inherit the pane's default `fg` and `bg` values.

## Links and text selection

Links are resolved on the server, where the cells are. iTerm2 reports an OSC 8 hyperlink on each cell it covers (`CellStyle.url`), so the link becomes part of the run key and its boundaries survive run-length grouping. Plain addresses are matched with xterm.js's web-links pattern over the line's text and the runs are split at the match boundaries, keeping each piece's style; an explicit OSC 8 link is never overridden by the address it happens to show. iTerm2 wraps a long line onto the following rows and reports each row with `hard_eol` false, so the rows of one logical line are joined before matching and an address that wrapped mid-way is one link on every row it spans. Only `http` and `https` addresses become links: `file://` links (which `ls --hyperlink` emits) and app schemes point at the Mac and would be dead taps on a phone. The client renders a linked run as an anchor opening in a new tab and rewrites loopback hosts to the address the page was loaded from.

OSC 8 hyperlinks do not currently reach the server, through no fault of the API contract: as of iTerm2 3.6.11 (and `master` at 2f85a80), `PTYSession.m`'s `protoStyleForCharacter:externalAttributes:` builds the `ITMURL` message for a hyperlinked cell but never assigns it to the cell style, so `CellStyle.url` is always empty on the wire. A hyperlink whose visible text is itself a web address is still linked by the pattern pass; one with other text (a file name linking to a docs page, say) shows as plain text until iTerm2 attaches the URL. The OSC 8 path here is unit-tested against a faked cell style and needs no change when that happens.

Text selection needed two things. Nothing above the terminal text may carry `user-select: none` — WebKit has not reliably let a descendant's `text` win over an ancestor's `none` — so the chrome opts out element by element instead of the body opting everything out. And the DOM under a selection must hold still: iOS abandons a long-press selection if the DOM changes mid-gesture, and a pane running a spinner redraws several times a second, which made selecting anything in it impossible. While a finger is on a pane, or a selection lives inside it, that pane's incoming frames are kept aside (tagged with their session, so a pane switch cannot flush one pane's output into another) and the latest one is applied when the gesture and the selection have both ended; the top bar shows PAUSED meanwhile. Older-history pages requested by scrolling are not held, since they prepend above the viewport and the scroll position is preserved for them anyway.

## Diagnosing reconnects

Every connection is bracketed in `.iterm-server.log` by `Client connected: <sid> by key|cookie from <peer> (<user agent>)`, the client's `hello <sid>: {...}` report, and `Client disconnected: <sid> (<reason>, <transport>)`. Read them together:

- `reason` is python-socketio's: `ping timeout` is a stalled path, `transport close` the network or OS closing the socket, `client disconnect` the page letting go, `server disconnect` this server.
- `hello.page` and `hello.connect` tell a fresh page load (new id) from the same page reconnecting (same id, counter +1); `hello.navigation` is `reload` for a user refresh.
- `hello.lastDisconnect` is the page's own view of how the previous connection ended: Socket.IO's reason (`transport error` is the WebSocket erroring under a visible page), `visibility` at that moment (`hidden` means the page was suspended — a lock or an app switch, not a network fault), `socketClose.code` (1006 is an abnormal TCP loss; 1002/1007/1009 are protocol, data, or size errors that implicate the framing or compression layer rather than the network), and `sinceSendMs`/`sinceReceiveMs`.
- The peer address separates the LAN path (192.168.x) from the tailnet one (100.x). python-engineio's aiohttp driver fills `REMOTE_ADDR` with a constant 127.0.0.1, so the server reads the address from the aiohttp request instead.
- `launcher: <action> by pid … (parent …)` records every `iterm-server` invocation with its caller, so a stack that keeps being restarted can be traced to whoever is doing it.
- `websocket from <peer> ended: close_code=… exception=…` is aiohttp's view of the same ending, logged by a wrapper the server installs in python-engineio's driver table: 1000 is a clean close by the peer, 1006 a TCP connection lost without one, and the exception (a `ConnectionResetError`, say) is what the reset looked like from the Mac.

`REMOTE_ITERM_WS_COMPRESSION=0` in the server's environment turns off WebSocket permessage-deflate (aiohttp negotiates it by default; python-engineio does not expose the flag, so the server swaps in a `WebSocketResponse` built with `compress=False`). It exists to test whether a client's WebSocket errors come from the compression layer, at about four times the bytes per live frame.

## Pane geometry

iTerm2 represents a tab as nested splitters. A vertical splitter arranges children left-to-right; a horizontal splitter arranges them top-to-bottom. The recursion that calculates each child's natural size and assigns normalized rectangles from `0` to `1` lives in `server/geometry.py`, shared by the phone server and the snapshotter. The client can therefore reproduce mixed nested layouts without knowing the Mac's pixel dimensions.

When iTerm2 maximizes a pane, the frames for minimized sessions are unavailable. The server keeps those sessions discoverable and deliberately falls back to an even grid rather than presenting fabricated split proportions.

## State snapshots and restore

The snapshotter turns the live geometry into durable, recoverable state under `~/Library/Application Support/remote-iterm/snapshots/`:

- `latest/layout.txt` — a human-readable ASCII map of every window/tab/pane plus a per-pane table (cwd, job, last command), the file to open after a crash.
- `latest/state.json` — the full structured snapshot, including each tab's serialized split tree, for restore.
- `latest/panes/<id>.txt` — a ~200-line plain-text tail of each pane, for reading and grepping.
- `latest/panes/<id>.ansi` — the same lines with their colours and attributes as SGR escapes (palette colours stay palette indices, so the pane's theme colours them as before); the source for restore's content echo.
- `sessions/<timestamp>/` — a full, content-bearing copy of a *completed* session (same shape as `latest/`). Keeps the last 20 within 14 days.
- `history/<date>.jsonl` — one record per *changed* layout (deduplicated like the server's `push_state`), carrying layout + metadata only (no content), pruned to 14 days.

**Surviving reopen (the key crash-recovery mechanism).** `latest/` is overwritten continuously, so the naïve failure mode is: iTerm2 crashes, you reopen it, the snapshotter starts capturing the new blank session and overwrites the pre-crash one before you can restore it. To prevent this, the snapshotter's very first action at startup — before it captures anything — is to copy the outgoing `latest/` (content included) into `sessions/<its capturedAt>/`. Because the snapshotter is an AutoLaunch process, "startup" happens every time iTerm2 launches, so each session's final state is preserved as an archive. `iterm-snapshot restore` therefore defaults to the newest `sessions/` archive — i.e. your last completed session, with content — rather than the live `latest/` (available via `--current`). The crash-recovery flow reduces to: reopen iTerm2, run `iterm-snapshot restore`.

**Surviving a graceful quit (the empty-snapshot guard).** A crash kills iTerm2 instantly, so `latest/` keeps the last rich pre-crash state — exactly what the archive step wants. A *graceful* quit is the subtler case: iTerm2 tears its windows down one step ahead of the snapshotter dying, so the still-alive snapshotter receives the layout-change notifications and, left unchecked, captures a **zero-window** layout into `latest/`, then archives that emptiness on reopen — erasing the very session you meant to keep. The snapshotter therefore never overwrites `latest/` with a snapshot that has no windows, and never archives an empty `latest/`; empty archives from before this guard are pruned on sight (and are never offered as a restore source). So a graceful quit leaves the last real state intact, just like a crash. Closing every window without quitting is handled the same way — `latest/` holds the last non-empty layout until a new window appears.

**Maximized tabs.** Because the snapshotter runs continuously it can do better than a one-shot capture: it caches each tab's last split tree seen while *not* maximized and renders/restores that when the tab is later maximized (whose live tree has collapsed to a single pane). A tab that has only ever been seen maximized falls back to a grid tree over all its panes, so no pane is lost.

**Restore (`server/restore.py`).** Restore replays a snapshot into **new** windows — it never touches existing ones. For each tab it recreates the split tree with `async_split_pane` (matching orientation; proportions are approximate because the API always halves), `cd`s each pane to its saved directory, replays any explicit tab title override (so a `🧰 <project>` tab keeps its identity, minus the transient `🔔` attention marker), reapplies each pane's custom initial-directory profile setting (so splitting a restored project pane still opens in the project dir — the effective profile matches what the launcher created), and uses `async_inject` to *display* the pane's previous output without executing it, followed by the full command line that was running (e.g. the whole `node …/reminders-today.ts`), highlighted on its own line directly above the fresh prompt. It prefers iTerm's recorded command line and falls back to the foreground job name; a pane that was only a shell prompt gets no such note. Running processes cannot be revived. Restoring from a history record (`--at`) rebuilds layout and cwds but has no content to echo.

**Reading pane content faithfully (`server/terminal_lines.py`).** Two quirks of iTerm2's buffer API make the obvious read lossy. A cell a program moved the cursor over without writing — Claude Code pads with cursor-forward rather than spaces — holds character 0, which the API returns as a literal NUL; a terminal drops NULs, so echoed back they run the words either side together. They are read as spaces (restore does the same for archives written before this, which kept them). And iTerm2 measures each cell's text in UTF-16 code units while `LineContents.string_at` slices the decoded Python string, which counts code points, with those numbers: past any character outside the Basic Multilingual Plane (most emoji) every cell reads its neighbour's text and the line's last characters are lost. `terminal_lines.Line` slices the UTF-16 encoding instead, so each style run gets exactly its own text.

## Input semantics

A keystroke or command emitted into a WebSocket that has just died is lost without a trace: Socket.IO delivers at most once, and a phone on the tailnet loses its socket every minute or so. That is what "I pressed send and the text was ignored" was. So the page does not simply emit input. Its input queue numbers each item — the page's id and a sequence number — and keeps it through two acknowledgements:

1. **Received.** The server acknowledges each numbered input as soon as it has queued it, without waiting for iTerm2 (the acknowledgement also lists recent failures, since the count it carries includes them). A page that gets no acknowledgement within five seconds of visible time concludes its socket is dead, drops it, and reconnects at once. On each connection the page sends nothing until the server has said which process it is (`serverInfo`, its first message); it then sends again everything that server never acknowledged, in order. Socket.IO's own buffer of emits made into a socket it had not yet noticed was dead is emptied of input on disconnect, since flushing it on the next connection would put that input ahead of older input being resent.
2. **Delivered.** The server runs each page's input through one queue, strictly in order, and reports `inputDelivered` once iTerm2 has taken it — which during an iTerm2 stall is when the stall ends. A sequence number already accepted is acknowledged again but never delivered twice, so a resend is always safe. Input naming a pane that has closed is reported as failed and typed nowhere; it used to go to whatever pane the Mac had in front, as did a keystroke sent before the page knew any pane.

Two cases cannot be settled by resending. Input sent to a server that has since been replaced (a restart) may or may not have reached iTerm2 — whether or not it was acknowledged, since an acknowledgement can be lost after the text went in — so the page neither resends nor forgets it: it names it in a notice and, for a command, puts it back in the command box. A command sent but unconfirmed when the page itself is unloaded (iOS evicts background pages) is likewise put back in the box on the next load, not resent. Failed commands are restored to the box the same way (not broadcasts, which some panes may have taken). Input without a page and sequence number — aveiro's — takes the old path: sent at once, unacknowledged. A server too old to number input answers with an empty acknowledgement, which the page takes as delivery rather than resending, and one that never sends `serverInfo` is assumed after three seconds.

The service writes directly to the target iTerm2 session:

- Commands end with carriage return (`0x0d`), matching a physical Return key in shells and raw-mode terminal applications.
- Quick actions and native direct input use the actual control characters or ANSI escape sequences instead of printable labels.
- Mobile split focus controls which session receives input; it does not need to change the pane focused in iTerm2 on the Mac.

## Why iTerm2 stalls

Everything the phone does — the first frame, every keystroke — is an RPC answered on iTerm2's main thread, so when that thread stalls the phone stalls with it. On 2026-09-28 it stalled for 20-90 seconds at a time for over half an hour, with the phone server itself idle. Two things were found, both outside this project's control but not outside its influence:

- **Memory pressure.** The Mac had 7.1 of 8 GB of swap in use and about 340 MB of RAM free; iTerm2's 2.2 GB footprint (3.6 GB at its peak) was almost all compressed or swapped out, and a live sample of its main thread showed it spending most of its time freeing objects whose pages first had to be brought back in. While that lasts, nothing here can make iTerm2 answer faster.
- **The Script Console.** Before sending any API message — a response or a notification — iTerm2 renders it as protobuf text for its Script Console, on its main thread, whether or not the console is open (`-[iTermScriptHistoryEntry apiServerWillSendMessage:]`, which a macOS CPU report caught as the heaviest main-thread stack). The text is about seven times the wire size for styled lines: one 250-line read of a Claude Code pane measured 71-92 KB on the wire and 476-675 KB of text, a screen read ~200 KB, a session listing 5 KB against 22 KB. Each connection's console entry keeps its last 100 messages, and on iTerm2 3.6.11 the entry of a connection that has ended is never released until iTerm2 quits — so every restart of this server, the snapshotter, or any other API script leaves up to 100 such messages in iTerm2's memory for good. iTerm2's nightlies have a Clear Terminated button in the Script Console (upstream issue 12942), which frees them.

What this project does about it: it reads less (incremental live tails, no separate screen read for the cursor, a tiny watchdog probe, a snapshotter that skips idle panes, a slower state sync when no phone is connected), backs off when reads get slow, and when iTerm2 does stall it says so, keeps typing queued rather than lost, and draws a reconnecting phone from what it already holds.

## Trust and security model

The backend listens on all interfaces but rejects the Socket.IO namespace connection unless the generated shared key arrives either in the authentication payload or in the `remote_iterm_key` cookie. Rejection happens before window state or terminal content is emitted. The key is stored in a user-only file on the Mac and in browser `localStorage`; QR and bookmark URLs carry it in the fragment, which browsers do not include in the initial HTTP request.

The cookie exists because Safari deletes a site's script-writable storage after seven days of Safari use without interaction with the site, and a phone that only opens remote-iterm from notification deep links fits that pattern exactly; server-set cookies are not subject to that purge. `POST /auth` issues it (HttpOnly, `SameSite=Lax`, one year) to a request carrying a valid key or a valid existing cookie, so its lifetime slides with use. Cookies are scoped by host rather than port, so the cookie set by the API on 7291 rides along from the page served on 7292. Because the cookie is sent automatically, cross-origin requests are only accepted from origins whose host matches the request's `Host` header, whatever the port (`is_trusted_origin`); Engine.IO rejects other origins outright and `/auth` refuses them. `SameSite=Lax` additionally stops other sites from carrying the cookie at all.

The phone's server-restart button posts to `/api/restart-server` on the web client's port (7292), served by that process (`client/server/restartEndpoint.ts`) so it works when the Python server does not. It checks the same key — in the JSON body, or the same HttpOnly cookie, which the browser sends to 7292 too because cookies are scoped by host, not port — and refuses any request whose `Origin` is not exactly the page's own, so no other site can restart it with the cookie. One restart runs at a time.

This is bearer-key authentication, not encrypted transport. The subsequent Socket.IO authentication payload and terminal traffic travel over plain HTTP/WebSocket, so a capable network observer can capture them and reuse the key. There is also no per-client authorization, command confirmation, or read-only mode. Do not expose ports 7291 or 7292 to the public internet; prefer a trusted LAN or VPN and treat TLS as a prerequisite before any remote relay or internet-facing deployment.

## Testing boundaries

`server/test_server.py` covers pure styled-output behavior, scrollback ranges, and latest-wins watcher routing. `server/test_live_tail.py` checks every incremental read against the true buffer — including rows rewritten on screen before they scrolled off, reflow, clearing, and a limited scrollback — so a stale row fails a test. `server/test_resilience.py` covers input ordering, deduplication, failure reporting and the unacknowledged path, stall reporting, what a reconnecting client is drawn from, and stream teardown that hangs. `client/server/restartEndpoint.test.ts` exercises the restart endpoint over real HTTP, and `client/src/inputQueue.test.ts` the queue's delivery rules. `npm --prefix client run build` type-checks and bundles the React client. `npm --prefix client test` runs the vitest suite: pure deep-link parsing and link rewriting, and a jsdom render of the real `App` with only the socket faked, which is how the command-draft persistence, link rendering, and the selection hold are checked end to end. The iTerm2 connection, macOS screen geometry, notifications, and end-to-end phone interaction still require manual integration testing against a running iTerm2 instance.
