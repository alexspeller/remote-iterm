#!/usr/bin/env python3
"""Remote iTerm server — drives iTerm2 over its native Python API.

Replaces the previous AppleScript/osascript implementation. A single asyncio
loop runs both the Socket.IO server (for the web/phone client) and the iTerm2
connection. Terminal output and window/tab state are PUSHED from iTerm2's
notification streams instead of being polled, so no per-call processes are
spawned. The Socket.IO event contract is identical to the old Node server, so
the client is unchanged.
"""
import asyncio
import json
import math
import signal

import iterm2
import socketio
from aiohttp import web

try:
    from AppKit import NSScreen
except Exception:  # pragma: no cover - pyobjc should always be present
    NSScreen = None

PORT = 7291

# Lines of recent output (including scrollback) sent per session. The API makes
# this cheap, so we send a comfortable chunk and let the client scroll.
CONTENT_LINES = 120

sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
app = web.Application()
sio.attach(app)

clients: set[str] = set()

connection: iterm2.Connection | None = None
itermapp: iterm2.App | None = None

screen_size = {"width": 1470, "height": 956}

# Sessions each connected client is actively viewing (its main pane + optional
# split pane). We live-stream exactly the union of these, independent of which
# session iTerm has focused on the Mac — so the phone can watch any pane it
# likes (including a background split pane) and still get live updates.
watched_by_sid: dict[str, set[str]] = {}
stream_tasks: dict[str, asyncio.Task] = {}
last_content: dict[str, dict] = {}

# Per-session color palette (default fg/bg + ANSI 0-15), read once from the
# session's iTerm2 profile so standard colors match the user's actual theme.
palette_cache: dict[str, dict] = {}


# --- Screen geometry (NSScreen, no osascript) ----------------------------------

def read_screen_size() -> dict:
    if NSScreen is None:
        return {"width": 1470, "height": 956}
    try:
        frame = NSScreen.screens()[0].frame()
        return {"width": int(frame.size.width), "height": int(frame.size.height)}
    except Exception:
        return {"width": 1470, "height": 956}


# --- Pane (split) geometry -----------------------------------------------------
#
# A tab's panes live in a tree of Splitters (verified against the live API):
#   * Splitter.vertical=True  -> children are arranged left→right (divide x)
#   * Splitter.vertical=False -> children are arranged top→bottom (divide y)
#   * child order is visual order; Session.frame is top-left origin but LOCAL to
#     its parent splitter, so we reconstruct tab-wide rects by walking the tree
#     and weighting each child by its natural (frame) size along the split axis.

def _natural_size(node) -> tuple:
    if isinstance(node, iterm2.Session):
        f = node.frame
        if f is None:
            return (1.0, 1.0)
        return (float(f.size.width), float(f.size.height))
    sizes = [_natural_size(c) for c in node.children] or [(1.0, 1.0)]
    if node.vertical:
        return (sum(s[0] for s in sizes), max(s[1] for s in sizes))
    return (max(s[0] for s in sizes), sum(s[1] for s in sizes))


def _assign_rects(node, x: float, y: float, w: float, h: float, out: dict) -> None:
    if isinstance(node, iterm2.Session):
        out[node.session_id] = (x, y, w, h)
        return
    sizes = [_natural_size(c) for c in node.children]
    if node.vertical:
        total = sum(s[0] for s in sizes) or 1.0
        cx = x
        for c, s in zip(node.children, sizes):
            cw = w * s[0] / total
            _assign_rects(c, cx, y, cw, h, out)
            cx += cw
    else:
        total = sum(s[1] for s in sizes) or 1.0
        cy = y
        for c, s in zip(node.children, sizes):
            ch = h * s[1] / total
            _assign_rects(c, x, cy, w, ch, out)
            cy += ch


