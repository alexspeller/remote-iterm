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
import functools
import json
import os
import re
import resource
import signal
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime

import iterm2
import iterm2.rpc
import socketio
from aiohttp import web
import engineio.async_drivers.aiohttp as engineio_aiohttp

try:
    from .auth import (
        COOKIE_MAX_AGE,
        COOKIE_NAME,
        is_trusted_origin,
        is_valid_key,
        key_from_cookie_header,
        load_or_create_key,
    )
    from .geometry import pane_layout
    from .iterm_cookie import use_osascript_for_cookie
    from .live_tail import LiveTail, TailFrame
    from .terminal_lines import async_get_lines_and_cursor
except ImportError:  # Running server.py directly from the server directory.
    from auth import (
        COOKIE_MAX_AGE,
        COOKIE_NAME,
        is_trusted_origin,
        is_valid_key,
        key_from_cookie_header,
        load_or_create_key,
    )
    from geometry import pane_layout
    from iterm_cookie import use_osascript_for_cookie
    from live_tail import LiveTail, TailFrame
    from terminal_lines import async_get_lines_and_cursor

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
# A read that took iTerm2 a while says its main thread is loaded, so the next
# one waits in proportion (twice the read's duration), up to this ceiling.
# The alternative — reading again at full rate — adds to the very queue the
# phone's keystrokes are waiting in.
STREAM_MAX_INTERVAL = 2.0
MAX_SOCKET_QUEUE_DEPTH = 2
MAX_WATCHED_SESSIONS = 2

# The last frame read for recently viewed panes, so a phone that reconnects
# or reloads draws something at once instead of WAITING FOR OUTPUT until
# iTerm2 answers — which, on a Mac short of memory, has taken over a minute.
# Bounded by count and pruned of closed panes on every state push.
CONTENT_CACHE_SIZE = 48

# How often window/tab state is re-read for changes no notification reports
# (tab titles). Only a phone looks at those live; other clients (aveiro keeps
# one connected permanently) get the slower rate, so an idle Mac is not
# re-read every two seconds around the clock.
SYNC_INTERVAL_BROWSER = 2.0
SYNC_INTERVAL_BACKGROUND = 10.0

# How often the watchdog proves the iTerm2 connection is actually answering
# RPCs (not just open), and how long it waits for one round trip. While a
# phone is connected it probes more often, since the same probe is what tells
# the phone that iTerm2 has stopped answering.
WATCHDOG_INTERVAL = 15.0
WATCHDOG_INTERVAL_BROWSER = 3.0
WATCHDOG_TIMEOUT = 20.0
# A probe still unanswered after this long marks iTerm2 as stalled for the
# phones, which say so rather than looking broken.
STALL_AFTER = 3.0
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
# Deliberately patient (~15 minutes of unbroken silence at these intervals),
# because this path has almost nothing left to catch. iTerm2 3.3.12+ serves
# the API over a unix domain socket, which cannot go half-open: if iTerm2
# exits or closes it, the read loop gets EOF at once and
# _iterm_connection_dead reports it on the very first failed probe. Only the
# legacy TCP transport can hang silently, so waiting longer costs nothing
# real and a false restart costs every connected phone. It was ~4 minutes
# until a Mac short of memory stalled iTerm2 for 158s on 2026-09-28: a
# restart during a stall cannot help (the new server cannot connect until
# iTerm2 answers either), and each one leaves its Script Console entry in
# iTerm2's memory for good (see docs/ARCHITECTURE.md, "Why iTerm2 stalls").
WATCHDOG_FAILURES_BEFORE_RESTART = 40
SHUTDOWN_TIMEOUT = 10.0

# Identifies this process to clients. A page holding input the previous
# process acknowledged but had not yet handed to iTerm2 cannot know whether
# it arrived, and must not send it twice; seeing a new instance is how it
# learns to ask the user instead.
SERVER_INSTANCE = uuid.uuid4().hex[:12]

# Input streams for pages that have gone quiet are dropped after this long,
# keeping only the last sequence number each delivered (see InputStream).
INPUT_STREAM_IDLE_SECONDS = 1800.0
MAX_INPUT_PAGES = 500

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

def _origin_allowed(origin, environ) -> bool:
    # Credentialed CORS (the auth cookie rides along) must not echo arbitrary
    # origins; see is_trusted_origin. Engine.IO answers a disallowed origin
    # with 400 and skips the check when there is no Origin header at all.
    return is_trusted_origin(origin, environ.get("HTTP_HOST"))


# aiohttp negotiates permessage-deflate on every WebSocket by default and
# python-engineio builds its WebSocketResponse without exposing the flag.
# REMOTE_ITERM_WS_COMPRESSION=0 turns it off: the switch for testing whether
# a phone's WebSocket errors come from the compression layer rather than the
# network, at the cost of ~4x the bytes per live frame.
WEBSOCKET_COMPRESSION = os.environ.get("REMOTE_ITERM_WS_COMPRESSION", "1") != "0"
if not WEBSOCKET_COMPRESSION:
    engineio_aiohttp.WebSocketResponse = functools.partial(
        web.WebSocketResponse, compress=False)


