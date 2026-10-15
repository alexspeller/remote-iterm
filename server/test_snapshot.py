import asyncio
import json
import os
import signal
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import iterm2
import iterm2.rpc
from iterm2 import api_pb2

from server import snapshot as S
from server.restore import pane_ids, split_count
from server.test_geometry import FakeSplit
from server.test_terminal_lines import line, proto_line


def make_clean(cwd="/Users/x/proj", captured="2026-08-19T10:00:00+00:00"):
    return {
        "version": 1,
        "capturedAt": captured,
        "windows": [{
            "id": "w1", "isFront": True, "bounds": {"x": 0, "y": 0, "w": 9, "h": 9},
            "tabs": [{
                "index": 1, "title": "proj", "isSelected": True,
                "currentSessionId": "p:A", "aspect": 1.7, "maximized": False,
                "usingCachedLayout": False,
                "tree": {"type": "pane", "id": "p:A"},
                "layout": "┌┐\n└┘",
                "panes": [{
                    "id": "p:A", "name": "fish", "cwd": cwd, "job": "vim",
                    "customDir": "/Users/x/proj",
                    "cmd": "vim file", "cols": 80, "rows": 24,
                    "rect": {"x": 0, "y": 0, "w": 1, "h": 1},
                    "contentFile": "panes/p_A.txt", "contentLines": 3,
                    "styledFile": "panes/p_A.ansi",
                }],
            }],
        }],
    }


class HelperTest(unittest.TestCase):
    def test_tilde(self):
        home = str(Path.home())
        self.assertEqual(S._tilde(home), "~")
        self.assertEqual(S._tilde(home + "/dexory"), "~/dexory")
        self.assertEqual(S._tilde("/etc/hosts"), "/etc/hosts")

    def test_safe_id(self):
        self.assertEqual(S._safe_id("w0t1p2:AB-cd.ef"), "w0t1p2_AB-cd.ef")

    def test_history_record_strips_content_keeps_tree(self):
        rec = S._history_record(make_clean())
        pane = rec["windows"][0]["tabs"][0]["panes"][0]
        self.assertNotIn("contentFile", pane)
        self.assertNotIn("styledFile", pane)
        self.assertNotIn("contentLines", pane)
        self.assertNotIn("_content", pane)
        self.assertIn("cwd", pane)
        self.assertEqual(pane["customDir"], "/Users/x/proj")  # kept for restore
        self.assertIn("tree", rec["windows"][0]["tabs"][0])
        self.assertIn("layout", rec["windows"][0]["tabs"][0])

    def test_render_snapshot_text(self):
        text = S.render_snapshot_text(make_clean())
        self.assertIn("Window 1", text)
        self.assertIn("Tab 1: proj", text)
        self.assertIn("* ", text)          # focused-pane marker
        self.assertIn("$ vim file", text)  # last command in the table


class TailTextsTest(unittest.TestCase):
    def test_plain_and_styled_hold_the_same_lines(self):
        lines = [
            line([]),
            line(["\x00", "\x00"]),
            line(list("ok") + ["\x00"] + list("go"),
                 [({"fgStandard": 2}, 2), ({}, 3)]),
            line([]),
            line(list("next")),
            line([" ", " "]),
        ]
        plain, styled = S.tail_texts(lines)
        # Blank lines at either end are dropped, ones in the middle kept; the
        # cell Claude Code skipped over reads as the space it looked like.
        self.assertEqual(plain, "ok go\n\nnext")
        self.assertEqual(styled, "\x1b[0;32mok\x1b[0m go\n\nnext")

    def test_an_empty_pane_has_no_text(self):
        self.assertEqual(S.tail_texts([line([]), line(["\x00"])]), ("", ""))


class WriteLatestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = (S.LATEST_DIR, S.PANES_DIR)
        S.LATEST_DIR = root / "latest"
        S.PANES_DIR = S.LATEST_DIR / "panes"

    def tearDown(self):
        S.LATEST_DIR, S.PANES_DIR = self._orig
        self._tmp.cleanup()

    def test_writes_plain_and_styled_content_and_drops_stale_files(self):
        S.PANES_DIR.mkdir(parents=True)
        (S.PANES_DIR / "gone.txt").write_text("old\n")
        (S.PANES_DIR / "gone.ansi").write_text("old\n")
        snap = make_clean()
        pane = snap["windows"][0]["tabs"][0]["panes"][0]
        pane["_content"] = "red text"
        pane["_styled"] = "\x1b[0;31mred\x1b[0m text"

        clean = S.Snapshotter(None, None)._write_latest(snap)

        self.assertEqual((S.PANES_DIR / "p_A.txt").read_text(), "red text\n")
        self.assertEqual((S.PANES_DIR / "p_A.ansi").read_text(),
                         "\x1b[0;31mred\x1b[0m text\n")
        self.assertEqual(sorted(p.name for p in S.PANES_DIR.iterdir()),
                         ["p_A.ansi", "p_A.txt"])
        state = json.loads((S.LATEST_DIR / "state.json").read_text())
        saved = state["windows"][0]["tabs"][0]["panes"][0]
        self.assertEqual(saved["styledFile"], "panes/p_A.ansi")
        self.assertNotIn("_content", saved)
        self.assertNotIn("_styled", saved)
        self.assertNotIn("_styled", clean["windows"][0]["tabs"][0]["panes"][0])


class GridTreeTest(unittest.TestCase):
    def test_grid_tree_covers_every_pane(self):
        for n in range(1, 10):
            ids = [f"p{i}" for i in range(n)]
            tree = S._grid_tree(ids)
            self.assertEqual(pane_ids(tree), ids)           # all panes, in order
            self.assertEqual(split_count(tree), n - 1)      # nothing lost

    def test_grid_tree_single_and_empty(self):
        self.assertEqual(S._grid_tree(["only"]), {"type": "pane", "id": "only"})
        self.assertEqual(S._grid_tree([])["type"], "pane")


def _today_at(hour, minute=0, second=0):
    """A capturedAt on today's date.

    Archive names are _safe_id(capturedAt) and _prune_sessions deletes any whose
    name is more than snapshot.RETENTION_DAYS old, so a hardcoded literal here is
    a time bomb: these tests carried "2026-08-19" and passed for exactly 14 days
    before every archive started being pruned the instant it was written. Dates
    relative to today keep the archive inside the retention window forever, while
    the varying hour preserves the ordering the tests actually care about.
    """
    return f"{date.today().isoformat()}T{hour:02d}:{minute:02d}:{second:02d}+00:00"


class SessionArchiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # Also redirect LOG_PATH so the test's log() calls don't pollute the
        # real snapshot.log.
        self._orig = (S.LATEST_DIR, S.SESSIONS_DIR, S.MAX_SESSION_ARCHIVES,
                      S.LOG_PATH, S.SNAPSHOT_DIR)
        S.LATEST_DIR = root / "latest"
        S.SESSIONS_DIR = root / "sessions"
        S.MAX_SESSION_ARCHIVES = 3
        S.SNAPSHOT_DIR = root
        S.LOG_PATH = root / "snapshot.log"
        (S.LATEST_DIR / "panes").mkdir(parents=True)

    def tearDown(self):
        (S.LATEST_DIR, S.SESSIONS_DIR, S.MAX_SESSION_ARCHIVES,
         S.LOG_PATH, S.SNAPSHOT_DIR) = self._orig
        self._tmp.cleanup()

    def _write_latest(self, captured, windows=None):
        if windows is None:
            windows = [{"id": "w1", "tabs": [{"index": 1, "panes": [{"id": "p:A"}]}]}]
        (S.LATEST_DIR / "state.json").write_text(
            json.dumps({"capturedAt": captured, "windows": windows}))
        (S.LATEST_DIR / "panes" / "p_A.txt").write_text("scrollback here\n")

    def test_archive_preserves_latest_with_content(self):
        self._write_latest(_today_at(10))
        S.Snapshotter(None, None)._archive_previous_session()
        archives = S.session_archives()
        self.assertEqual(len(archives), 1)
        # the archived session carries the pane content (needed for echo-restore)
        self.assertTrue((archives[0] / "panes" / "p_A.txt").exists())
        self.assertTrue((archives[0] / "state.json").exists())

    def test_archive_is_idempotent_per_capture(self):
        self._write_latest(_today_at(10))
        snap = S.Snapshotter(None, None)
        snap._archive_previous_session()
        snap._archive_previous_session()  # same capturedAt -> no duplicate
        self.assertEqual(len(S.session_archives()), 1)

    def test_archives_are_newest_first_and_pruned(self):
        snap = S.Snapshotter(None, None)
        for captured in [_today_at(h) for h in (9, 10, 11, 12, 13)]:
            self._write_latest(captured)
            snap._archive_previous_session()
        archives = S.session_archives()
        self.assertEqual(len(archives), 3)  # MAX_SESSION_ARCHIVES
        # newest first, and the oldest two were pruned
        today = date.today().isoformat()
        self.assertTrue(archives[0].name.startswith(f"{today}T13"))
        self.assertTrue(archives[-1].name.startswith(f"{today}T11"))

    def test_no_latest_no_archive(self):
        S.Snapshotter(None, None)._archive_previous_session()  # no state.json
        self.assertEqual(S.session_archives(), [])

    def test_empty_latest_is_not_archived(self):
        # A graceful iTerm quit tears windows down while the snapshotter is still
        # alive, leaving an empty `latest/`. Archiving that would erase the
        # recoverable session — so it must be refused.
        self._write_latest(_today_at(14, 19, 2), windows=[])
        S.Snapshotter(None, None)._archive_previous_session()
        self.assertEqual(S.session_archives(), [])

    def test_existing_empty_archive_is_pruned_and_hidden(self):
        # A real session, then a bug-artifact empty archive already on disk.
        self._write_latest(_today_at(11))
        S.Snapshotter(None, None)._archive_previous_session()
        empty_captured = _today_at(14, 19, 2)
        empty_name = S._safe_id(empty_captured)
        empty = S.SESSIONS_DIR / empty_name
        (empty / "panes").mkdir(parents=True)
        (empty / "state.json").write_text(
            json.dumps({"capturedAt": empty_captured, "windows": []}))
        # Never offered as restorable...
        names = [d.name for d in S.session_archives()]
        self.assertNotIn(empty_name, names)
        self.assertEqual(len(names), 1)
        # ...and physically removed on the next prune.
        S._prune_sessions()
        self.assertFalse(empty.exists())


class HistoryFsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = S.HISTORY_DIR
        S.HISTORY_DIR = Path(self._tmp.name)

    def tearDown(self):
        S.HISTORY_DIR = self._orig
        self._tmp.cleanup()

    def _lines(self):
        files = sorted(S.HISTORY_DIR.glob("*.jsonl"))
        return [ln for f in files for ln in f.read_text().splitlines() if ln]

    def test_append_dedupes_unchanged(self):
        snap = S.Snapshotter(None, None)
        snap._append_history(make_clean(cwd="/a"))
        snap._append_history(make_clean(cwd="/a"))  # identical -> no new line
        self.assertEqual(len(self._lines()), 1)
        snap._append_history(make_clean(cwd="/b"))  # changed -> new line
        self.assertEqual(len(self._lines()), 2)

    def test_prune_drops_files_older_than_retention(self):
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=S.RETENTION_DAYS + 1)).isoformat()
        fresh = date.today().isoformat()
        (S.HISTORY_DIR / f"{old}.jsonl").write_text('{"x":1}\n')
        (S.HISTORY_DIR / f"{fresh}.jsonl").write_text('{"x":1}\n')
        S._prune_history()
        self.assertFalse((S.HISTORY_DIR / f"{old}.jsonl").exists())
        self.assertTrue((S.HISTORY_DIR / f"{fresh}.jsonl").exists())


class _FakeWebSocket:
    def __init__(self, closed=False):
        self.closed = closed
        self.close_code = 1006 if closed else None


