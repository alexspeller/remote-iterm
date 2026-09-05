#!/usr/bin/env python3
"""Remote iTerm server — drives iTerm2 over its native Python API.

Replaces the previous AppleScript/osascript implementation. A single asyncio
loop runs both the Socket.IO server (for the web/phone client) and the iTerm2
connection. Terminal output and window/tab state are PUSHED from iTerm2's
notification streams instead of being polled, so no per-call processes are
spawned. The Socket.IO contract retains the upstream event vocabulary while
adding pane watches and structured, styled terminal content for the rewritten
client.
"""
import asyncio
import ctypes
import json
import os
import resource
import signal
import sys
import time
from datetime import datetime

import iterm2
import iterm2.rpc
import socketio
from aiohttp import web

try:
    from .auth import is_valid_key, load_or_create_key
    from .geometry import pane_layout
except ImportError:  # Running server.py directly from the server directory.
    from auth import is_valid_key, load_or_create_key
    from geometry import pane_layout

PORT = 7291

# Watched panes receive only their recent tail on each screen change. Older
# content is fetched in pages when the client scrolls upward, avoiding a full
# scrollback read (and a large Socket.IO payload) for every keypress.
LIVE_CONTENT_LINES = 250
HISTORY_PAGE_LINES = 500
PREVIEW_CONTENT_LINES = 40
# At most ~7 live snapshots per second per session. iTerm2 answers API
# requests on its main thread, so every content read competes with its own
# rendering: at 20/s a pair of watched panes running a build kept iTerm2
# saturated, which stalled *all* API clients (measured: p50 RPC latency 2ms
# idle vs 20ms with the stack live, with multi-second spikes). A phone can't
# usefully render 20 frames/second of terminal text anyway, and the cost is
# at most one extra STREAM_MIN_INTERVAL of latency after a keystroke.
STREAM_MIN_INTERVAL = 0.15
MAX_SOCKET_QUEUE_DEPTH = 2
MAX_WATCHED_SESSIONS = 2

# How often the watchdog proves the iTerm2 connection is actually answering
# RPCs (not just open), and how long it waits for one round trip.
WATCHDOG_INTERVAL = 15.0
WATCHDOG_TIMEOUT = 20.0
# How long to wait before re-probing after a failed probe (rather than the
# full WATCHDOG_INTERVAL), so a connection that really is dead in a way we
# can't detect directly is still declared dead within ~90s.
WATCHDOG_RETRY_INTERVAL = 3.0
# Consecutive failed probes tolerated before declaring an *undetectably*
# broken connection dead. A slow probe is not a broken connection: iTerm2
# services API requests on its main thread, so a loaded Mac stalls every
# API client with nothing actually wrong — measured on this machine, 3.5s
# round trips with the stack idle and a 65s stall under load. Restarting on
# a single timeout turned each of those hiccups into a full process restart,
# which dropped every phone client and — because the restart re-read every
# pane — provoked the next stall.
#
# Deliberately patient (~4 minutes of unbroken silence at these intervals),
# because this path has almost nothing left to catch. iTerm2 3.3.12+ serves
# the API over a unix domain socket, which cannot go half-open: if iTerm2
# exits or closes it, the read loop gets EOF at once and
# _iterm_connection_dead reports it on the very first failed probe. Only the
# legacy TCP transport can hang silently, so waiting longer costs nothing
# real and a false restart costs every connected phone.
WATCHDOG_FAILURES_BEFORE_RESTART = 10
SHUTDOWN_TIMEOUT = 10.0

# Coroutine qualnames whose death means "the iTerm2 connection is broken",
# not "an isolated per-client/per-session bug" (those already contain their
# own errors — see stream_session and _deliver_client_events). An unhandled
# exception from one of these is treated the same as the watchdog's own
# detected failure: log it and exit for a supervised restart.
_CRITICAL_TASK_NAMES = {
    "layout_monitor", "focus_monitor", "sync_loop", "connection_watchdog",
    "Connection._async_dispatch_forever",
}


def log(*args) -> None:
    print(datetime.now().isoformat(timespec="seconds"), *args)

sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
app = web.Application()
sio.attach(app)

clients: set[str] = set()

connection: iterm2.Connection | None = None
itermapp: iterm2.App | None = None
shared_key: str | None = None

screen_size = {"width": 1470, "height": 956}

# Sessions each connected client is actively viewing (its main pane + optional
# split pane). We live-stream exactly the union of these, independent of which
# session iTerm has focused on the Mac — so the phone can watch any pane it
# likes (including a background split pane) and still get live updates.
watched_by_sid: dict[str, set[str]] = {}
stream_tasks: dict[str, asyncio.Task] = {}
last_content: dict[str, dict] = {}
stream_reconcile_lock = asyncio.Lock()