class _ReportingWebSocket(engineio_aiohttp.WebSocket):
    """python-engineio's aiohttp WebSocket wrapper, logging how each socket
    ended. Engine.IO reduces every ending to 'transport close'; aiohttp
    knows the close code (1000 a clean close by the peer, 1006 the TCP
    connection lost without one) and the exception a reset raised, which
    is the difference between a page letting go and a network path being
    torn down under it.
    """

    async def wait(self):
        try:
            return await super().wait()
        except Exception as err:
            sock = self._sock
            request = getattr(self, "environ", {}).get("aiohttp.request")
            log(f"websocket from {getattr(request, 'remote', '?')} ended: "
                f"close_code={getattr(sock, 'close_code', None)} "
                f"exception={(sock.exception() if sock is not None else None)!r} "
                f"raised={err!r}")
            raise


# The driver table holds the class object, not the module attribute.
engineio_aiohttp._async["websocket"] = _ReportingWebSocket

sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins=_origin_allowed)

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

# Most recent live frame per pane, least recently used first; see
# CONTENT_CACHE_SIZE.
content_cache: "OrderedDict[str, dict]" = OrderedDict()

# Clients that introduced themselves with `hello` — browser pages, as
# opposed to programmatic clients — and the page each connection belongs to.
browser_clients: set[str] = set()
# The connection each page most recently introduced itself on. Delivery
# reports for a page go there, not to whichever connection last carried its
# input: a late packet on a half-dead old connection must not divert them.
sid_by_page: dict[str, str] = {}

# Whether iTerm2 is answering, as the watchdog last saw it. `stalledSince` is
# a wall-clock epoch in milliseconds, so a client can count the stall up
# itself without being sent a tick every second.
iterm_status: dict = {"ok": True, "stalledSince": None, "latencyMs": None}


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
# The same state as built, handed to a newly connected client at once rather
# than after the dozen RPCs a fresh build costs — which during an iTerm2
# stall is as long as the stall. A fresh build follows it regardless.
_last_state: list | None = None

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


def _state_session_ids(state: list) -> set[str]:
    return {session["id"]
            for window in state for tab in window["tabs"]
            for session in tab["sessions"]}


def _forget_closed_panes(live_ids: set[str]) -> None:
    """Drop per-pane caches for panes that no longer exist. Without this they
    grow with every pane ever opened for as long as the server runs."""
    for cache in (palette_cache, content_cache, _read_failure_logged):
        for session_id in [s for s in cache if s not in live_ids]:
            del cache[session_id]


async def push_state(new_client: str | None = None) -> None:
    """Broadcast window/tab state, skipping the emit when nothing changed —
    except to ``new_client``, which gets it regardless."""
    global _last_pushed_state, _last_state
    if not clients:
        return
    state = await build_state()
    serialized = json.dumps(state, sort_keys=True)
    if serialized == _last_pushed_state:
        if new_client is not None:
            queue_client_event(new_client, "state", "state", state)
        return
    _last_pushed_state = serialized
    _last_state = state
    _forget_closed_panes(_state_session_ids(state))
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
        await asyncio.sleep(SYNC_INTERVAL_BROWSER if browser_clients
                            else SYNC_INTERVAL_BACKGROUND)
        if clients:
            await push_state()


async def health_loop() -> None:
    """Periodic bounded-queue and peak-RSS telemetry for leak diagnosis.

    `lines_read` is how many terminal lines live streams fetched from iTerm2
    in the last minute — the load the phone puts on iTerm2's main thread.
    """
    lines_before = stream_lines_read
    while True:
        await asyncio.sleep(60)
        try:
            await evict_idle_input_streams()
        except Exception as err:
            log(f"input stream eviction failed: {err!r}")
        queue_depths = [
            depth for sid in clients
            if (depth := _socket_queue_depth(sid)) is not None
        ]
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        max_rss_mb = max_rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        lines_read = stream_lines_read - lines_before
        lines_before = stream_lines_read
        iterm = ("ok" if iterm_status["ok"]
                 else f"stalled {int(time.time() - iterm_status['stalledSince'] / 1000)}s")
        log(
            "health: "
            f"max_rss={max_rss_mb:.1f}MB clients={len(clients)} "
            f"browsers={len(browser_clients)} "
            f"streams={len(stream_tasks)} retiring={len(_retiring_streams)} "
            f"pending={sum(map(len, pending_events.values()))} "
            f"engineio_queue_max={max(queue_depths, default=0)} "
            f"lines_read={lines_read} cached={len(content_cache)} "
            f"inputs={len(input_streams)} iterm={iterm}")


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


# Only web addresses become links: the phone can open those, whereas the
# file:// links `ls --hyperlink` emits, or app-specific schemes, point at the
# Mac and would be dead taps on a phone.
_LINK_SCHEMES = ("http://", "https://")


def _cell_link(style) -> str | None:
    """The OSC 8 hyperlink on a cell, if it is one a phone can open."""
    url = style.url
    if url is None:
        return None
    target = url.url
    return target if target.lower().startswith(_LINK_SCHEMES) else None