class _FakeConnection:
    """Stands in for iterm2.Connection, exposing only what
    _iterm_connection_dead inspects."""

    def __init__(self, *, closed=False, dispatcher_done=False):
        self.websocket = _FakeWebSocket(closed)
        future = asyncio.get_event_loop().create_future()
        if dispatcher_done:
            future.set_result(None)
        self._Connection__dispatch_forever_future = future

    def cleanup(self):
        if not self._Connection__dispatch_forever_future.done():
            self._Connection__dispatch_forever_future.cancel()


class SnapshotTimeoutTest(unittest.IsolatedAsyncioTestCase):
    """A full build issues one unbounded RPC after another, so a Mac slow
    enough to stall iTerm2 can make one take minutes. The scheduler awaits
    snapshot(), so a build that never finishes stops every later snapshot
    too — it has to be abandoned and retried instead."""

    async def test_abandons_a_build_that_never_finishes(self):
        messages = []

        async def never_finishes(_self):
            await asyncio.Event().wait()

        snapshotter = S.Snapshotter.__new__(S.Snapshotter)
        with patch.object(S, "SNAPSHOT_TIMEOUT_SECONDS", 0.01), \
             patch.object(S, "log", messages.append), \
             patch.object(S.Snapshotter, "build", never_finishes):
            await asyncio.wait_for(snapshotter.snapshot(), timeout=5)

        self.assertTrue(any("abandoned" in m for m in messages), messages)

    async def test_a_normal_build_still_writes(self):
        written = []

        async def quick_build(_self):
            return {"windows": [{"id": "w1"}]}

        snapshotter = S.Snapshotter.__new__(S.Snapshotter)
        with patch.object(S, "log", lambda *a, **k: None), \
             patch.object(S.Snapshotter, "build", quick_build), \
             patch.object(S.Snapshotter, "_write_latest",
                          lambda self, snap: written.append(snap) or snap), \
             patch.object(S.Snapshotter, "_append_history", lambda self, c: None):
            await snapshotter.snapshot()

        self.assertEqual(len(written), 1)


class ConnectionDeadTest(unittest.IsolatedAsyncioTestCase):
    """Same distinction as server.py's identical helper: slow is not dead."""

    async def test_open_connection_with_a_live_dispatcher_is_not_dead(self):
        connection = _FakeConnection()
        self.addCleanup(connection.cleanup)
        self.assertFalse(S._iterm_connection_dead(connection))

    async def test_closed_websocket_is_dead(self):
        connection = _FakeConnection(closed=True)
        self.addCleanup(connection.cleanup)
        self.assertTrue(S._iterm_connection_dead(connection))

    async def test_finished_dispatch_loop_is_dead(self):
        connection = _FakeConnection(dispatcher_done=True)
        self.addCleanup(connection.cleanup)
        self.assertTrue(S._iterm_connection_dead(connection))