# sid -> in-flight seed_client task, so a client that disconnects mid-seed
# (the common case when iTerm2 is slow) doesn't leave one running.
seed_tasks: dict[str, asyncio.Task] = {}

# Socket.IO/Engine.IO queues are unbounded. Keep a bounded, latest-wins
# application outbox per client and stop feeding Engine.IO while its real
# socket queue is backed up (for example when a phone sleeps).
pending_events: dict[str, dict[str, tuple[str, object]]] = {}
delivery_wakeups: dict[str, asyncio.Event] = {}
delivery_tasks: dict[str, asyncio.Task] = {}

# Per-session color palette (default fg/bg + ANSI 0-15), read once from the
# session's iTerm2 profile so standard colors match the user's actual theme.
palette_cache: dict[str, dict] = {}


# --- Screen geometry (CoreGraphics, no AppKit/osascript) -----------------------

class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


def read_screen_size() -> dict:
    try:
        core_graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        core_graphics.CGMainDisplayID.restype = ctypes.c_uint32
        core_graphics.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        core_graphics.CGDisplayBounds.restype = _CGRect
        bounds = core_graphics.CGDisplayBounds(core_graphics.CGMainDisplayID())
        if bounds.size.width <= 0 or bounds.size.height <= 0:
            raise RuntimeError("CoreGraphics did not return a display")
        return {
            "width": int(bounds.size.width),
            "height": int(bounds.size.height),
        }
    except Exception:
        return {"width": 1470, "height": 956}


# --- State serialization (windows / tabs / sessions) ---------------------------

async def build_state() -> list:
    """Mirror the old AppleScript getState() shape.

    Window/tab ids are opaque to the client; session ids are iTerm2 GUIDs and
    match what the client already persists. Bounds are converted from iTerm2's
    bottom-left Cocoa origin to the top-left origin the client's window map
    expects.
    """
    if itermapp is None:
        return []
    current_window = itermapp.current_window
    current_window_id = current_window.window_id if current_window else None
    flip_height = screen_size["height"]
    state = []
    for window in itermapp.windows:
        try:
            frame = await window.async_get_frame()
            bounds = {
                "x": int(frame.origin.x),
                "y": int(flip_height - (frame.origin.y + frame.size.height)),
                "w": int(frame.size.width),
                "h": int(frame.size.height),
            }
        except Exception:
            bounds = {"x": 0, "y": 0, "w": 800, "h": 600}

        current_tab = window.current_tab
        current_tab_id = current_tab.tab_id if current_tab else None
        tabs = []
        for index, tab in enumerate(window.tabs, start=1):
            # The tab bar in iTerm shows the tab's title (which a worktree/script
            # may set independently of the session name); mirror that.
            try:
                tab_title = await tab.async_get_variable("title")
            except Exception:
                tab_title = None
            rects, aspect, maximized = pane_layout(tab)
            sessions = []
            # all_sessions (not sessions) so a tab with a maximized pane still
            # reports every pane — the rest are "minimized", which tab.sessions
            # excludes. session.name avoids a per-pane RPC and is set for
            # minimized panes too.
            for session in tab.all_sessions:
                entry = {"id": session.session_id, "name": session.name or ""}
                r = rects.get(session.session_id)
                if r is not None:
                    entry["rect"] = {"x": round(r[0], 4), "y": round(r[1], 4),
                                     "w": round(r[2], 4), "h": round(r[3], 4)}
                sessions.append(entry)
            # Which pane iTerm has focused in this tab; the client uses it as the
            # default pane to show when you switch to the tab.
            cur_sess = tab.current_session
            tabs.append({
                "index": index,
                "id": f"{window.window_id}-{index}",
                "title": tab_title or "",
                "isSelected": tab.tab_id == current_tab_id,
                "currentSessionId": cur_sess.session_id if cur_sess else "",
                "aspect": round(aspect, 4),
                "maximized": maximized,
                "sessions": sessions,
            })

        state.append({
            "id": window.window_id,
            "isFront": window.window_id == current_window_id,
            "tabs": tabs,
            "bounds": bounds,
        })
    return state


_last_pushed_state: str | None = None

# Set by the iTerm2 notification monitors instead of having each notification
# rebuild state inline. build_state() costs one RPC per window plus one per
# tab, and iTerm2 emits layout/focus notifications in bursts (a pane opening
# a split, a tab switch, any session whose title or job changes), so an
# inline rebuild multiplied one burst into dozens of RPCs against iTerm2's
# main thread. Coalescing them costs at most STATE_PUSH_MIN_INTERVAL of
# staleness — and push_state already suppresses the emit when nothing
# actually changed, so the client sees no difference.
_state_dirty = asyncio.Event()
STATE_PUSH_MIN_INTERVAL = 0.5