def _line_runs(line, pal, cursor_x: int | None = None) -> list:
    """Group a line into styled runs, optionally marking its cursor cell.

    A run also carries ``u`` when its cells are hyperlinked (OSC 8), so
    the link boundaries survive the run-length grouping.
    """
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
        key = (fg, bg, bool(style.bold), bool(style.faint), _cell_link(style))
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
    fg, bg, bold, dim, link = key
    run = {"t": text}
    if fg:
        run["f"] = fg
    if bg:
        run["g"] = bg
    if bold:
        run["b"] = True
    if dim:
        run["d"] = True
    if link:
        run["u"] = link
    return run


# Anything that spells out a web address is linked too, since most tools
# print plain URLs rather than OSC 8 hyperlinks. The pattern is xterm.js's
# web-links one: a scheme, a body of anything but whitespace and obvious
# delimiters, ending on a character that is not trailing punctuation — so
# "(see https://x.y/z)." links exactly https://x.y/z.
_URL_RE = re.compile(
    r"""https?://[^\s"'!*(){}|\\^<>`]*[^\s"':,.!?{}|\\^~\[\]`()<>]""",
    re.IGNORECASE)


def _link_ranges(text: str) -> list:
    """(start, end, url) for every web address in ``text``, in order."""
    return [(m.start(), m.end(), m.group(0)) for m in _URL_RE.finditer(text)]


def _apply_links(runs: list, links: list) -> list:
    """Split a line's runs at link boundaries and tag the pieces with ``u``.

    ``links`` are (start, end, url) character ranges over the line's text,
    which the runs' ``t`` values spell out in order — except the zero-width
    cursor run, which occupies no characters and is passed through where it
    sits. A run already hyperlinked by OSC 8 keeps the explicit link.
    """
    if not links:
        return runs
    out: list = []
    pos = 0
    for run in runs:
        text = run["t"]
        end = pos + len(text)
        if run.get("c") or "u" in run or not text:
            out.append(run)
            pos = end
            continue
        cut = pos
        for start, stop, url in links:
            lo, hi = max(start, pos), min(stop, end)
            if lo >= hi:
                continue
            if lo > cut:
                out.append({**run, "t": text[cut - pos:lo - pos]})
            out.append({**run, "t": text[lo - pos:hi - pos], "u": url})
            cut = hi
        if cut < end:
            out.append({**run, "t": text[cut - pos:]})
        pos = end
    return out


def _autolink(rendered: list, lines) -> None:
    """Link the web addresses in a page of rendered lines, in place.

    iTerm2 wraps a long line onto the following rows and reports each row
    with ``hard_eol`` False, so the rows of one logical line are joined
    before matching and an address that wrapped mid-way is one link on
    every row it spans. The page boundary cuts a logical line off, which
    can only ever shorten a link, never invent one.
    """
    texts = [line.string.replace("\x00", " ") for line in lines]
    start = 0
    while start < len(lines):
        stop = start
        while stop < len(lines) - 1 and not lines[stop].hard_eol:
            stop += 1
        matches = _link_ranges("".join(texts[start:stop + 1]))
        offset = 0
        for i in range(start, stop + 1):
            length = len(texts[i])
            local = [
                (max(s, offset) - offset, min(e, offset + length) - offset, url)
                for s, e, url in matches
                if s < offset + length and e > offset
            ]
            if local:
                rendered[i] = _apply_links(rendered[i], local)
            offset += length
        start = stop + 1


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


def render_frame(frame: TailFrame, pal: dict, *, is_latest: bool) -> dict:
    """The styled, linked content a client draws, from lines read from iTerm2."""
    cursor_x, cursor_y = frame.cursor
    rendered = [
        _line_runs(line, pal,
                   cursor_x if frame.first + i == cursor_y else None)
        for i, line in enumerate(frame.lines)
    ]
    _autolink(rendered, frame.lines)
    return {
        "lines": rendered, "fg": pal["fg"], "bg": pal["bg"],
        "firstLine": frame.first, "availableFirstLine": frame.available_first,
        "terminalEnd": frame.terminal_end,
        "isLatest": is_latest,
    }


