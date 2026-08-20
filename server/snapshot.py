#!/usr/bin/env python3
"""iTerm2 state snapshotter — an always-on safety net for crash recovery.

Runs as its own process (spawned by the ``iterm-server`` launcher / AutoLaunch
supervisor) with its own iTerm2 API connection. On layout/focus changes and a
periodic heartbeat it serializes every window → tab → pane — the split geometry,
each pane's cwd / foreground job / last command line, and a plain-text tail of
its output — and writes:

  <support>/snapshots/latest/layout.txt   human-readable ASCII layout + table
  <support>/snapshots/latest/state.json   full structured snapshot (for restore)
  <support>/snapshots/latest/panes/*.txt   per-pane ~200-line plain-text tail
  <support>/snapshots/history/DATE.jsonl   one changed layout+metadata record/line

where <support> is ~/Library/Application Support/remote-iterm. History keeps
layout + metadata only (no content) and is pruned to 14 days; the latest snapshot
keeps full per-pane content for the crash-recovery case.

This is deliberately independent of the phone server: a bug there can't stop the
snapshots, and the snapshots keep flowing whether or not any phone is connected.
"""
import asyncio
import copy
import json
import math
import os
import shutil
import tempfile
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

import iterm2

try:
    from .ascii_layout import render as render_ascii
    from .geometry import pane_layout, serialize_tree
except ImportError:  # Running directly from the server directory.
    from ascii_layout import render as render_ascii
    from geometry import pane_layout, serialize_tree

SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "remote-iterm"
SNAPSHOT_DIR = SUPPORT_DIR / "snapshots"
LATEST_DIR = SNAPSHOT_DIR / "latest"
PANES_DIR = LATEST_DIR / "panes"
HISTORY_DIR = SNAPSHOT_DIR / "history"
# Full, content-bearing archives of each completed session. One is created at
# snapshotter startup from the outgoing `latest/` *before* the new session
# overwrites it — so reopening iTerm after a crash preserves the pre-crash
# session (layout + content) instead of clobbering it with the blank one.
SESSIONS_DIR = SNAPSHOT_DIR / "sessions"
LOG_PATH = SNAPSHOT_DIR / "snapshot.log"

SNAPSHOT_VERSION = 1
TAIL_LINES = 200
RETENTION_DAYS = 14
MAX_SESSION_ARCHIVES = 20

DEBOUNCE_SECONDS = 1.5      # coalesce a burst of change notifications
MIN_INTERVAL_SECONDS = 4.0  # floor between successive snapshots
HEARTBEAT_SECONDS = 30.0    # guarantee freshness + refresh content/cwd/job
CONTENT_TTL_SECONDS = 8.0   # don't re-read a pane's content more often than this
ASCII_COLS = 60


def log(msg: str) -> None:
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError:
        pass


