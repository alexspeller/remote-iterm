import asyncio
import json
import os
import signal
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import snapshot as S
from server.restore import pane_ids, split_count


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

        with patch("iterm2.rpc.async_list_sessions",
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

        with patch("iterm2.rpc.async_list_sessions", side_effect=always_stalls):
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

        with patch("iterm2.rpc.async_list_sessions", side_effect=fail):
            await S.connection_watchdog(connection, stop)

        self.assertTrue(stop.is_set())
        self.assertEqual(probes, 1)

    async def test_keeps_running_while_iterm2_answers(self):
        stop = asyncio.Event()
        calls = 0

        async def fake_list_sessions(_connection):
            nonlocal calls
            calls += 1
            return object()

        with patch("iterm2.rpc.async_list_sessions",
                   side_effect=fake_list_sessions):
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


if __name__ == "__main__":
    unittest.main()