async def read_content(
        session_id: str | None,
        line_count: int = LIVE_CONTENT_LINES,
        before_line: int | None = None,
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
        first, count, available_first, terminal_end = _content_line_range(
            info, line_count, before_line)
        if count <= 0:
            return {
                "lines": [], "fg": pal["fg"], "bg": pal["bg"],
                "firstLine": first, "availableFirstLine": available_first,
                "terminalEnd": terminal_end,
                "isLatest": before_line is None,
            }
        # Not session.async_get_contents: its per-cell text shifts by one
        # after every emoji outside the Basic Multilingual Plane (see
        # terminal_lines), which misaligns colours and drops the line's end.
        # The cursor comes back in the same response.
        lines, cursor = await async_get_lines_and_cursor(session, first, count)
        content = render_frame(
            TailFrame(first=first, lines=lines, cursor=cursor,
                      available_first=available_first,
                      terminal_end=terminal_end),
            pal, is_latest=before_line is None)
        if before_line is None and line_count >= LIVE_CONTENT_LINES:
            remember_content(session_id, content)
        return content
    except Exception:
        return None


def remember_content(session_id: str, content: dict) -> None:
    """Keep a pane's latest live frame for the next client that asks for it."""
    if not content.get("lines"):
        return
    content_cache[session_id] = content
    content_cache.move_to_end(session_id)
    while len(content_cache) > CONTENT_CACHE_SIZE:
        content_cache.popitem(last=False)


def cached_content(session_id: str) -> dict | None:
    """The freshest frame this server holds for a pane, without asking iTerm2."""
    return last_content.get(session_id) or content_cache.get(session_id)


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
    """The pane a request names, or iTerm's focused pane if it names none.

    A named pane that no longer exists resolves to nothing. It used to fall
    back to the focused pane too, which typed a stale request's keystrokes
    into whatever the Mac happened to have in front.
    """
    if itermapp is not None and session_id and session_id != "undefined":
        return itermapp.get_session_by_id(session_id)
    return current_session()


# --- Live streaming of watched sessions ----------------------------------------
#
# The client tells us which sessions it is viewing (the `watch` event); we run
# one screen-streamer task per distinct watched session and push its content on
# every change. This is decoupled from iTerm's own focus, so the phone can watch
# any pane — including a background split pane — and still get live updates.

# Lines fetched from iTerm2 by live streams since start, for the health log:
# the measure of how much work the phone is asking of iTerm2.
stream_lines_read = 0


_read_failure_logged: dict[str, float] = {}


async def _read_live_frame(session_id: str, session, tail: LiveTail) -> dict | None:
    global stream_lines_read
    try:
        pal = await get_palette(session)
        before = tail.lines_read
        frame = await tail.read(session)
        stream_lines_read += tail.lines_read - before
        return render_frame(frame, pal, is_latest=True)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        now = time.monotonic()
        if now - _read_failure_logged.get(session_id, -60.0) >= 60.0:
            _read_failure_logged[session_id] = now
            log(f"live read failed ({session_id}): {err!r}")
        return None


def _publish_frame(session_id: str, content: dict) -> None:
    last_content[session_id] = content
    remember_content(session_id, content)
    queue_content_for_watchers(session_id, content)


async def stream_session(session_id: str) -> None:
    session = itermapp.get_session_by_id(session_id) if itermapp else None
    if session is None:
        return
    tail = LiveTail(LIVE_CONTENT_LINES)

    try:
        content = await _read_live_frame(session_id, session, tail)
        if content is not None and content["lines"] and clients:
            _publish_frame(session_id, content)

        # The notification itself is enough: waiting briefly before fetching the
        # current screen coalesces bursts of changes and avoids allocating a
        # stale screen snapshot for every byte of fast terminal output.
        pause = STREAM_MIN_INTERVAL
        async with session.get_screen_streamer(want_contents=False) as streamer:
            while True:
                await streamer.async_get()
                await asyncio.sleep(pause)
                if not clients:
                    continue
                started = time.monotonic()
                content = await _read_live_frame(session_id, session, tail)
                pause = min(STREAM_MAX_INTERVAL,
                            max(STREAM_MIN_INTERVAL,
                                2 * (time.monotonic() - started)))
                if content is not None and content != last_content.get(session_id):
                    _publish_frame(session_id, content)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        log(f"stream error ({session_id}): {err}")
    finally:
        # A stream being torn down can still be finishing (its unsubscribe
        # waits on iTerm2) after a new stream for the same pane has started;
        # the frame it would drop is then the new stream's.
        if stream_tasks.get(session_id) in (None, asyncio.current_task()):
            last_content.pop(session_id, None)


# Cancelled streams still finishing their teardown. Their unsubscribe is an
# RPC like any other, so during an iTerm2 stall it takes as long as the stall;
# they finish in the background rather than holding up the next reconcile.
_retiring_streams: set[asyncio.Task] = set()


def _retire(task: asyncio.Task) -> None:
    task.cancel()
    _retiring_streams.add(task)
    task.add_done_callback(_retiring_streams.discard)


async def apply_watches() -> None:
    """Reconcile running stream tasks with the union of watched sessions.

    Never waits for a cancelled stream to finish: that wait used to happen
    under the lock, so while iTerm2 was stalled one client switching panes
    held up every other client's `watch` until iTerm2 answered.
    """
    async with stream_reconcile_lock:
        union: set[str] = set()
        for ids in watched_by_sid.values():
            union |= ids

        for session_id, task in list(stream_tasks.items()):
            if session_id not in union:
                stream_tasks.pop(session_id, None)
                _retire(task)
            elif task.done():
                stream_tasks.pop(session_id, None)  # restart below if wanted

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
            _retire(task)


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
        stalled_since_ms = int(time.time() * 1000)
        probe = asyncio.ensure_future(_probe_iterm())
        try:
            try:
                await asyncio.wait_for(asyncio.shield(probe), timeout=STALL_AFTER)
            except asyncio.TimeoutError:
                if probe.done():
                    raise  # the probe itself failed, promptly: not a stall
                _set_iterm_status(ok=False, stalled_since_ms=stalled_since_ms)
                await asyncio.wait_for(probe, timeout=WATCHDOG_TIMEOUT - STALL_AFTER)
        except asyncio.CancelledError:
            probe.cancel()
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
            _set_iterm_status(
                ok=True, latency_ms=int((time.monotonic() - started) * 1000))
        if failures:
            delay = WATCHDOG_RETRY_INTERVAL
        elif browser_clients:
            delay = WATCHDOG_INTERVAL_BROWSER
        else:
            delay = WATCHDOG_INTERVAL
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def _probe_iterm() -> None:
    """The cheapest round trip iTerm2 offers: one app variable. The probe
    used to be a full session listing, which iTerm2 renders as ~22 KB of
    text for its Script Console every time (see live_tail)."""
    await iterm2.rpc.async_variable(connection, gets=["pid"])


def _set_iterm_status(*, ok: bool, stalled_since_ms: int | None = None,
                      latency_ms: int | None = None) -> None:
    """Record whether iTerm2 is answering, telling clients when that changes."""
    was_ok = iterm_status["ok"]
    if ok:
        iterm_status.update(ok=True, stalledSince=None, latencyMs=latency_ms)
        if not was_ok:
            log(f"iTerm2 answering again ({latency_ms}ms)")
    elif was_ok:
        iterm_status.update(ok=False, stalledSince=stalled_since_ms,
                            latencyMs=None)
        log(f"iTerm2 not answering for {STALL_AFTER:.0f}s — telling clients")
    else:
        return
    if ok != was_ok:
        for sid in list(clients):
            queue_client_event(sid, "itermStatus", "itermStatus",
                               iterm_status_payload())


def iterm_status_payload() -> dict:
    """iterm_status plus this server's clock, so a client can count a stall
    up in its own clock whatever the difference between the two."""
    return {**iterm_status, "now": int(time.time() * 1000)}


# --- Socket.IO handlers ---------------------------------------------------------

async def seed_client(sid: str) -> None:
    """Give a freshly connected client its window/tab state and a first
    screenful, so it has something to draw before its own `watch` arrives.

    Runs as a task rather than inline in `connect` — see the note there.
    """
    try:
        await sio.emit("serverInfo", {"instance": SERVER_INSTANCE}, to=sid)
        await sio.emit("itermStatus", iterm_status_payload(), to=sid)
        await sio.emit("screenSize", screen_size, to=sid)
        # The last state pushed, at once, then a fresh one: during an iTerm2
        # stall the fresh build takes as long as the stall.
        if _last_state is not None:
            await sio.emit("state", _last_state, to=sid)
        # Whatever iTerm has focused, if a frame of it is at hand. No read is
        # made for it here: the client's own `watch` follows at once and
        # streams the pane it actually picks.
        session_id = current_active_session_id()
        content = cached_content(session_id) if session_id else None
        if content is not None:
            await sio.emit(
                "content", {"sessionId": session_id, **content, "cached": True},
                to=sid)
        # Through the outbox like every other state, so a newer state pushed
        # while this one was being built is never overtaken by it.
        await push_state(new_client=sid)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        log(f"seeding client {sid} failed: {err!r}")
    finally:
        seed_tasks.pop(sid, None)


@sio.event
async def connect(sid, environ, auth=None):
    # Either the key in the Socket.IO auth payload (from localStorage or the
    # QR URL fragment) or the HttpOnly cookie issued by /auth will do; the
    # cookie is what keeps a phone working after Safari has purged the page's
    # localStorage.
    supplied_key = auth.get("key") if isinstance(auth, dict) else None
    cookie_key = key_from_cookie_header(environ.get("HTTP_COOKIE"))
    by_key = is_valid_key(shared_key, supplied_key)
    if not (by_key or is_valid_key(shared_key, cookie_key)):
        log(f"Rejected unauthenticated client: {sid}")
        raise socketio.exceptions.ConnectionRefusedError("invalid access key")
    clients.add(sid)
    start_client_delivery(sid)
    agent = str(environ.get("HTTP_USER_AGENT", "?"))[:90]
    # The peer address tells the LAN path (192.168.x) from the tailnet one
    # (100.x) when a phone keeps dropping — the two fail differently. Read
    # from the aiohttp request: python-engineio's aiohttp driver fills
    # REMOTE_ADDR with a constant 127.0.0.1.
    request = environ.get("aiohttp.request")
    peer = getattr(request, "remote", None) or "?"
    log(f"Client connected: {sid} by {'key' if by_key else 'cookie'} "
        f"from {peer} ({agent})")
    # Seeding is deferred to a task because python-socketio only acknowledges
    # the connection once this handler returns, and seeding needs a dozen
    # iTerm2 RPCs. Inline, that made the Socket.IO handshake as slow as
    # iTerm2's slowest moment: the client gives up after 10s and reconnects,
    # so on a busy Mac it sat in an endless RECONNECTING loop, each attempt
    # dying just before the state it was waiting for arrived. The client
    # already accepts `state` and `content` whenever they turn up.
    seed_tasks[sid] = asyncio.create_task(seed_client(sid))


def _client_transport(sid: str) -> str:
    """'websocket' or 'polling' for a live Socket.IO client, else '?'."""
    eio_sid = sio.manager.eio_sid_from_sid(sid, "/")
    socket = sio.eio.sockets.get(eio_sid) if eio_sid else None
    if socket is None:
        return "?"
    return "websocket" if getattr(socket, "upgraded", False) else "polling"


@sio.event
async def disconnect(sid, reason=None):
    # Read before teardown: Engine.IO drops its socket once this returns.
    transport = _client_transport(sid)
    clients.discard(sid)
    browser_clients.discard(sid)
    for page in [p for p, s in sid_by_page.items() if s == sid]:
        del sid_by_page[page]
    watched_by_sid.pop(sid, None)
    seed = seed_tasks.pop(sid, None)
    if seed is not None:
        seed.cancel()
    await stop_client_delivery(sid)
    # The reason is the whole diagnosis when a phone keeps reconnecting:
    # 'ping timeout' is a stalled path, 'transport close' is the network or
    # the OS closing the socket under us, 'client disconnect' is the page
    # itself letting go.
    log(f"Client disconnected: {sid} ({reason or 'no reason'}, {transport})")
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

    # Immediately hand this client the latest frame this server holds for each
    # pane. A shared per-session stream emits its first frame only when its task
    # is created (see stream_session), so a client that starts watching an
    # already-streamed pane — e.g. a phone reconnecting after sleep while its
    # previous connection's stream is still alive — would otherwise sit on
    # "WAITING FOR OUTPUT" until that pane next changes. A pane not streamed
    # right now may still have a frame cached from earlier, which is drawn at
    # once while the stream apply_watches() starts below reads a fresh one.
    # Nothing is read from iTerm2 here: with no frame at hand, the stream's
    # own first read is what the client gets.
    for session_id in sessions:
        content = cached_content(session_id)
        if content is not None:
            queue_client_event(
                sid, f"content:{session_id}", "content",
                {"sessionId": session_id, **content, "cached": True})

    await apply_watches()


@sio.on("hello")
async def on_hello(sid, data):
    # Client-reported context for a connection, logged for diagnosing
    # reconnect churn: whether this is a fresh page load or the same page
    # reconnecting, how the page saw its previous connection end, and what
    # it could see of the network at the time. It also marks the connection
    # as a browser page, and says which page, so input it sent on an earlier
    # connection can be reported delivered on this one.
    if not isinstance(data, dict):
        return
    log(f"hello {sid}: {json.dumps(data, sort_keys=True)[:500]}")
    if sid not in clients:
        return
    browser_clients.add(sid)
    page = data.get("page")
    if isinstance(page, str) and page:
        sid_by_page[page] = sid
        stream = input_streams.get(page)
        if stream is not None:
            stream.sid = sid
            stream.report_delivered()
        elif page in delivered_by_page:
            queue_client_event(sid, f"inputDelivered:{page}", "inputDelivered",
                               {"page": page, "upto": delivered_by_page[page]})


@sio.event
async def ping(sid):
    # Returning sends the Socket.IO ack with no args -> client latency callback.
    return


@sio.on("getContent")
async def on_get_content(sid, data):
    # Always a fresh read, never a cached frame: a programmatic client
    # (aveiro) takes the first `content` it gets as the pane's current state,
    # and checks it before typing into the pane. The phone does not use this;
    # its `watch` is drawn from the cache and then streamed.
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


# --- Input: ordered, acknowledged, never sent twice ----------------------------
#
# A keystroke or command emitted into a WebSocket that has just died is lost
# without a trace: Socket.IO does not retransmit, and a phone on a tailnet
# loses its socket every minute or so (docs/ARCHITECTURE.md, "Diagnosing
# reconnects"). So a page numbers everything it types and keeps it until
# this server says iTerm2 has it, resending on each reconnect. Here each
# page's input runs through one queue, in order, and a number already
# accepted is acknowledged again but never delivered twice.
#
# Two acknowledgements, because they fail differently: the Socket.IO ack
# ("received") comes straight back, so a page that does not get one knows its
# socket is dead; `inputDelivered` follows once iTerm2 has taken the text,
# which during an iTerm2 stall is when the stall ends. Delivery is never
# abandoned for taking long: the request is already on its way to iTerm2,
# and giving up on the answer would not take it back.
#
# Messages without a page and sequence number (aveiro's) take the old,
# unacknowledged path.

# The last sequence number delivered for each page, kept after the page's
# stream is dropped so a page that comes back is still answered correctly.
delivered_by_page: "OrderedDict[str, int]" = OrderedDict()
input_streams: dict[str, "InputStream"] = {}
# Failures recalled per page, reported with every delivery update so one
# that happened while the page was disconnected still reaches it.
MAX_RECALLED_FAILURES = 20


class InputStream:
    """One page's input, delivered to iTerm2 strictly in the order typed."""

    def __init__(self, page: str):
        self.page = page
        self.received = delivered_by_page.get(page, 0)
        self.delivered = self.received
        self.failures: "OrderedDict[int, str]" = OrderedDict()
        self.sid: str | None = None
        self.last_active = time.monotonic()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._run())

    def accept(self, seq: int, deliver) -> None:
        """Queue ``deliver`` as input number ``seq``, unless it already was."""
        self.last_active = time.monotonic()
        if seq <= self.received:
            return
        if self.received and seq != self.received + 1:
            log(f"input {self.page}: #{seq} arrived after #{self.received}; "
                f"#{self.received + 1}..#{seq - 1} never did")
        self.received = seq
        self._queue.put_nowait((seq, deliver))

    @property
    def idle(self) -> bool:
        return (self.delivered == self.received
                and time.monotonic() - self.last_active > INPUT_STREAM_IDLE_SECONDS)

    def failure_report(self) -> list:
        return [{"seq": seq, "reason": reason}
                for seq, reason in self.failures.items()]

    def report_delivered(self) -> None:
        if self.sid is None:
            return
        queue_client_event(
            self.sid, f"inputDelivered:{self.page}", "inputDelivered",
            {"page": self.page, "upto": self.delivered,
             "failed": self.failure_report()})

    async def stop(self) -> None:
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            seq, deliver = await self._queue.get()
            try:
                failure = await deliver()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                failure = f"iTerm2 refused it ({err})"
            if failure:
                log(f"input {self.page}#{seq} not delivered: {failure}")
                self.failures[seq] = failure
                while len(self.failures) > MAX_RECALLED_FAILURES:
                    self.failures.popitem(last=False)
            self.delivered = seq
            self.last_active = time.monotonic()
            delivered_by_page[self.page] = seq
            delivered_by_page.move_to_end(self.page)
            while len(delivered_by_page) > MAX_INPUT_PAGES:
                delivered_by_page.popitem(last=False)
            self.report_delivered()