def _socket_queue_depth(sid: str) -> int | None:
    """Return Engine.IO's real outbound queue depth for a Socket.IO client."""
    eio_sid = sio.manager.eio_sid_from_sid(sid, "/")
    socket = sio.eio.sockets.get(eio_sid) if eio_sid else None
    return socket.queue.qsize() if socket is not None else None


def queue_client_event(
        sid: str, key: str, event: str, data: object) -> None:
    """Queue one latest-wins event without growing an unbounded backlog."""
    if sid not in clients:
        return
    outbox = pending_events.get(sid)
    wakeup = delivery_wakeups.get(sid)
    if outbox is None or wakeup is None:
        return
    outbox[key] = (event, data)
    wakeup.set()


async def _deliver_client_events(sid: str) -> None:
    """Feed Engine.IO only while a client has capacity for another packet."""
    wakeup = delivery_wakeups[sid]
    outbox = pending_events[sid]
    while sid in clients:
        await wakeup.wait()
        wakeup.clear()
        while sid in clients and outbox:
            depth = _socket_queue_depth(sid)
            if depth is None:
                return
            if depth >= MAX_SOCKET_QUEUE_DEPTH:
                await asyncio.sleep(STREAM_MIN_INTERVAL)
                continue
            key = next(iter(outbox))
            event, data = outbox.pop(key)
            await sio.emit(event, data, to=sid)


def start_client_delivery(sid: str) -> None:
    pending_events[sid] = {}
    delivery_wakeups[sid] = asyncio.Event()
    delivery_tasks[sid] = asyncio.create_task(_deliver_client_events(sid))


async def stop_client_delivery(sid: str) -> None:
    task = delivery_tasks.pop(sid, None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    pending_events.pop(sid, None)
    delivery_wakeups.pop(sid, None)


def queue_content_for_watchers(session_id: str, content: dict) -> None:
    data = {"sessionId": session_id, **content}
    for sid, watched in list(watched_by_sid.items()):
        if session_id in watched:
            queue_client_event(
                sid, f"content:{session_id}", "content", data)


async def push_state() -> None:
    """Broadcast window/tab state, skipping the emit when nothing changed."""
    global _last_pushed_state
    if not clients:
        return
    state = await build_state()
    serialized = json.dumps(state, sort_keys=True)
    if serialized == _last_pushed_state:
        return
    _last_pushed_state = serialized
    for sid in list(clients):
        queue_client_event(sid, "state", "state", state)


async def sync_loop() -> None:
    """Safety-net refresh for changes the notification monitors don't surface
    (job/title updates, pure window moves). Cheap API calls only, gated on
    connected clients, and deduplicated by push_state so it emits only on real
    changes — this is what keeps tab labels fresh without polling iTerm2 via
    per-call subprocesses the way the old AppleScript server did.
    """
    while True:
        await asyncio.sleep(2.0)
        if clients:
            await push_state()


async def health_loop() -> None:
    """Periodic bounded-queue and peak-RSS telemetry for leak diagnosis."""
    while True:
        await asyncio.sleep(60)
        queue_depths = [
            depth for sid in clients
            if (depth := _socket_queue_depth(sid)) is not None
        ]
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        max_rss_mb = max_rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        log(
            "health: "
            f"max_rss={max_rss_mb:.1f}MB clients={len(clients)} "
            f"streams={len(stream_tasks)} pending={sum(map(len, pending_events.values()))} "
            f"engineio_queue_max={max(queue_depths, default=0)}")


# --- Reading terminal contents with faithful colors ---------------------------
#
# Each cell's style gives a foreground/background that is either true-color RGB,
# a standard ANSI index (0-255), or "default". We resolve those to hex strings
# (mapping ANSI 0-15 + default through the session's actual iTerm2 theme, and
# 16-255 through the xterm-256 palette) and emit run-length-grouped colored runs.

_DEFAULT_ANSI = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]
_DEFAULT_PALETTE = {"fg": "#d4d4d8", "bg": "#0a0a0a", "ansi": _DEFAULT_ANSI}


def _hex(rgb) -> str:
    # iTerm2 profile colors are floats (0-255); cell colors are ints. Normalize.
    return "#" + "".join(f"{min(255, max(0, int(round(c)))):02x}" for c in rgb)