class ConnectionWatchdogTest(unittest.IsolatedAsyncioTestCase):
    """Same rationale as server.py's identical test: the iterm2 library never
    reconnects mid-session, so a dead connection just hangs every future
    call forever, silently. connection_watchdog is the periodic probe that
    catches that silence and exits the process for a supervised restart —
    without mistaking a Mac that is merely busy for one that has gone away.
    """

    def setUp(self):
        for entry in (patch.object(S, "WATCHDOG_INTERVAL", 0),
                      patch.object(S, "WATCHDOG_RETRY_INTERVAL", 0),
                      # log() appends to the real snapshots/snapshot.log.
                      patch.object(S, "log", lambda *a, **k: None)):
            entry.start()
            self.addCleanup(entry.stop)

    def _live_connection(self):
        connection = _FakeConnection()
        self.addCleanup(connection.cleanup)
        return connection

    async def test_intermittent_stalls_never_accumulate_into_a_restart(self):
        """A snapshotter restart re-reads every pane and archives the outgoing
        session, so restarting on a transient stall feeds the next stall."""
        stop = asyncio.Event()
        probes = 0

        async def stall_every_other_probe(_connection):
            nonlocal probes
            probes += 1
            if probes % 2:
                raise asyncio.TimeoutError()
            return object()

        with patch.object(S, "_probe_iterm",
                          side_effect=stall_every_other_probe):
            task = asyncio.create_task(
                S.connection_watchdog(self._live_connection(), stop))
            for _ in range(20_000):
                if probes >= 10 * S.WATCHDOG_FAILURES_BEFORE_RESTART:
                    break
                await asyncio.sleep(0)
            else:
                self.fail("watchdog stopped probing")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(stop.is_set())

    async def test_restarts_once_stalls_run_consecutively(self):
        stop = asyncio.Event()
        probes = 0

        async def always_stalls(_connection):
            nonlocal probes
            probes += 1
            raise asyncio.TimeoutError()

        with patch.object(S, "_probe_iterm", side_effect=always_stalls):
            await S.connection_watchdog(self._live_connection(), stop)

        self.assertTrue(stop.is_set())
        self.assertEqual(probes, S.WATCHDOG_FAILURES_BEFORE_RESTART)

    async def test_restarts_immediately_when_the_connection_is_dead(self):
        stop = asyncio.Event()
        connection = _FakeConnection(closed=True)
        self.addCleanup(connection.cleanup)
        probes = 0

        async def fail(_connection):
            nonlocal probes
            probes += 1
            raise RuntimeError("boom")

        with patch.object(S, "_probe_iterm", side_effect=fail):
            await S.connection_watchdog(connection, stop)

        self.assertTrue(stop.is_set())
        self.assertEqual(probes, 1)

    async def test_keeps_running_while_iterm2_answers(self):
        stop = asyncio.Event()
        calls = 0

        async def fake_probe(_connection):
            nonlocal calls
            calls += 1
            return object()

        with patch.object(S, "_probe_iterm",
                          side_effect=fake_probe):
            task = asyncio.create_task(
                S.connection_watchdog(self._live_connection(), stop))
            for _ in range(20_000):
                if calls >= 1:
                    break
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertGreaterEqual(calls, 1)
        self.assertFalse(stop.is_set())


class SnapshotterShutdownTest(unittest.IsolatedAsyncioTestCase):
    """Regression test for a bug caught during manual verification:
    Snapshotter.run() must always exit the process (never just return) when
    told to stop. iterm2.run_forever awaits its own internal dispatch-forever
    task *after* the wrapped coroutine returns, and that task only completes
    when the websocket closes — which run() never does itself. A version of
    run() that returned normally on SIGINT/SIGTERM hung the whole process
    forever instead of exiting, silently breaking both `iterm-server stop`
    and the watchdog-triggered restart (a stale snapshot.py process kept
    running under the old pid instead of actually restarting).
    """

    async def test_signal_exits_the_process_instead_of_hanging(self):
        # run() must be awaited directly here, not wrapped in its own Task:
        # asyncio's Task machinery doesn't route a BaseException like
        # SystemExit through the awaiting `await task`, so a Task wrapper
        # would mask exactly the distinction this test exists to check. This
        # matches production anyway — iterm2.run_forever awaits the
        # equivalent coroutine directly, never as a sub-task.
        snap = S.Snapshotter(None, None)
        never = lambda *_a, **_kw: asyncio.Event().wait()
        loop = asyncio.get_running_loop()
        with patch.object(S, "_prune_history"), \
             patch.object(snap, "_archive_previous_session"), \
             patch.object(snap, "snapshot", new=AsyncMock()), \
             patch.object(snap, "_scheduler", new=never), \
             patch.object(snap, "_heartbeat", new=never), \
             patch.object(snap, "_layout_monitor", new=never), \
             patch.object(snap, "_focus_monitor", new=never), \
             patch.object(S, "connection_watchdog", new=never):
            loop.call_later(0.05, os.kill, os.getpid(), signal.SIGINT)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    async with asyncio.timeout(5):
                        await snap.run()
                self.assertEqual(ctx.exception.code, 0)
            finally:
                loop.remove_signal_handler(signal.SIGINT)