def _named_session(session_id) -> iterm2.Session | None:
    """The pane a sequenced input names — and only that pane."""
    if itermapp is None or not isinstance(session_id, str) or not session_id:
        return None
    return itermapp.get_session_by_id(session_id)


def _accept_input(sid: str, data: dict, deliver) -> dict | None:
    """Queue sequenced input and return its acknowledgement, or None for a
    message without a page and sequence number (the unacknowledged path)."""
    page = data.get("page")
    seq = data.get("seq")
    if not (isinstance(page, str) and page
            and isinstance(seq, int) and not isinstance(seq, bool)):
        return None
    stream = input_streams.get(page)
    if stream is None:
        stream = input_streams[page] = InputStream(page)
    current = sid_by_page.get(page)
    stream.sid = current if current in clients else sid
    stream.accept(seq, deliver)
    # `failed` travels with `delivered`: `delivered` counts failures too, so
    # a page told only the count would retire a failed input as delivered.
    return {"ok": True, "instance": SERVER_INSTANCE,
            "delivered": stream.delivered, "failed": stream.failure_report()}


async def evict_idle_input_streams() -> None:
    for page, stream in list(input_streams.items()):
        if stream.idle:
            del input_streams[page]
            await stream.stop()


@sio.on("execute")
async def on_execute(sid, data):
    command = data.get("command", "")
    session_id = data.get("sessionId")

    async def deliver():
        session = _named_session(session_id)
        if session is None:
            return "that pane has closed"
        # CR (0x0D), not LF: a real Return submits in shells (ICRNL maps CR->NL)
        # and in raw-mode TUIs like Claude Code, where LF only inserts a newline.
        await session.async_send_text(command + "\r")

    ack = _accept_input(sid, data, deliver)
    if ack is not None:
        return ack
    session = resolve_session(session_id)
    if session is not None:
        await session.async_send_text(command + "\r")