def pane_layout(tab) -> tuple:
    """Returns ({session_id: (x, y, w, h) in 0..1, top-left}, aspect, maximized).

    When a pane is maximized the others are "minimized": they have no frame and
    the split tree collapses to just the maximized pane, so the true geometry is
    gone. In that case we fall back to an even grid over all panes.
    """
    try:
        if tab.minimized_sessions:
            panes = tab.all_sessions
            n = len(panes)
            cols = max(1, math.ceil(math.sqrt(n)))
            rows = max(1, math.ceil(n / cols))
            rects = {
                s.session_id: ((i % cols) / cols, (i // cols) / rows,
                               1 / cols, 1 / rows)
                for i, s in enumerate(panes)
            }
            aspect = 1.6
            for s in panes:
                f = s.frame
                if f is not None and f.size.height:
                    aspect = f.size.width / f.size.height
                    break
            return rects, aspect, True

        root = tab.root
        nat = _natural_size(root)
        aspect = (nat[0] / nat[1]) if nat[1] else 1.0
        rects: dict = {}
        _assign_rects(root, 0.0, 0.0, 1.0, 1.0, rects)
        return rects, aspect, False
    except Exception:
        return {}, 1.0, False


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
    await sio.emit("state", state)


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


def _line_runs(line, pal) -> list:
    """Group a LineContents into run-length colored runs: {t, f?, g?, b?}."""
    runs: list = []
    cur = None
    buf = ""
    x = 0
    while True:
        style = line.style_at(x)
        if style is None:
            break
        ch = line.string_at(x).replace("\x00", " ")
        fg = _resolve(style.fg_color, pal)
        bg = _resolve(style.bg_color, pal)
        if style.inverse:
            fg, bg = (bg or pal["bg"]), (fg or pal["fg"])
        key = (fg, bg, bool(style.bold))
        if key != cur:
            if buf:
                runs.append(_make_run(cur, buf))
            cur, buf = key, ch
        else:
            buf += ch
        x += 1
    if buf:
        runs.append(_make_run(cur, buf))
    # Trim trailing whitespace runs that carry no background.
    while runs and "g" not in runs[-1] and not runs[-1]["t"].strip():
        runs.pop()
    if runs and "g" not in runs[-1]:
        runs[-1]["t"] = runs[-1]["t"].rstrip()
    return runs


def _make_run(key, text: str) -> dict:
    fg, bg, bold = key
    run = {"t": text}
    if fg:
        run["f"] = fg
    if bg:
        run["g"] = bg
    if bold:
        run["b"] = True
    return run


async def read_content(session_id: str | None) -> dict | None:
    if itermapp is None or not session_id or session_id == "undefined":
        return None
    session = itermapp.get_session_by_id(session_id)
    if session is None:
        return None
    try:
        pal = await get_palette(session)
        async with iterm2.Transaction(connection):
            info = await session.async_get_line_info()
            total = (info.overflow + info.scrollback_buffer_height
                     + info.mutable_area_height)
            first = max(info.overflow, total - CONTENT_LINES)
            count = total - first
            if count <= 0:
                return {"lines": [], "fg": pal["fg"], "bg": pal["bg"]}
            lines = await session.async_get_contents(first, count)
        rendered = [_line_runs(line, pal) for line in lines]
        while rendered and not rendered[-1]:
            rendered.pop()
        return {"lines": rendered, "fg": pal["fg"], "bg": pal["bg"]}
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
        await sio.emit("content", {"sessionId": session_id, **content})

    try:
        async with session.get_screen_streamer() as streamer:
            while True:
                await streamer.async_get()
                if not clients:
                    continue
                content = await read_content(session_id)
                if content is not None and content != last_content.get(session_id):
                    last_content[session_id] = content
                    await sio.emit(
                        "content", {"sessionId": session_id, **content})
    except asyncio.CancelledError:
        raise
    except Exception as err:
        print(f"stream error ({session_id}): {err}")
    finally:
        last_content.pop(session_id, None)


def apply_watches() -> None:
    """Reconcile running stream tasks with the union of watched sessions."""
    union: set[str] = set()
    for ids in watched_by_sid.values():
        union |= ids

    for session_id, task in list(stream_tasks.items()):
        if session_id not in union:
            task.cancel()
            stream_tasks.pop(session_id, None)
        elif task.done():
            stream_tasks.pop(session_id, None)  # restarted below if still wanted

    if clients:
        for session_id in union:
            if session_id not in stream_tasks:
                stream_tasks[session_id] = asyncio.create_task(
                    stream_session(session_id))


def stop_all_streams() -> None:
    for task in stream_tasks.values():
        task.cancel()
    stream_tasks.clear()


# --- iTerm2 notification monitors (replace the old poll loops) -----------------

async def layout_monitor() -> None:
    async with iterm2.LayoutChangeMonitor(connection) as monitor:
        while True:
            await monitor.async_get()
            await push_state()


async def focus_monitor() -> None:
    async with iterm2.FocusMonitor(connection) as monitor:
        while True:
            await monitor.async_get_next_update()
            await push_state()


# --- Socket.IO handlers (identical contract to the old Node server) -------------

@sio.event
async def connect(sid, environ, auth=None):
    clients.add(sid)
    print(f"Client connected: {sid}")
    await sio.emit("screenSize", screen_size, to=sid)
    await sio.emit("state", await build_state(), to=sid)
    # Seed the view with a snapshot of whatever iTerm currently has focused, so
    # there's instant output before the client sends its first `watch`.
    session_id = current_active_session_id()
    if session_id:
        content = await read_content(session_id)
        if content and content["lines"]:
            await sio.emit(
                "content", {"sessionId": session_id, **content}, to=sid)


@sio.event
async def disconnect(sid, reason=None):
    clients.discard(sid)
    watched_by_sid.pop(sid, None)
    print(f"Client disconnected: {sid}")
    if not clients:
        stop_all_streams()
    else:
        apply_watches()


@sio.on("watch")
async def on_watch(sid, data):
    # The client lists the sessions it is currently viewing (main + split panes);
    # we live-stream exactly that union across all clients.
    ids = data.get("sessionIds") or []
    watched_by_sid[sid] = {s for s in ids if s and s != "undefined"}
    apply_watches()


@sio.event
async def ping(sid):
    # Returning sends the Socket.IO ack with no args -> client latency callback.
    return


@sio.on("getContent")
async def on_get_content(sid, data):
    session_id = data.get("sessionId")
    content = await read_content(session_id)
    if content and content["lines"]:
        await sio.emit(
            "content", {"sessionId": session_id, **content}, to=sid)


@sio.on("getAllContent")
async def on_get_all_content(sid, data):
    session_ids = data.get("sessionIds") or []

    async def one(session_id):
        content = await read_content(session_id)
        if content and content["lines"]:
            await sio.emit(
                "content", {"sessionId": session_id, **content}, to=sid)

    await asyncio.gather(*(one(s) for s in session_ids))


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
    global connection, itermapp, screen_size
    screen_size = read_screen_size()
    print("Screen size:", screen_size)

    connection = await iterm2.Connection.async_create()
    itermapp = await iterm2.async_get_app(connection)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Server running on http://0.0.0.0:{PORT}")

    asyncio.create_task(layout_monitor())
    asyncio.create_task(focus_monitor())
    asyncio.create_task(sync_loop())

    # Clean shutdown (incl. `iterm-server stop`, which sends SIGTERM).
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