class _IdlePane:
    """iterm2.Session double for the snapshotter's content reads. ``rows``
    is the number of lines in its buffer; output appends to it. ``cursor``
    is where its cursor is."""

    def __init__(self, session_id="p:A", rows=30):
        self.session_id = session_id
        self.rows = rows
        self.cursor = (0, 5)
        self.grid_size = type("Size", (), {"width": 80, "height": 24})()
        self.tail_reads = 0

    async def async_get_line_info(self):
        return type("Info", (), {"overflow": 0, "scrollback_buffer_height": self.rows - 24,
                                 "mutable_area_height": 24})()


class ContentReadTest(unittest.IsolatedAsyncioTestCase):
    """Each tail read is hundreds of KB of work on iTerm2's main thread, so a
    pane whose buffer has not moved is not read again."""

    def setUp(self):
        self.now = 1000.0
        self.snapshotter = S.Snapshotter(connection=None, app=None)
        self.pane = _IdlePane()

        async def read_tail(_self, session, info, n=S.TAIL_LINES):
            session.tail_reads += 1
            if getattr(session, "fail_next", False):
                session.fail_next = False
                return None
            return (f"tail at {session.rows}", "")

        async def one_line(session, first, count):
            return [], session.cursor

        for entry in (patch.object(S, "async_get_lines_and_cursor", one_line),
                      patch.object(S, "time", type("Clock", (), {"monotonic": staticmethod(lambda: self.now)})),
                      patch.object(S.Snapshotter, "_read_tail", read_tail)):
            entry.start()
            self.addCleanup(entry.stop)

    async def test_an_idle_pane_is_not_read_again(self):
        await self.snapshotter._content_for(self.pane)
        self.now += S.CONTENT_TTL_SECONDS + 1
        texts = await self.snapshotter._content_for(self.pane)
        self.assertEqual(self.pane.tail_reads, 1)
        self.assertEqual(texts, ("tail at 30", ""))

    async def test_a_pane_with_new_output_is_read_again(self):
        await self.snapshotter._content_for(self.pane)
        self.now += S.CONTENT_TTL_SECONDS + 1
        self.pane.rows += 3
        texts = await self.snapshotter._content_for(self.pane)
        self.assertEqual(self.pane.tail_reads, 2)
        self.assertEqual(texts, ("tail at 33", ""))

    async def test_a_resized_pane_is_read_again(self):
        await self.snapshotter._content_for(self.pane)
        self.now += S.CONTENT_TTL_SECONDS + 1
        self.pane.grid_size = type("Size", (), {"width": 100, "height": 24})()
        await self.snapshotter._content_for(self.pane)
        self.assertEqual(self.pane.tail_reads, 2)

    async def test_even_an_idle_pane_is_read_again_eventually(self):
        await self.snapshotter._content_for(self.pane)
        self.now += S.CONTENT_MAX_AGE_SECONDS
        await self.snapshotter._content_for(self.pane)
        self.assertEqual(self.pane.tail_reads, 2)

    async def test_output_on_a_screen_not_yet_full_is_noticed(self):
        # A new pane or one just cleared: output moves the cursor down the
        # screen without changing any line count.
        await self.snapshotter._content_for(self.pane)
        self.now += S.CONTENT_TTL_SECONDS + 1
        self.pane.cursor = (0, 9)
        await self.snapshotter._content_for(self.pane)
        self.assertEqual(self.pane.tail_reads, 2)

    async def test_a_failed_read_is_retried_rather_than_kept(self):
        self.pane.fail_next = True
        self.assertEqual(await self.snapshotter._content_for(self.pane), ("", ""))
        self.now += S.CONTENT_TTL_SECONDS + 1
        texts = await self.snapshotter._content_for(self.pane)
        self.assertEqual(self.pane.tail_reads, 2)
        self.assertEqual(texts, ("tail at 30", ""))

    async def test_nothing_is_asked_of_iterm2_within_the_ttl(self):
        await self.snapshotter._content_for(self.pane)
        self.pane.async_get_line_info = AsyncMock(side_effect=AssertionError("asked"))
        await self.snapshotter._content_for(self.pane)
        self.assertEqual(self.pane.tail_reads, 1)