@sio.on("broadcast")
async def on_broadcast(sid, data):
    command = data.get("command", "")
    session_ids = data.get("sessionIds") or []

    async def deliver():
        sessions = [s for s in map(_named_session, session_ids) if s is not None]
        results = await asyncio.gather(
            *(s.async_send_text(command + "\r") for s in sessions),
            return_exceptions=True)
        missed = (len(session_ids) - len(sessions)
                  + sum(isinstance(r, BaseException) for r in results))
        if missed == len(session_ids):
            return "none of those panes took it"
        if missed:
            return f"{missed} of {len(session_ids)} panes did not take it"

    ack = _accept_input(sid, data, deliver)
    if ack is not None:
        _state_dirty.set()
        return ack
    sessions = [itermapp.get_session_by_id(s) for s in session_ids] if itermapp else []
    await asyncio.gather(*(
        s.async_send_text(command + "\r") for s in sessions if s is not None))
    await push_state()


@sio.on("sendKeys")
async def on_send_keys(sid, data):
    keys = data.get("keys", "")
    session_id = data.get("sessionId")

    async def deliver():
        session = _named_session(session_id)
        if session is None:
            return "that pane has closed"
        await session.async_send_text(keys)

    ack = _accept_input(sid, data, deliver)
    if ack is not None:
        return ack
    session = resolve_session(session_id)
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
    #
    # The acknowledgement names the new pane, so the phone can switch to it the
    # way iTerm focuses a new split on the Mac.
    session = resolve_session((data or {}).get("sessionId"))
    if session is None:
        return {"error": "that pane has closed"}
    vertical = bool((data or {}).get("vertical", True))
    try:
        created = await session.async_split_pane(vertical=vertical)
    except Exception as err:
        log(f"split pane failed: {err}")
        await push_state()
        return {"error": f"iTerm2 could not split it ({err})"}
    await push_state()
    return {"sessionId": created.session_id}