def _tilde(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _safe_id(session_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in session_id)


def _grid_tree(ids: list[str]) -> dict:
    """A splitter tree matching pane_layout's even-grid fallback (row-major).

    Used for a tab that is maximized and was never seen unmaximized, so the live
    split tree has collapsed to one pane. Rows stack top→bottom; each row's panes
    sit left→right — the same placement the ASCII even grid uses.
    """
    if not ids:
        return {"type": "pane", "id": None}
    if len(ids) == 1:
        return {"type": "pane", "id": ids[0]}
    cols = max(1, math.ceil(math.sqrt(len(ids))))
    rows = [ids[i:i + cols] for i in range(0, len(ids), cols)]
    row_nodes = []
    for row in rows:
        if len(row) == 1:
            row_nodes.append({"type": "pane", "id": row[0]})
        else:
            row_nodes.append({"type": "split", "vertical": True,
                              "children": [{"type": "pane", "id": i} for i in row]})
    if len(row_nodes) == 1:
        return row_nodes[0]
    return {"type": "split", "vertical": False, "children": row_nodes}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Snapshotter:
    def __init__(self, connection, app):
        self.connection = connection
        self.app = app
        self.trigger = asyncio.Event()
        # tab_id -> {"rects", "aspect", "tree"} last captured while NOT maximized,
        # so a currently-maximized tab still renders/restores its real layout.
        self.last_good: dict[str, dict] = {}
        # session_id -> (monotonic_ts, text) so rapid triggers don't re-read all
        # panes; the heartbeat keeps them fresh.
        self.content_cache: dict[str, tuple[float, str]] = {}
        # session_id -> profile custom initial directory ("" if not custom). Read
        # once; a profile's working-directory setting doesn't change per session.
        self.custom_dir_cache: dict[str, str] = {}
        self.last_history_key: str | None = None

    # --- content -----------------------------------------------------------

    async def _read_tail(self, session, n: int = TAIL_LINES) -> str:
        try:
            async with iterm2.Transaction(self.connection):
                info = await session.async_get_line_info()
                available_first = info.overflow
                terminal_end = (available_first + info.scrollback_buffer_height
                                + info.mutable_area_height)
                first = max(available_first, terminal_end - n)
                count = terminal_end - first
                if count <= 0:
                    return ""
                lines = await session.async_get_contents(first, count)
            text = "\n".join(line.string.rstrip() for line in lines)
            return text.strip("\n")
        except Exception as err:
            log(f"content read failed for {session.session_id}: {err}")
            return ""

    async def _content_for(self, session) -> str:
        now = time.monotonic()
        cached = self.content_cache.get(session.session_id)
        if cached is not None and now - cached[0] < CONTENT_TTL_SECONDS:
            return cached[1]
        text = await self._read_tail(session)
        self.content_cache[session.session_id] = (now, text)
        return text

    async def _custom_dir(self, session) -> str:
        """The pane's profile custom initial directory (e.g. a ~/bin/project
        tab), or "" if the profile isn't in custom-directory mode. Restore
        reapplies this so splitting a restored pane opens in the same place."""
        sid = session.session_id
        if sid in self.custom_dir_cache:
            return self.custom_dir_cache[sid]
        custom = ""
        try:
            profile = await session.async_get_profile()
            mode = profile.initial_directory_mode
            custom_value = iterm2.profile.InitialWorkingDirectory. \
                INITIAL_WORKING_DIRECTORY_CUSTOM.value
            if getattr(mode, "value", mode) == custom_value:
                custom = profile.custom_directory or ""
        except Exception:
            custom = ""
        self.custom_dir_cache[sid] = custom
        return custom

    # --- snapshot build ----------------------------------------------------

    async def _pane_entry(self, session, rect) -> dict:
        async def var(name):
            try:
                return await session.async_get_variable(name) or ""
            except Exception:
                return ""

        cwd = await var("path")
        job = await var("jobName")
        cmd = await var("commandLine")
        try:
            grid = session.grid_size
            cols, rows = int(grid.width), int(grid.height)
        except Exception:
            cols, rows = 0, 0
        content = await self._content_for(session)
        entry = {
            "id": session.session_id,
            "name": session.name or "",
            "cwd": cwd,
            "customDir": await self._custom_dir(session),
            "job": job,
            "cmd": cmd,
            "cols": cols,
            "rows": rows,
            "rect": ({"x": round(rect[0], 4), "y": round(rect[1], 4),
                      "w": round(rect[2], 4), "h": round(rect[3], 4)}
                     if rect is not None else None),
            "contentLines": content.count("\n") + 1 if content else 0,
            "contentFile": f"panes/{_safe_id(session.session_id)}.txt" if content else None,
            "_content": content,  # stripped before serialization; written to panes/
        }
        return entry

    async def _tab_entry(self, window, tab, index: int, current_tab_id) -> dict:
        async def tab_var(name):
            try:
                return await tab.async_get_variable(name)
            except Exception:
                return None

        # `title` is the displayed title (for the human-readable map). The
        # explicit `titleOverride` is what a tool like ~/bin/project sets
        # ("🧰 <name>"); restore replays it so a project tab keeps its identity.
        title = await tab_var("title")
        title_override = await tab_var("titleOverride")
        rects, aspect, maximized = pane_layout(tab)
        tree = serialize_tree(tab.root)

        using_cache = False
        if not maximized and rects:
            self.last_good[tab.tab_id] = {
                "rects": rects, "aspect": aspect, "tree": tree}
        elif maximized:
            cached = self.last_good.get(tab.tab_id)
            live_ids = {s.session_id for s in tab.all_sessions}
            cached_rects = ({k: v for k, v in cached["rects"].items()
                             if k in live_ids} if cached else {})
            if cached_rects:
                # Reuse the real geometry captured before this tab was maximized.
                rects, aspect, tree = cached_rects, cached["aspect"], cached["tree"]
                using_cache = True
            else:
                # Never seen unmaximized: serialize_tree collapsed to the single
                # maximized pane, so synthesize a grid tree over ALL panes to
                # match the even-grid rects — otherwise restore loses panes.
                ids = [s.session_id for s in tab.all_sessions]
                tree = _grid_tree(ids)

        panes = [await self._pane_entry(s, rects.get(s.session_id))
                 for s in tab.all_sessions]
        cur = tab.current_session
        return {
            "index": index,
            "title": title or "",
            "titleOverride": title_override or "",
            "isSelected": tab.tab_id == current_tab_id,
            "currentSessionId": cur.session_id if cur else "",
            "aspect": round(aspect, 4),
            "maximized": maximized,
            "usingCachedLayout": using_cache,
            "tree": tree,
            "panes": panes,
        }

    async def build(self) -> dict:
        current_window = self.app.current_window
        current_window_id = current_window.window_id if current_window else None
        windows = []
        for window in self.app.terminal_windows:
            try:
                frame = await window.async_get_frame()
                bounds = {"x": int(frame.origin.x), "y": int(frame.origin.y),
                          "w": int(frame.size.width), "h": int(frame.size.height)}
            except Exception:
                bounds = None
            current_tab = window.current_tab
            current_tab_id = current_tab.tab_id if current_tab else None
            tabs = [await self._tab_entry(window, tab, i, current_tab_id)
                    for i, tab in enumerate(window.tabs, start=1)]
            windows.append({
                "id": window.window_id,
                "isFront": window.window_id == current_window_id,
                "bounds": bounds,
                "tabs": tabs,
            })
        return {
            "version": SNAPSHOT_VERSION,
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "windows": windows,
        }

    # --- writing -----------------------------------------------------------

    def _write_latest(self, snapshot: dict) -> None:
        # Write per-pane content files, and strip the inline content out of the
        # JSON (state.json carries refs only).
        PANES_DIR.mkdir(parents=True, exist_ok=True)
        live_files = set()
        clean = copy.deepcopy(snapshot)
        for window in clean["windows"]:
            for tab in window["tabs"]:
                for pane in tab["panes"]:
                    content = pane.pop("_content", "")
                    if content:
                        fname = f"{_safe_id(pane['id'])}.txt"
                        _atomic_write(PANES_DIR / fname, content + "\n")
                        live_files.add(fname)
                tab["layout"] = render_tab_layout(tab)
        # Drop stale pane files for panes that no longer exist.
        for existing in PANES_DIR.glob("*.txt"):
            if existing.name not in live_files:
                try:
                    existing.unlink()
                except OSError:
                    pass
        _atomic_write(LATEST_DIR / "state.json",
                      json.dumps(clean, indent=2, sort_keys=True) + "\n")
        _atomic_write(LATEST_DIR / "layout.txt", render_snapshot_text(clean))
        return clean

    def _archive_previous_session(self) -> None:
        """Preserve the outgoing `latest/` (with content) as a session archive.

        Called once at startup, before the first snapshot of the new session
        overwrites `latest/`. After a crash+reopen this is what keeps the
        pre-crash session recoverable (``iterm-snapshot restore``) instead of it
        being replaced by the blank reopened session.
        """
        state = LATEST_DIR / "state.json"
        if not state.exists():
            return
        try:
            data = json.loads(state.read_text())
        except Exception:
            data = {}
        if not data.get("windows"):
            # Defense in depth: never archive an empty `latest/` as a "previous
            # session" (see the snapshot() guard). Nothing to recover from it.
            log("skipped archiving empty previous session")
            _prune_sessions()
            return
        captured = data.get("capturedAt", "")
        stamp = _safe_id(captured) if captured else "unknown"
        dest = SESSIONS_DIR / stamp
        if dest.exists():
            return  # already archived (e.g. a restart within the same second)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SESSIONS_DIR / (".tmp-" + stamp)
        try:
            if tmp.exists():
                shutil.rmtree(tmp)
            shutil.copytree(LATEST_DIR, tmp)
            os.replace(tmp, dest)
            log(f"archived previous session -> sessions/{stamp}")
        except Exception as err:
            log(f"archive previous session failed: {err}")
            shutil.rmtree(tmp, ignore_errors=True)
        _prune_sessions()

    def _append_history(self, clean: dict) -> None:
        record = _history_record(clean)
        key = json.dumps(record["windows"], sort_keys=True)
        if key == self.last_history_key:
            return
        self.last_history_key = key
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        day = date.today().isoformat()
        with open(HISTORY_DIR / f"{day}.jsonl", "a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        _prune_history()

    async def snapshot(self) -> None:
        try:
            snap = await self.build()
            if not snap["windows"]:
                # Zero terminal windows means iTerm is quitting (graceful quit
                # tears windows down while this process is still alive) or every
                # window was closed. Do NOT overwrite the last good `latest/`
                # with an empty snapshot: that erases the recoverable session and
                # then gets archived as a blank "previous session" on reopen. A
                # crash, by contrast, kills iTerm instantly with no teardown
                # notification, so `latest/` correctly retains the pre-crash state.
                log("skipped empty snapshot (0 windows — iTerm quitting/closed)")
                return
            clean = self._write_latest(snap)
            self._append_history(clean)
        except Exception:
            log("snapshot failed:\n" + traceback.format_exc())

    # --- scheduling --------------------------------------------------------

    async def _scheduler(self) -> None:
        while True:
            await self.trigger.wait()
            self.trigger.clear()
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self.trigger.clear()
            await self.snapshot()
            await asyncio.sleep(MIN_INTERVAL_SECONDS)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            # Heartbeat forces a full content refresh by aging out the cache.
            self.content_cache.clear()
            self.trigger.set()

    async def _layout_monitor(self) -> None:
        async with iterm2.LayoutChangeMonitor(self.connection) as monitor:
            while True:
                await monitor.async_get()
                self.trigger.set()

    async def _focus_monitor(self) -> None:
        async with iterm2.FocusMonitor(self.connection) as monitor:
            while True:
                await monitor.async_get_next_update()
                self.trigger.set()

    async def run(self) -> None:
        _prune_history()
        # Preserve the previous session BEFORE the first snapshot overwrites it.
        self._archive_previous_session()
        log("snapshotter started")
        await self.snapshot()  # capture immediately on startup
        await asyncio.gather(
            self._scheduler(),
            self._heartbeat(),
            self._layout_monitor(),
            self._focus_monitor(),
        )


# --- rendering (shared with the CLI) -------------------------------------------

def _history_record(clean: dict) -> dict:
    """Layout + metadata only (no content refs), for the history log."""
    record = {"capturedAt": clean["capturedAt"], "windows": []}
    for window in clean["windows"]:
        tabs = []
        for tab in window["tabs"]:
            panes = [{"id": p["id"], "name": p["name"], "cwd": p["cwd"],
                      "customDir": p.get("customDir", ""),
                      "job": p["job"], "cmd": p["cmd"], "rect": p["rect"]}
                     for p in tab["panes"]]
            tabs.append({
                "index": tab["index"], "title": tab["title"],
                "titleOverride": tab.get("titleOverride", ""),
                "isSelected": tab["isSelected"], "maximized": tab["maximized"],
                "currentSessionId": tab.get("currentSessionId", ""),
                "tree": tab["tree"],
                "layout": tab.get("layout") or render_tab_layout(tab),
                "panes": panes,
            })
        record["windows"].append({
            "id": window["id"], "isFront": window["isFront"],
            "bounds": window["bounds"], "tabs": tabs})
    return record


def render_tab_layout(tab: dict) -> str:
    rects = {p["id"]: (p["rect"]["x"], p["rect"]["y"], p["rect"]["w"], p["rect"]["h"])
             for p in tab["panes"] if p.get("rect")}
    labels = {}
    for p in tab["panes"]:
        base = os.path.basename((p["cwd"] or "").rstrip("/")) or _tilde(p["cwd"] or "")
        top = p["job"] or p["name"] or base
        lines = [top]
        if base and base != top:
            lines.append(base)
        labels[p["id"]] = lines
    return render_ascii(rects, labels, cols=ASCII_COLS, aspect=tab.get("aspect", 1.7))


def render_snapshot_text(clean: dict) -> str:
    out = []
    captured = clean.get("capturedAt", "")
    n_tabs = sum(len(w["tabs"]) for w in clean["windows"])
    n_panes = sum(len(t["panes"]) for w in clean["windows"] for t in w["tabs"])
    out.append(f"iTerm2 snapshot — {captured}")
    out.append(f"{len(clean['windows'])} window(s), {n_tabs} tab(s), {n_panes} pane(s)")
    out.append("")
    for w_index, window in enumerate(clean["windows"], start=1):
        front = "  (front)" if window["isFront"] else ""
        out.append(f"══ Window {w_index}{front} " + "═" * 40)
        for tab in window["tabs"]:
            sel = "  [selected]" if tab["isSelected"] else ""
            if tab.get("usingCachedLayout"):
                maxi = "  [maximized — last known layout]"
            elif tab["maximized"]:
                maxi = "  [maximized — approximate grid]"
            else:
                maxi = ""
            title = tab["title"] or "(untitled)"
            out.append("")
            out.append(f"Tab {tab['index']}: {title}{sel}{maxi}")
            layout = tab.get("layout") or render_tab_layout(tab)
            if layout:
                out.append(layout)
            out.append("  panes:")
            for pane in tab["panes"]:
                marker = "*" if pane["id"] == tab.get("currentSessionId") else " "
                short = pane["id"].split(":")[-1][:8]
                cwd = _tilde(pane["cwd"] or "")
                job = pane["job"] or ""
                cmd = pane["cmd"] or ""
                detail = cmd if cmd and cmd != job else job
                out.append(f"   {marker} {short:8}  {cwd}"
                           + (f"    $ {detail}" if detail else ""))
        out.append("")
    return "\n".join(out) + "\n"


def _prune_history() -> None:
    if not HISTORY_DIR.exists():
        return
    today = date.today()
    for f in HISTORY_DIR.glob("*.jsonl"):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if (today - d).days > RETENTION_DAYS:
            try:
                f.unlink()
            except OSError:
                pass


def _archive_is_empty(d: Path) -> bool:
    """A session archive with no windows carries nothing to restore (a teardown
    snapshot that slipped through). Unreadable archives count as empty too."""
    try:
        return not json.loads((d / "state.json").read_text()).get("windows")
    except Exception:
        return True


def session_archives() -> list:
    """Restorable session-archive dirs, newest first. Empty (0-window) archives
    are excluded so restore/list never offer a blank session. Names are
    _safe_id(capturedAt), which sort chronologically, so lexical sort ==
    chronological."""
    if not SESSIONS_DIR.exists():
        return []
    dirs = [d for d in SESSIONS_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".tmp-")
            and not _archive_is_empty(d)]
    return sorted(dirs, key=lambda d: d.name, reverse=True)


def _prune_sessions() -> None:
    if not SESSIONS_DIR.exists():
        return
    # First delete bug-artifact empties (e.g. a graceful-quit teardown snapshot
    # archived before the empty-snapshot guards existed).
    for d in SESSIONS_DIR.iterdir():
        if (d.is_dir() and not d.name.startswith(".tmp-")
                and _archive_is_empty(d)):
            shutil.rmtree(d, ignore_errors=True)
    archives = session_archives()  # non-empty only
    today = date.today()
    for i, d in enumerate(archives):
        too_many = i >= MAX_SESSION_ARCHIVES
        too_old = False
        try:
            too_old = (today - date.fromisoformat(d.name[:10])).days > RETENTION_DAYS
        except ValueError:
            pass
        if too_many or too_old:
            shutil.rmtree(d, ignore_errors=True)


async def main(connection) -> None:
    app = await iterm2.async_get_app(connection)
    snapshotter = Snapshotter(connection, app)
    await snapshotter.run()


if __name__ == "__main__":
    iterm2.run_forever(main)