class _BufferPane(iterm2.Session):
    """An iTerm2 pane with a real buffer of text rows on a 80x24 screen,
    answering line-info requests itself and buffer reads through
    ``_FakeBuffers``. Subclasses iterm2.Session for geometry's isinstance
    check, without its constructor's connection."""

    def __init__(self, sid):
        self._sid = sid
        self.connection = None
        self.name = "fish"
        self.frame = type("Frame", (), {"size": type("Size", (), {
            "width": 640.0, "height": 400.0})()})()
        self.rows = [f"{sid} line {i}" for i in range(40)]
        self.cursor = (0, 39)

    @property
    def session_id(self):
        return self._sid

    @property
    def grid_size(self):
        return type("Size", (), {"width": 80, "height": 24})()

    def output(self, text):
        self.rows.append(text)
        self.cursor = (0, len(self.rows) - 1)

    async def async_get_line_info(self):
        return type("Info", (), {
            "overflow": 0,
            "scrollback_buffer_height": max(0, len(self.rows) - 24),
            "mutable_area_height": 24})()

    async def async_get_variable(self, name):
        return ""

    async def async_get_profile(self):
        raise RuntimeError("no profile in tests")


class _FakeBuffers:
    """Stands in for iTerm2's get_buffer RPC, recording how many lines each
    request asked for: a one-line request is the snapshotter's cursor check,
    anything longer a tail read."""

    def __init__(self, panes):
        self.panes = {pane.session_id: pane for pane in panes}
        self.requests: list[tuple[str, int]] = []

    async def __call__(self, connection, session, windowed_coord_range=None,
                       style=False):
        pane = self.panes[session]
        rng = windowed_coord_range.coordRange
        first, end = rng.start.y, rng.end.y
        self.requests.append((session, end - first))
        message = api_pb2.ServerOriginatedMessage()
        buffer = message.get_buffer_response
        buffer.status = api_pb2.GetBufferResponse.Status.Value("OK")
        for row in pane.rows[first:end]:
            buffer.contents.add().CopyFrom(proto_line(list(row)))
        buffer.cursor.x, buffer.cursor.y = pane.cursor
        return message

    def tail_reads(self) -> list[str]:
        return [sid for sid, count in self.requests if count > 1]