@sio.on("closePane")
async def on_close_pane(sid, data):
    # Deliberately not resolve_session: that falls back to whatever iTerm has
    # focused when the id is unknown, which for a destructive call means a
    # stale id closes the wrong pane. An id we cannot find is a pane that has
    # already gone, so there is nothing to do.
    session_id = (data or {}).get("sessionId")
    session = itermapp.get_session_by_id(session_id) if (itermapp and session_id) else None
    if session is None:
        return
    try:
        # force=True because the phone has already asked. Without it iTerm
        # runs closeSessionWithConfirmation:, which puts a modal alert on the
        # Mac for any pane with a running job — nobody is at the Mac to answer
        # it, and it blocks iTerm's main thread, and with it the whole API.
        await session.async_close(force=True)
    except Exception as err:
        log(f"close pane failed: {err}")
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
    # A specific pane (a notification deep link lands on one): one call selects
    # the pane, its tab, and orders its window front. A pane that has since
    # closed falls through to the window/tab the client asked for.
    session_id = data.get("sessionId")
    session = (itermapp.get_session_by_id(str(session_id))
               if session_id and session_id != "undefined" else None)
    if session is not None:
        log(f"focus {sid}: pane {session_id}")
        await session.async_activate(select_tab=True, order_window_front=True)
        await itermapp.async_activate()
        await push_state()
        return

    window_id = data.get("windowId")
    tab_index = data.get("tabIndex") or 0
    if session_id:
        log(f"focus {sid}: pane {session_id} not found; "
            f"falling back to window {window_id} tab {tab_index}")
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