def _xterm256(idx: int):
    if idx < 16:
        return _DEFAULT_ANSI[idx]
    if idx < 232:
        n = idx - 16
        levels = (0, 95, 135, 175, 215, 255)
        return (levels[n // 36], levels[(n // 6) % 6], levels[n % 6])
    gray = 8 + 10 * (idx - 232)
    return (gray, gray, gray)


def _system_is_dark() -> bool:
    try:
        import Foundation
        defaults = Foundation.NSUserDefaults.standardUserDefaults()
        return defaults.stringForKey_("AppleInterfaceStyle") == "Dark"
    except Exception:
        return True


async def get_palette(session) -> dict:
    pal = palette_cache.get(session.session_id)
    if pal is not None:
        return pal
    try:
        profile = await session.async_get_profile()
        # Honor "separate colors for light and dark mode": read the variant that
        # matches the current system appearance, so colors match the display.
        suffix = ""
        if profile.use_separate_colors_for_light_and_dark_mode:
            suffix = "_dark" if _system_is_dark() else "_light"

        def rgb(name):
            c = getattr(profile, name + suffix)
            return (c.red, c.green, c.blue)

        pal = {
            "fg": _hex(rgb("foreground_color")),
            "bg": _hex(rgb("background_color")),
            "ansi": [rgb(f"ansi_{i}_color") for i in range(16)],
        }
    except Exception:
        pal = _DEFAULT_PALETTE
    palette_cache[session.session_id] = pal
    return pal


def _resolve(color, pal) -> str | None:
    """A CellStyle.Color -> hex string, or None meaning the pane default."""
    if color.is_rgb:
        c = color.rgb
        return _hex((c.red, c.green, c.blue))
    if color.is_standard:
        idx = color.standard
        return _hex(pal["ansi"][idx] if idx < 16 else _xterm256(idx))
    return None  # alternate / default


def _line_runs(line, pal, cursor_x: int | None = None) -> list:
    """Group a line into styled runs, optionally marking its cursor cell."""
    runs: list = []
    cur = None
    buf = ""
    x = 0
    while True:
        style = line.style_at(x)
        if style is None:
            break
        if cursor_x == x:
            if buf:
                runs.append(_make_run(cur, buf))
                buf = ""
            # The client renders this as a zero-width overlay, so the cursor
            # does not move the character underneath it.
            runs.append({"t": "", "c": True})
        ch = line.string_at(x).replace("\x00", " ")
        fg = _resolve(style.fg_color, pal)
        bg = _resolve(style.bg_color, pal)
        if style.inverse:
            fg, bg = (bg or pal["bg"]), (fg or pal["fg"])
        key = (fg, bg, bool(style.bold), bool(style.faint))
        if key != cur:
            if buf:
                runs.append(_make_run(cur, buf))
            cur, buf = key, ch
        else:
            buf += ch
        x += 1
    if buf:
        runs.append(_make_run(cur, buf))
    if cursor_x is not None and cursor_x >= x:
        if cursor_x > x:
            runs.append({"t": " " * (cursor_x - x)})
        runs.append({"t": "", "c": True})
    # Trim trailing whitespace runs that carry no background.
    # Never trim through the cursor: it may be on an otherwise blank line.
    while (runs and not runs[-1].get("c") and "g" not in runs[-1]
           and not runs[-1]["t"].strip()):
        runs.pop()
    if runs and not runs[-1].get("c") and "g" not in runs[-1]:
        runs[-1]["t"] = runs[-1]["t"].rstrip()
    return runs


def _make_run(key, text: str) -> dict:
    fg, bg, bold, dim = key
    run = {"t": text}
    if fg:
        run["f"] = fg
    if bg:
        run["g"] = bg
    if bold:
        run["b"] = True
    if dim:
        run["d"] = True
    return run


def _content_line_range(
        info, line_count: int, before_line: int | None = None
) -> tuple[int, int, int, int]:
    """Return a page within the complete range retained by iTerm.

    Absolute line numbers below ``overflow`` have already been discarded by
    iTerm. Everything after it consists of the session's scrollback plus its
    mutable screen area and is available through ``async_get_contents``.
    """
    available_first = info.overflow
    terminal_end = (available_first + info.scrollback_buffer_height
                    + info.mutable_area_height)
    page_end = terminal_end
    if before_line is not None:
        page_end = min(page_end, max(available_first, before_line))
    first = max(available_first, page_end - line_count)
    return first, page_end - first, available_first, terminal_end


async def read_content(
        session_id: str | None,
        line_count: int = LIVE_CONTENT_LINES,
        before_line: int | None = None,
        screen=None,
) -> dict | None:
    if itermapp is None or not session_id or session_id == "undefined":
        return None
    session = itermapp.get_session_by_id(session_id)
    if session is None:
        return None
    try:
        pal = await get_palette(session)
        # Deliberately not wrapped in an iterm2.Transaction: a transaction
        # blocks iTerm2's entire main thread until it is explicitly ended, and
        # this read runs inside a cancelable stream task (cancelled e.g. on
        # client disconnect). If cancellation lands between the BEGIN and END
        # RPCs, iTerm2 hangs forever waiting on a END that will never come —
        # freezing its whole GUI, not just this server. async_get_contents
        # already tolerates the screen changing between these two calls by
        # returning fewer lines than requested rather than raising, so a rare
        # race here just yields a slightly short frame that the next stream
        # tick (at most STREAM_MIN_INTERVAL later) corrects.
        info = await session.async_get_line_info()
        if screen is None:
            screen = await session.async_get_screen_contents()
        first, count, available_first, terminal_end = _content_line_range(
            info, line_count, before_line)
        if count <= 0:
            return {
                "lines": [], "fg": pal["fg"], "bg": pal["bg"],
                "firstLine": first, "availableFirstLine": available_first,
                "terminalEnd": terminal_end,
                "isLatest": before_line is None,
            }
        lines = await session.async_get_contents(first, count)
        cursor = screen.cursor_coord
        rendered = [
            _line_runs(line, pal, cursor.x if first + i == cursor.y else None)
            for i, line in enumerate(lines)
        ]
        return {
            "lines": rendered, "fg": pal["fg"], "bg": pal["bg"],
            "firstLine": first, "availableFirstLine": available_first,
            "terminalEnd": terminal_end,
            "isLatest": before_line is None,
        }
    except Exception:
        return None


# --- Active-session selection helpers ------------------------------------------

def current_session() -> iterm2.Session | None:
    window = itermapp.current_window if itermapp else None
    if window is None:
        return None
    tab = window.current_tab
    return tab.current_session if tab else None


def current_active_session_id() -> str | None:
    session = current_session()
    return session.session_id if session else None


def resolve_session(session_id: str | None) -> iterm2.Session | None:
    if itermapp is not None and session_id and session_id != "undefined":
        session = itermapp.get_session_by_id(session_id)
        if session is not None:
            return session
    return current_session()


# --- Live streaming of watched sessions ----------------------------------------
#
# The client tells us which sessions it is viewing (the `watch` event); we run
# one screen-streamer task per distinct watched session and push its content on
# every change. This is decoupled from iTerm's own focus, so the phone can watch
# any pane — including a background split pane — and still get live updates.

async def stream_session(session_id: str) -> None:
    session = itermapp.get_session_by_id(session_id) if itermapp else None
    if session is None:
        return

    content = await read_content(session_id)
    if content is not None and content["lines"] and clients:
        last_content[session_id] = content
        queue_content_for_watchers(session_id, content)

    try:
        # The notification itself is enough: waiting briefly before fetching the
        # current screen coalesces bursts of changes and avoids allocating a
        # stale screen snapshot for every byte of fast terminal output.
        async with session.get_screen_streamer(want_contents=False) as streamer:
            while True:
                await streamer.async_get()
                await asyncio.sleep(STREAM_MIN_INTERVAL)
                if not clients:
                    continue
                content = await read_content(session_id)
                if content is not None and content != last_content.get(session_id):
                    last_content[session_id] = content
                    queue_content_for_watchers(session_id, content)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        log(f"stream error ({session_id}): {err}")
    finally:
        last_content.pop(session_id, None)


async def apply_watches() -> None:
    """Reconcile running stream tasks with the union of watched sessions."""
    async with stream_reconcile_lock:
        union: set[str] = set()
        for ids in watched_by_sid.values():
            union |= ids

        finished = []
        for session_id, task in list(stream_tasks.items()):
            if session_id not in union:
                task.cancel()
                finished.append(task)
                stream_tasks.pop(session_id, None)
            elif task.done():
                finished.append(task)
                stream_tasks.pop(session_id, None)  # restart below if wanted

        if finished:
            await asyncio.gather(*finished, return_exceptions=True)

        if clients:
            for session_id in union:
                if session_id not in stream_tasks:
                    stream_tasks[session_id] = asyncio.create_task(
                        stream_session(session_id))


async def stop_all_streams() -> None:
    async with stream_reconcile_lock:
        tasks = list(stream_tasks.values())
        stream_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# --- iTerm2 notification monitors (replace the old poll loops) -----------------

async def layout_monitor() -> None:
    async with iterm2.LayoutChangeMonitor(connection) as monitor:
        while True:
            await monitor.async_get()
            _state_dirty.set()


async def focus_monitor() -> None:
    async with iterm2.FocusMonitor(connection) as monitor:
        while True:
            await monitor.async_get_next_update()
            _state_dirty.set()


async def state_push_loop() -> None:
    """Rebuilds and broadcasts state for the notification monitors, at most
    once per STATE_PUSH_MIN_INTERVAL. Errors are logged and swallowed: a
    single failed rebuild (a window closing mid-build, say) must not kill the
    loop, or state would silently stop updating for the rest of the session.
    """
    while True:
        await _state_dirty.wait()
        _state_dirty.clear()
        try:
            await push_state()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log(f"state push failed: {err!r}")
        await asyncio.sleep(STATE_PUSH_MIN_INTERVAL)


def _iterm_connection_dead() -> bool:
    """True only for a connection that can never answer again.

    This is the distinction the watchdog turns on: *stalled* is not *dead*.
    iTerm2 answers API requests on its main thread, so a busy Mac makes every
    RPC slow — including the watchdog's own probe — while the connection is
    perfectly healthy and will answer as soon as iTerm2 catches up. A timed
    out probe is also harmless in itself: the iterm2 library matches
    responses to requests by id and drops the receiver when the late response
    finally arrives, so an abandoned probe leaves nothing behind.

    Two states genuinely are unrecoverable, because the library never
    reconnects mid-session:

    * the websocket is closed; or
    * `_async_dispatch_forever`, the library's read loop, has exited. It is
      the only thing that resolves pending responses, so once it is gone
      every future call hangs forever with nothing to raise.
    """
    if connection is None:
        return True
    websocket = getattr(connection, "websocket", None)
    if websocket is None:
        return True
    # The `websockets` legacy protocol the iterm2 library asks for exposes
    # `closed`; its modern client exposes `close_code` instead. An object
    # with neither is treated as alive rather than guessed at.
    closed = getattr(websocket, "closed", None)
    if closed is None:
        closed = getattr(websocket, "close_code", None) is not None
    if closed:
        return True
    # Name-mangled because the library keeps the read loop private. The
    # getattr default means a future library version that renames it costs
    # us this fast path, not a crash — the consecutive-failure count below
    # still catches the same failure, just more slowly.
    dispatcher = getattr(connection, "_Connection__dispatch_forever_future", None)
    return dispatcher is not None and dispatcher.done()


async def connection_watchdog(stop: asyncio.Event) -> None:
    """Proves the iTerm2 connection is actually answering RPCs, not just
    still open. The iterm2 library never reconnects mid-session, and a dead
    connection is silent rather than raising, so it isn't caught by the
    exception handler installed in main() — this periodic probe is the only
    thing that catches it. On a confirmed failure this exits the process for
    a supervised restart, which is far more reliable than trying to repair a
    half-broken connection in place (every notification subscription and
    cached App/Session reference would need re-establishing).

    A restart is expensive — it drops every connected phone — so it takes
    real evidence, not one slow round trip. `_iterm_connection_dead` catches
    the detectable deaths immediately; anything else has to fail
    WATCHDOG_FAILURES_BEFORE_RESTART probes in a row to count.
    """
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                iterm2.rpc.async_list_sessions(connection),
                timeout=WATCHDOG_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            failures += 1
            elapsed = time.monotonic() - started
            if _iterm_connection_dead():
                log(f"watchdog: iTerm2 connection is dead after {elapsed:.1f}s "
                    f"({err!r}); restarting")
                stop.set()
                return
            if failures >= WATCHDOG_FAILURES_BEFORE_RESTART:
                log(f"watchdog: iTerm2 unresponsive for {failures} probes in a "
                    f"row ({err!r}); restarting")
                stop.set()
                return
            log(f"watchdog: iTerm2 probe timed out after {elapsed:.1f}s "
                f"({failures}/{WATCHDOG_FAILURES_BEFORE_RESTART}) — connection "
                "still open, so iTerm2 is busy rather than gone; waiting")
        else:
            if failures:
                log(f"watchdog: iTerm2 answering again after {failures} slow "
                    "probe(s)")
            failures = 0
        delay = WATCHDOG_RETRY_INTERVAL if failures else WATCHDOG_INTERVAL
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


# --- Socket.IO handlers ---------------------------------------------------------

async def seed_client(sid: str) -> None:
    """Give a freshly connected client its window/tab state and a first
    screenful, so it has something to draw before its own `watch` arrives.

    Runs as a task rather than inline in `connect` — see the note there.
    """
    try:
        await sio.emit("screenSize", screen_size, to=sid)
        await sio.emit("state", await build_state(), to=sid)
        # A snapshot of whatever iTerm currently has focused.
        session_id = current_active_session_id()
        if session_id and sid in clients:
            content = await read_content(session_id)
            if content and content["lines"]:
                await sio.emit(
                    "content", {"sessionId": session_id, **content}, to=sid)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        log(f"seeding client {sid} failed: {err!r}")
    finally:
        seed_tasks.pop(sid, None)


@sio.event
async def connect(sid, environ, auth=None):
    supplied_key = auth.get("key") if isinstance(auth, dict) else None
    if not is_valid_key(shared_key, supplied_key):
        log(f"Rejected unauthenticated client: {sid}")
        raise socketio.exceptions.ConnectionRefusedError("invalid access key")
    clients.add(sid)
    start_client_delivery(sid)
    log(f"Client connected: {sid}")
    # Seeding is deferred to a task because python-socketio only acknowledges
    # the connection once this handler returns, and seeding needs a dozen
    # iTerm2 RPCs. Inline, that made the Socket.IO handshake as slow as
    # iTerm2's slowest moment: the client gives up after 10s and reconnects,
    # so on a busy Mac it sat in an endless RECONNECTING loop, each attempt
    # dying just before the state it was waiting for arrived. The client
    # already accepts `state` and `content` whenever they turn up.
    seed_tasks[sid] = asyncio.create_task(seed_client(sid))


@sio.event
async def disconnect(sid, reason=None):
    clients.discard(sid)
    watched_by_sid.pop(sid, None)
    seed = seed_tasks.pop(sid, None)
    if seed is not None:
        seed.cancel()
    await stop_client_delivery(sid)
    log(f"Client disconnected: {sid}")
    if not clients:
        await stop_all_streams()
    else:
        await apply_watches()


@sio.on("watch")
async def on_watch(sid, data):
    # The client lists the sessions it is currently viewing (main + split panes);
    # we live-stream exactly that union across all clients.
    ids = data.get("sessionIds") or []
    sessions = {
        s for s in ids[:MAX_WATCHED_SESSIONS] if s and s != "undefined"
    }
    watched_by_sid[sid] = sessions

    # Immediately hand this client a snapshot of any pane that is *already* being
    # streamed for another connection. A shared per-session stream emits its first
    # frame only when its task is created (see stream_session), so a client that
    # starts watching an already-streamed pane — e.g. a phone reconnecting after
    # sleep while its previous connection's stream is still alive — would
    # otherwise sit on "WAITING FOR OUTPUT" until that pane next changes. Panes
    # not yet streamed are covered by the initial read in stream_session(), which
    # apply_watches() starts below, so only pre-existing streams are seeded here.
    for session_id in sessions:
        if session_id not in stream_tasks:
            continue
        content = last_content.get(session_id)
        if content is None:
            content = await read_content(session_id)
        if content and content["lines"]:
            queue_client_event(
                sid, f"content:{session_id}", "content",
                {"sessionId": session_id, **content})

    await apply_watches()


@sio.event
async def ping(sid):
    # Returning sends the Socket.IO ack with no args -> client latency callback.
    return


@sio.on("getContent")
async def on_get_content(sid, data):
    session_id = data.get("sessionId")
    content = await read_content(session_id)
    if content and content["lines"]:
        queue_client_event(
            sid, f"content:{session_id}", "content",
            {"sessionId": session_id, **content})


@sio.on("getAllContent")
async def on_get_all_content(sid, data):
    session_ids = data.get("sessionIds") or []

    async def one(session_id):
        content = await read_content(session_id, PREVIEW_CONTENT_LINES)
        if content and content["lines"]:
            queue_client_event(
                sid, f"content:{session_id}", "content",
                {"sessionId": session_id, **content})

    await asyncio.gather(*(one(s) for s in session_ids))


@sio.on("getEarlierContent")
async def on_get_earlier_content(sid, data):
    session_id = data.get("sessionId")
    before_line = data.get("beforeLine")
    if not isinstance(before_line, int):
        return
    content = await read_content(
        session_id, HISTORY_PAGE_LINES, before_line=before_line)
    if content:
        queue_client_event(
            sid, f"history:{session_id}:{before_line}", "historyContent",
            {"sessionId": session_id, **content})


@sio.on("execute")
async def on_execute(sid, data):
    command = data.get("command", "")
    session = resolve_session(data.get("sessionId"))
    if session is not None:
        # CR (0x0D), not LF: a real Return submits in shells (ICRNL maps CR->NL)
        # and in raw-mode TUIs like Claude Code, where LF only inserts a newline.
        await session.async_send_text(command + "\r")


@sio.on("broadcast")
async def on_broadcast(sid, data):
    command = data.get("command", "")
    session_ids = data.get("sessionIds") or []
    sessions = [itermapp.get_session_by_id(s) for s in session_ids] if itermapp else []
    await asyncio.gather(*(
        s.async_send_text(command + "\r") for s in sessions if s is not None))
    await push_state()


@sio.on("sendKeys")
async def on_send_keys(sid, data):
    keys = data.get("keys", "")
    session = resolve_session(data.get("sessionId"))
    if session is not None:
        await session.async_send_text(keys)


@sio.on("newTab")
async def on_new_tab(sid):
    window = itermapp.current_window if itermapp else None
    if window is not None:
        await window.async_create_tab()
    await push_state()


@sio.on("closeTab")
async def on_close_tab(sid):
    session = current_session()
    if session is not None:
        await session.async_close()
    await push_state()


@sio.on("splitPane")
async def on_split_pane(sid, data):
    # sessionId follows the same resolve_session convention as sendKeys/
    # execute/broadcast: split whichever pane the phone currently has
    # focused, which may differ from iTerm's own Mac-side focus.
    session = resolve_session((data or {}).get("sessionId"))
    if session is None:
        return
    vertical = bool((data or {}).get("vertical", True))
    try:
        await session.async_split_pane(vertical=vertical)
    except Exception as err:
        log(f"split pane failed: {err}")
    await push_state()


@sio.on("renameSession")
async def on_rename_session(sid, data):
    session_id = data.get("sessionId")
    name = data.get("name")
    if itermapp is None or not session_id or session_id == "undefined" or not name:
        return
    session = itermapp.get_session_by_id(session_id)
    if session is not None:
        await session.async_set_name(name)
    await push_state()


@sio.on("focus")
async def on_focus(sid, data):
    if itermapp is None:
        return
    window_id = data.get("windowId")
    tab_index = data.get("tabIndex") or 0
    if not window_id:
        return
    window = itermapp.get_window_by_id(str(window_id))
    if window is None:
        return

    await window.async_activate()
    await itermapp.async_activate()

    if 1 <= tab_index <= len(window.tabs):
        await window.tabs[tab_index - 1].async_activate()

    await push_state()


# --- Entry point ---------------------------------------------------------------

async def main() -> None:
    global connection, itermapp, screen_size, shared_key
    screen_size = read_screen_size()
    log("Screen size:", screen_size)

    shared_key = load_or_create_key()

    connection = await iterm2.Connection.async_create()
    itermapp = await iterm2.async_get_app(connection)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"Server running on http://0.0.0.0:{PORT}")

    # `stop` is shared by three independent triggers: a real shutdown signal,
    # connection_watchdog's periodic probe, and the exception handler below
    # (for a critical task that died outright instead of just going silent).
    # `signal_stop` distinguishes the first from the other two so we can exit
    # non-zero on an unplanned restart — purely diagnostic; the AutoLaunch
    # supervisor's restart trigger only looks at whether port 7291 is open.
    stop = asyncio.Event()
    signal_stop = False

    def handle_asyncio_exception(loop, context) -> None:
        exc = context.get("exception")
        # A Task's exception going unretrieved (our actual failure mode: a
        # fire-and-forget background task dies and nothing ever awaits it)
        # is reported by asyncio via Future.__del__ using the 'future' key,
        # not 'task' — only the unrelated "destroyed while pending" warning
        # uses 'task'. Check both.
        holder = context.get("task") or context.get("future")
        coro = holder.get_coro() if hasattr(holder, "get_coro") else None
        name = getattr(coro, "__qualname__", "") if coro is not None else ""
        log(f"unhandled exception in {name or 'event loop'}: "
            f"{context.get('message')}: {exc!r}")
        if name in _CRITICAL_TASK_NAMES and not stop.is_set():
            log(f"{name} died — treating as a broken iTerm2 connection; "
                "restarting")
            stop.set()

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(handle_asyncio_exception)

    background_tasks = [
        asyncio.create_task(layout_monitor()),
        asyncio.create_task(focus_monitor()),
        asyncio.create_task(state_push_loop()),
        asyncio.create_task(sync_loop()),
        asyncio.create_task(health_loop()),
        asyncio.create_task(connection_watchdog(stop)),
    ]

    # Clean shutdown (incl. `iterm-server stop`, which sends SIGTERM).
    def handle_signal() -> None:
        nonlocal signal_stop
        signal_stop = True
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except (NotImplementedError, RuntimeError):
            pass

    await stop.wait()
    if not signal_stop:
        log("shutting down for a supervised restart")

    async def cleanup() -> None:
        # Free the listening socket first. Everything that decides whether
        # this server is up — the AutoLaunch supervisor, `iterm-server
        # start`, `do_stop`'s wait — probes port 7291, so a port still bound
        # through a slow teardown reads as "still running" and holds up its
        # replacement. site.stop() only closes the listener; the part that
        # can take a while is runner.cleanup() below, which waits for open
        # connections (a connected phone's websocket) to finish.
        await site.stop()
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await stop_all_streams()
        for sid in list(delivery_tasks):
            clients.discard(sid)
            await stop_client_delivery(sid)
        await runner.cleanup()

    try:
        await asyncio.wait_for(cleanup(), timeout=SHUTDOWN_TIMEOUT)
    except asyncio.TimeoutError:
        # Cleanup itself hanging would defeat the whole point of this
        # watchdog: guarantee the process actually exits so the supervisor
        # can restart it. os._exit skips further asyncio/atexit machinery
        # entirely rather than risk hanging a second time.
        log("cleanup did not finish in time; forcing exit")
        os._exit(1)

    if not signal_stop:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