class HeartbeatContentTest(unittest.IsolatedAsyncioTestCase):
    """The heartbeat keeps the snapshot fresh without re-reading panes that
    have not moved: each tail read is ~200 styled lines that iTerm2 renders
    as hundreds of KB of text on its main thread."""

    def setUp(self):
        self.now = 1000.0
        # The real interval, for the fake clock; the task itself is made to
        # beat at once.
        self.beat = S.HEARTBEAT_SECONDS
        self.idle = _BufferPane("p:idle")
        self.busy = _BufferPane("p:busy")
        tab = type("Tab", (), {
            "tab_id": "t1",
            "root": FakeSplit(True, [self.idle, self.busy]),
            "minimized_sessions": [],
            "all_sessions": [self.idle, self.busy],
            "current_session": self.idle,
            "async_get_variable": AsyncMock(return_value=""),
        })()
        window = type("Window", (), {
            "window_id": "w1", "tabs": [tab], "current_tab": tab,
            "async_get_frame": AsyncMock(side_effect=RuntimeError("no frame")),
        })()
        app = type("App", (), {"terminal_windows": [window],
                               "current_window": window})()
        self.snapshotter = S.Snapshotter(connection=None, app=app)
        self.buffers = _FakeBuffers([self.idle, self.busy])
        clock = type("Clock", (), {"monotonic": staticmethod(lambda: self.now)})
        for entry in (patch.object(iterm2.rpc, "async_get_screen_contents", self.buffers),
                      patch.object(S, "time", clock),
                      patch.object(S, "PANE_READ_DELAY", 0),
                      patch.object(S, "HEARTBEAT_SECONDS", 0)):
            entry.start()
            self.addCleanup(entry.stop)

    async def heartbeat(self):
        """One beat of the real heartbeat task, and the build it triggers."""
        task = asyncio.create_task(self.snapshotter._heartbeat())
        try:
            await asyncio.wait_for(self.snapshotter.trigger.wait(), timeout=5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.snapshotter.trigger.clear()
        self.buffers.requests.clear()
        return await self.snapshotter.build()

    def content(self, snapshot, pane):
        panes = snapshot["windows"][0]["tabs"][0]["panes"]
        return next(p["_content"] for p in panes if p["id"] == pane.session_id)

    async def test_a_heartbeat_issues_no_tail_read_for_an_unmoved_pane(self):
        await self.snapshotter.build()
        self.now += self.beat
        snapshot = await self.heartbeat()
        self.assertEqual(self.buffers.tail_reads(), [])
        self.assertIn("p:idle line 39", self.content(snapshot, self.idle))

    async def test_idle_panes_cost_no_tail_reads_across_many_heartbeats(self):
        await self.snapshotter.build()
        reads = []
        while self.now + self.beat < 1000.0 + S.CONTENT_MAX_AGE_SECONDS:
            self.now += self.beat
            await self.heartbeat()
            reads += self.buffers.tail_reads()
        self.assertEqual(reads, [])

    async def test_a_heartbeat_rereads_only_the_pane_with_new_output(self):
        await self.snapshotter.build()
        self.now += self.beat
        self.busy.output("fresh output")
        snapshot = await self.heartbeat()
        self.assertEqual(self.buffers.tail_reads(), ["p:busy"])
        self.assertTrue(self.content(snapshot, self.busy).endswith("fresh output"))

    async def test_a_heartbeat_checks_a_pane_read_moments_before_it(self):
        # The heartbeat's timer and each pane's read time drift: a pane read
        # just under CONTENT_TTL_SECONDS before a heartbeat is still checked
        # by it, so the snapshot is never a whole extra heartbeat behind.
        await self.snapshotter.build()
        self.now += S.CONTENT_TTL_SECONDS - 1
        self.busy.output("fresh output")
        snapshot = await self.heartbeat()
        self.assertEqual(self.buffers.tail_reads(), ["p:busy"])
        self.assertTrue(self.content(snapshot, self.busy).endswith("fresh output"))

    async def test_a_heartbeat_still_rereads_an_idle_pane_at_the_max_age(self):
        await self.snapshotter.build()
        self.now += S.CONTENT_MAX_AGE_SECONDS
        await self.heartbeat()
        self.assertEqual(sorted(self.buffers.tail_reads()), ["p:busy", "p:idle"])

    async def test_builds_between_heartbeats_ask_nothing_within_the_ttl(self):
        await self.snapshotter.build()
        self.now += 1
        self.buffers.requests.clear()
        await self.snapshotter.build()
        self.assertEqual(self.buffers.requests, [])


class ForgetClosedTest(unittest.TestCase):
    def test_caches_drop_panes_and_tabs_that_have_closed(self):
        pane = type("Session", (), {"session_id": "open"})()
        tab = type("Tab", (), {"tab_id": "t1", "all_sessions": [pane]})()
        window = type("Window", (), {"tabs": [tab]})()
        app = type("App", (), {"terminal_windows": [window]})()
        snapshotter = S.Snapshotter(connection=None, app=app)
        snapshotter.content_cache.update({"open": (0, (), ("", "")), "closed": (0, (), ("", ""))})
        snapshotter.custom_dir_cache.update({"open": "", "closed": "/x"})
        snapshotter.last_good.update({"t1": {}, "t-closed": {}})

        snapshotter._forget_closed()

        self.assertEqual(list(snapshotter.content_cache), ["open"])
        self.assertEqual(list(snapshotter.custom_dir_cache), ["open"])
        self.assertEqual(list(snapshotter.last_good), ["t1"])


if __name__ == "__main__":
    unittest.main()