# --- Cookie credential ---------------------------------------------------------
#
# POST /auth {"key": ...} — or a request already carrying a valid cookie —
# answers 204 and (re)issues the HttpOnly auth cookie with a fresh lifetime.
# The client calls it on every successful connection, so the cookie's expiry
# slides forward with use and never lapses on a phone that is used at all.

def _cors_headers(request: web.Request) -> dict[str, str]:
    origin = request.headers.get("Origin")
    if not is_trusted_origin(origin, request.headers.get("Host")):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


async def auth_preflight(request: web.Request) -> web.Response:
    headers = _cors_headers(request)
    if not headers:
        return web.Response(status=403, text="origin not allowed")
    headers.update({
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    })
    return web.Response(status=204, headers=headers)


async def auth_issue_cookie(request: web.Request) -> web.Response:
    headers = _cors_headers(request)
    if request.headers.get("Origin") and not headers:
        return web.Response(status=403, text="origin not allowed")
    try:
        body = await request.json()
    except Exception:
        body = None
    supplied_key = body.get("key") if isinstance(body, dict) else None
    cookie_key = request.cookies.get(COOKIE_NAME)
    if not (is_valid_key(shared_key, supplied_key)
            or is_valid_key(shared_key, cookie_key)):
        return web.Response(status=401, headers=headers, text="invalid access key")
    response = web.Response(status=204, headers=headers)
    response.set_cookie(
        COOKIE_NAME, shared_key, max_age=COOKIE_MAX_AGE, path="/",
        httponly=True, samesite="Lax")
    return response


def create_app() -> web.Application:
    """The aiohttp application: the Socket.IO endpoint plus /auth.

    Built per run rather than at import because an Application binds to the
    event loop that first serves it, which lets tests spin one up per loop.
    """
    app = web.Application()
    sio.attach(app)
    app.router.add_route("OPTIONS", "/auth", auth_preflight)
    app.router.add_post("/auth", auth_issue_cookie)
    return app


# --- Entry point ---------------------------------------------------------------

async def main() -> None:
    global connection, itermapp, screen_size, shared_key
    screen_size = read_screen_size()
    log("Screen size:", screen_size)

    shared_key = load_or_create_key()

    use_osascript_for_cookie()
    connection = await iterm2.Connection.async_create()
    itermapp = await iterm2.async_get_app(connection)

    runner = web.AppRunner(create_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log(f"Server running on http://0.0.0.0:{PORT} "
        f"(websocket compression {'on' if WEBSOCKET_COMPRESSION else 'off'})")

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
        for stream in list(input_streams.values()):
            await stream.stop()
        await stop_all_streams()
        await asyncio.gather(*_retiring_streams, return_exceptions=True)
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
