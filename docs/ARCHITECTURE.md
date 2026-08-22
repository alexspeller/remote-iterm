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

The phone loads the React client separately from Vite (:7292).
```

This architecture is the largest departure from the [upstream project](https://github.com/mammadovziya/remote-iterm). Upstream used a Node.js Socket.IO server that invoked AppleScript with `osascript` for iTerm state, terminal content, and commands. It polled content every 150 ms and window state every second. This fork removes that server and talks to iTerm2 through its native Python API from a single `asyncio` event loop.

## Components

### Launcher (`iterm-server`)

The shell launcher owns the local process lifecycle:

1. Finds a working Python 3.8 or newer rather than accepting the macOS Command Line Tools shim.
2. Creates `server/.venv` on first use.
3. Hashes `server/requirements.txt` and installs dependencies only when that hash changes.
4. Creates a 256-bit shared key on first launch and reuses it from `~/Library/Application Support/remote-iterm/access-key`.
5. Starts the Python service on port 7291 and Vite on port 7292.
6. Stores both process IDs, prints a key-bearing QR code, and exposes `start`, `stop`, and `restart` commands.
7. Refuses Vite's fallback-port behavior, and on stop waits for both fixed ports to be released before a restart.
8. Runs the Python service without initializing AppKit, so it remains a background process instead of appearing in the macOS application switcher.

### Python service (`server/server.py`)

The backend runs `python-socketio` on `aiohttp` alongside one iTerm2 API connection. Its responsibilities are:

- Serialize iTerm windows, tabs, sessions, focus, titles, and screen geometry for the client.
- Reconstruct nested split-pane rectangles by walking iTerm2's splitter tree.
- Subscribe to layout and focus notifications.
- Run one screen-stream task per session watched by at least one connected client.
- Read a bounded live tail for watched sessions and encode it as compact styled runs.
- Page older scrollback from iTerm only when the client approaches the top of its currently loaded history.
- Resolve terminal colors through the session's profile and the xterm 256-color palette.
- Route commands and raw key bytes to a specific session.

State changes are pushed when iTerm2 reports them. A two-second synchronization loop covers job-title changes and pure window moves that do not have a suitable notification; serialized state is compared with the last value before anything is emitted.

Screen streaming is based on the union of every client's `watch` list. This means two phones watching the same pane share one backend stream task, while an unwatched background pane consumes no continuous screen-reading work. Each live update contains only the most recent 250 lines; pane-map previews request 40 lines, and older history loads in 500-line pages. This prevents large or unlimited iTerm histories from being read and retransmitted on every screen change while still making all retained scrollback reachable.

A shared stream task emits its first frame only when it is *created*, so a client that starts watching a pane already streamed for another connection must be seeded explicitly. `watch` therefore hands the requesting client an immediate snapshot of every already-streamed pane in its list (from the stream's cached last frame, or a fresh read if none). Without this, a phone reconnecting after sleep — while its previous connection's stream is still alive — would sit on "waiting for output" until the pane next changed. Panes not yet streamed need no seeding here: `watch` starts their stream task, whose own initial read fans out to all current watchers.

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
- Stores command history only in browser `localStorage`.
- Reconnects indefinitely and measures Socket.IO round-trip latency.
- Reads the shared key from the URL fragment, remembers it in browser `localStorage`, and sends it in the Socket.IO authentication payload.

The client is an installable PWA, but it is still a web application served by the Mac. There is no cloud relay or hosted control plane.

### State snapshotter (`server/snapshot.py`)

An independent process — deliberately separate from the phone server so a bug in one cannot silence the other, and so snapshots keep flowing whether or not any phone is connected. It holds its own iTerm2 API connection and, on layout/focus notifications (debounced) plus a periodic heartbeat, serializes every window → tab → pane: the split tree, each pane's cwd / foreground job / command line, and a plain-text tail of its output. It reuses `server/geometry.py` for the split geometry and `server/ascii_layout.py` to draw the layout map. It writes an always-current `latest/` snapshot (with full per-pane content), archives the previous session into `sessions/` at startup so a crash+reopen can't clobber it, and appends a change-deduplicated, 14-day-pruned `history/` log of layout + metadata only. See [State snapshots and restore](#state-snapshots-and-restore).

### AutoLaunch supervisor (`autolaunch/remote-iterm.py`)

A stdlib-only iTerm2 AutoLaunch script (installed via `iterm-snapshot install`) that starts the whole stack — phone server, Vite client, and snapshotter — through the existing `iterm-server` launcher whenever iTerm2 launches, and stops it when iTerm2 quits. It intentionally does not import the iTerm2 API (which would consume the single AutoLaunch `ITERM2_COOKIE` the child processes need) and runs the children through the user's login shell so their real `PATH` (for `npx vite`) is present. iTerm2 quit is detected via a signal handler and a parent-pid watch.

## Socket.IO contract

The protocol intentionally evolves the upstream event names where possible so the UI and backend remain loosely coupled.

| Direction | Event | Purpose |
| --- | --- | --- |
| server → client | `state` | Complete window, tab, pane, focus, and geometry snapshot |
| server → client | `screenSize` | Mac display dimensions used by the spatial window map |
| server → client | `content` | Styled live-tail or preview lines for one session |
| server → client | `historyContent` | An older page of styled terminal lines to prepend |
| client → server | `watch` | Replace the client's set of live-streamed session IDs |
| client → server | `getContent` | Request one immediate session snapshot |
| client → server | `getAllContent` | Request snapshots used for background previews |
| client → server | `getEarlierContent` | Request the page before the oldest currently loaded line |
| client → server | `execute` | Send a command followed by carriage return to one session |
| client → server | `sendKeys` | Send raw characters or terminal escape/control bytes |
| client → server | `broadcast` | Execute a command in a list of sessions |
| client → server | `newTab`, `closeTab` | Change the active iTerm window's tabs |
| client → server | `renameSession` | Rename one iTerm session |
| client → server | `focus` | Activate an iTerm window and tab on the Mac |
| bidirectional ack | `ping` | Measure application-level round-trip latency |

Styled terminal content is run-length encoded. Each run uses `t` for text and may include `f` (foreground), `g` (background), `b` (bold), `d` (faint), or `c` (cursor). Omitted colors inherit the pane's default `fg` and `bg` values.

## Pane geometry

iTerm2 represents a tab as nested splitters. A vertical splitter arranges children left-to-right; a horizontal splitter arranges them top-to-bottom. The recursion that calculates each child's natural size and assigns normalized rectangles from `0` to `1` lives in `server/geometry.py`, shared by the phone server and the snapshotter. The client can therefore reproduce mixed nested layouts without knowing the Mac's pixel dimensions.

When iTerm2 maximizes a pane, the frames for minimized sessions are unavailable. The server keeps those sessions discoverable and deliberately falls back to an even grid rather than presenting fabricated split proportions.

## State snapshots and restore

The snapshotter turns the live geometry into durable, recoverable state under `~/Library/Application Support/remote-iterm/snapshots/`:

- `latest/layout.txt` — a human-readable ASCII map of every window/tab/pane plus a per-pane table (cwd, job, last command), the file to open after a crash.
- `latest/state.json` — the full structured snapshot, including each tab's serialized split tree, for restore.
- `latest/panes/<id>.txt` — a ~200-line plain-text tail of each pane (the source for restore's content echo, also greppable).
- `sessions/<timestamp>/` — a full, content-bearing copy of a *completed* session (same shape as `latest/`). Keeps the last 20 within 14 days.
- `history/<date>.jsonl` — one record per *changed* layout (deduplicated like the server's `push_state`), carrying layout + metadata only (no content), pruned to 14 days.

**Surviving reopen (the key crash-recovery mechanism).** `latest/` is overwritten continuously, so the naïve failure mode is: iTerm2 crashes, you reopen it, the snapshotter starts capturing the new blank session and overwrites the pre-crash one before you can restore it. To prevent this, the snapshotter's very first action at startup — before it captures anything — is to copy the outgoing `latest/` (content included) into `sessions/<its capturedAt>/`. Because the snapshotter is an AutoLaunch process, "startup" happens every time iTerm2 launches, so each session's final state is preserved as an archive. `iterm-snapshot restore` therefore defaults to the newest `sessions/` archive — i.e. your last completed session, with content — rather than the live `latest/` (available via `--current`). The crash-recovery flow reduces to: reopen iTerm2, run `iterm-snapshot restore`.

**Surviving a graceful quit (the empty-snapshot guard).** A crash kills iTerm2 instantly, so `latest/` keeps the last rich pre-crash state — exactly what the archive step wants. A *graceful* quit is the subtler case: iTerm2 tears its windows down one step ahead of the snapshotter dying, so the still-alive snapshotter receives the layout-change notifications and, left unchecked, captures a **zero-window** layout into `latest/`, then archives that emptiness on reopen — erasing the very session you meant to keep. The snapshotter therefore never overwrites `latest/` with a snapshot that has no windows, and never archives an empty `latest/`; empty archives from before this guard are pruned on sight (and are never offered as a restore source). So a graceful quit leaves the last real state intact, just like a crash. Closing every window without quitting is handled the same way — `latest/` holds the last non-empty layout until a new window appears.

**Maximized tabs.** Because the snapshotter runs continuously it can do better than a one-shot capture: it caches each tab's last split tree seen while *not* maximized and renders/restores that when the tab is later maximized (whose live tree has collapsed to a single pane). A tab that has only ever been seen maximized falls back to a grid tree over all its panes, so no pane is lost.

**Restore (`server/restore.py`).** Restore replays a snapshot into **new** windows — it never touches existing ones. For each tab it recreates the split tree with `async_split_pane` (matching orientation; proportions are approximate because the API always halves), `cd`s each pane to its saved directory, replays any explicit tab title override (so a `🧰 <project>` tab keeps its identity, minus the transient `🔔` attention marker), reapplies each pane's custom initial-directory profile setting (so splitting a restored project pane still opens in the project dir — the effective profile matches what the launcher created), and uses `async_inject` to *display* the pane's previous output without executing it, followed by the full command line that was running (e.g. the whole `node …/reminders-today.ts`), highlighted on its own line directly above the fresh prompt. It prefers iTerm's recorded command line and falls back to the foreground job name; a pane that was only a shell prompt gets no such note. Running processes cannot be revived. Restoring from a history record (`--at`) rebuilds layout and cwds but has no content to echo.

## Input semantics

The service writes directly to the target iTerm2 session:

- Commands end with carriage return (`0x0d`), matching a physical Return key in shells and raw-mode terminal applications.
- Quick actions and native direct input use the actual control characters or ANSI escape sequences instead of printable labels.
- Mobile split focus controls which session receives input; it does not need to change the pane focused in iTerm2 on the Mac.

## Trust and security model

The backend listens on all interfaces and allows any Socket.IO origin, but rejects the Socket.IO namespace connection unless its authentication payload contains the generated shared key. Rejection happens before window state or terminal content is emitted. The key is stored in a user-only file on the Mac and in browser `localStorage`; QR and bookmark URLs carry it in the fragment, which browsers do not include in the initial HTTP request.

This is bearer-key authentication, not encrypted transport. The subsequent Socket.IO authentication payload and terminal traffic travel over plain HTTP/WebSocket, so a capable network observer can capture them and reuse the key. There is also no per-client authorization, command confirmation, or read-only mode. Do not expose ports 7291 or 7292 to the public internet; prefer a trusted LAN or VPN and treat TLS as a prerequisite before any remote relay or internet-facing deployment.

## Testing boundaries

`server/test_server.py` covers pure styled-output behavior, scrollback ranges, and latest-wins watcher routing. `npm --prefix client run build` type-checks and bundles the React client. The iTerm2 connection, macOS screen geometry, notifications, and end-to-end phone interaction still require manual integration testing against a running iTerm2 instance.
