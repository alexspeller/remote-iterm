import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import server.server as server_module
from server.server import (
    _DEFAULT_PALETTE,
    _content_line_range,
    _line_runs,
    clients,
    delivery_wakeups,
    last_content,
    on_watch,
    palette_cache,
    pending_events,
    queue_client_event,
    queue_content_for_watchers,
    read_content,
    stream_tasks,
    watched_by_sid,
)


class _DefaultColor:
    is_rgb = False
    is_standard = False


class _Style:
    def __init__(self, *, faint=False):
        self.fg_color = _DefaultColor()
        self.bg_color = _DefaultColor()
        self.bold = False
        self.faint = faint
        self.inverse = False


class _Line:
    def __init__(self, text, faint_at=()):
        self.text = text
        faint_at = set(faint_at)
        self.styles = [_Style(faint=i in faint_at) for i in range(len(text))]

    def style_at(self, index):
        return self.styles[index] if index < len(self.styles) else None

    def string_at(self, index):
        return self.text[index]


class LineRunsTest(unittest.TestCase):
    def test_marks_cursor_without_shifting_text(self):
        self.assertEqual(
            _line_runs(_Line("abc"), _DEFAULT_PALETTE, cursor_x=1),
            [{"t": "a"}, {"t": "", "c": True}, {"t": "bc"}],
        )

    def test_preserves_cursor_on_an_otherwise_blank_line(self):
        self.assertEqual(
            _line_runs(_Line("    "), _DEFAULT_PALETTE, cursor_x=2),
            [{"t": "  "}, {"t": "", "c": True}],
        )

    def test_preserves_faint_as_a_distinct_style(self):
        self.assertEqual(
            _line_runs(_Line("ab", faint_at={1}), _DEFAULT_PALETTE),
            [{"t": "a"}, {"t": "b", "d": True}],
        )


class _LineInfo:
    def __init__(self, overflow=0, scrollback_buffer_height=0,
                 mutable_area_height=1):
        self.overflow = overflow
        self.scrollback_buffer_height = scrollback_buffer_height
        self.mutable_area_height = mutable_area_height


class _CursorCoord:
    def __init__(self, x=0, y=0):
        self.x = x
        self.y = y


class _ScreenContents:
    def __init__(self, x=0, y=0):
        self.cursor_coord = _CursorCoord(x, y)


class _FakeSession:
    """Minimal iterm2.Session double for read_content(). ``contents_gate``,
    if given, is awaited inside async_get_contents before returning — lets a
    test suspend the call indefinitely so it can cancel the reading task."""

    def __init__(self, lines, *, contents_gate=None):
        self.session_id = "session-1"
        self._lines = lines
        self._contents_gate = contents_gate

    async def async_get_line_info(self):
        return _LineInfo(overflow=0, mutable_area_height=len(self._lines))

    async def async_get_screen_contents(self):
        return _ScreenContents(y=-1)  # cursor off the fetched range

    async def async_get_contents(self, first, count):
        if self._contents_gate is not None:
            await self._contents_gate.wait()
        return self._lines[first:first + count]


class _FakeApp:
    def __init__(self, session):
        self._session = session

    def get_session_by_id(self, session_id):
        return self._session if session_id == self._session.session_id else None


class ReadContentTransactionSafetyTest(unittest.IsolatedAsyncioTestCase):
    """Regression test for the iTerm2-freezing bug: read_content() must never
    open an iterm2.Transaction. A transaction blocks iTerm2's entire main
    thread until explicitly ended. This read runs inside a stream task that
    gets cancelled on client disconnect; if that cancellation lands between
    the BEGIN and END RPCs, the BEGIN is stranded and iTerm2's whole GUI (not
    just this server) hangs forever waiting on an END that never arrives.
    async_get_contents already tolerates the screen changing mid-read by
    returning fewer lines than requested rather than raising, so there's no
    need for transactional atomicity here.
    """

    def tearDown(self):
        palette_cache.clear()

    async def test_normal_read_never_opens_a_transaction(self):
        session = _FakeSession([_Line("hello")])
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch("iterm2.rpc.async_start_transaction",
                   side_effect=AssertionError("must not open a transaction")):
            result = await read_content("session-1")

        self.assertIsNotNone(result)
        self.assertEqual(result["lines"], [[{"t": "hello"}]])

    async def test_cancellation_mid_read_never_opens_a_transaction(self):
        gate = asyncio.Event()  # never set: async_get_contents blocks forever
        session = _FakeSession([_Line("hello")], contents_gate=gate)
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch("iterm2.rpc.async_start_transaction",
                   side_effect=AssertionError("must not open a transaction")):
            task = asyncio.create_task(read_content("session-1"))
            await asyncio.sleep(0)  # let it suspend inside async_get_contents
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


async def _spin_until(predicate, max_iterations=20_000):
    """Yield to the loop until `predicate` holds. Bounded so a watchdog that
    stops probing fails the assertion instead of hanging the suite."""
    for _ in range(max_iterations):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


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
        # Single leading underscore, so Python does not mangle it further —
        # this is verbatim the mangled name the iterm2 library stores.
        self._Connection__dispatch_forever_future = future

    def cleanup(self):
        future = self._Connection__dispatch_forever_future
        if not future.done():
            future.cancel()


class ConnectHandshakeTest(unittest.IsolatedAsyncioTestCase):
    """python-socketio only acknowledges a Socket.IO connection once the
    `connect` handler returns, and the client gives up after 10s. Seeding a
    new client costs a dozen iTerm2 RPCs, so doing it inline made the
    handshake as slow as iTerm2's slowest moment — and on a busy Mac that
    meant a client that reconnected forever, each attempt timing out just
    before its state arrived.
    """

    async def asyncTearDown(self):
        for task in list(server_module.seed_tasks.values()):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server_module.seed_tasks.clear()
        clients.discard("sid-under-test")

    async def test_handshake_completes_while_iterm2_is_stalled(self):
        seeding_started = asyncio.Event()

        async def stalled_build_state():
            seeding_started.set()
            await asyncio.Event().wait()  # never returns, like a stalled iTerm2

        with patch.object(server_module, "shared_key", "the-key"), \
             patch.object(server_module, "start_client_delivery"), \
             patch.object(server_module, "build_state", stalled_build_state), \
             patch.object(server_module.sio, "emit", AsyncMock()):
            await asyncio.wait_for(
                server_module.connect(
                    "sid-under-test", {}, {"key": "the-key"}),
                timeout=1)
            # The seed really is running — it just isn't holding the handshake.
            await asyncio.wait_for(seeding_started.wait(), timeout=1)

        self.assertIn("sid-under-test", clients)
        self.assertIn("sid-under-test", server_module.seed_tasks)
        self.assertFalse(server_module.seed_tasks["sid-under-test"].done())

    async def test_disconnect_cancels_an_unfinished_seed(self):
        async def stalled_build_state():
            await asyncio.Event().wait()

        with patch.object(server_module, "shared_key", "the-key"), \
             patch.object(server_module, "start_client_delivery"), \
             patch.object(server_module, "stop_client_delivery", AsyncMock()), \
             patch.object(server_module, "stop_all_streams", AsyncMock()), \
             patch.object(server_module, "build_state", stalled_build_state), \
             patch.object(server_module.sio, "emit", AsyncMock()):
            await server_module.connect(
                "sid-under-test", {}, {"key": "the-key"})
            seed = server_module.seed_tasks["sid-under-test"]
            await server_module.disconnect("sid-under-test")
            await asyncio.gather(seed, return_exceptions=True)

        self.assertTrue(seed.cancelled())
        self.assertNotIn("sid-under-test", server_module.seed_tasks)


class ConnectionDeadTest(unittest.IsolatedAsyncioTestCase):
    """_iterm_connection_dead separates a connection that is merely slow from
    one that can never answer again. Getting this wrong in the "slow"
    direction is expensive: it restarts the process and drops every phone."""

    def _connection(self, **kwargs):
        connection = _FakeConnection(**kwargs)
        self.addCleanup(connection.cleanup)
        return connection

    async def test_open_connection_with_a_live_dispatcher_is_not_dead(self):
        with patch.object(server_module, "connection", self._connection()):
            self.assertFalse(server_module._iterm_connection_dead())

    async def test_closed_websocket_is_dead(self):
        connection = self._connection(closed=True)
        with patch.object(server_module, "connection", connection):
            self.assertTrue(server_module._iterm_connection_dead())

    async def test_finished_dispatch_loop_is_dead(self):
        """The library's read loop is what resolves pending responses; once it
        exits, every future call hangs forever with nothing to raise."""
        connection = self._connection(dispatcher_done=True)
        with patch.object(server_module, "connection", connection):
            self.assertTrue(server_module._iterm_connection_dead())

    async def test_missing_connection_is_dead(self):
        with patch.object(server_module, "connection", None):
            self.assertTrue(server_module._iterm_connection_dead())


class ConnectionWatchdogTest(unittest.IsolatedAsyncioTestCase):
    """The iterm2 library never reconnects mid-session — a dead connection
    just hangs every future call forever, silently. connection_watchdog is
    the periodic probe that catches that silence and exits the process for a
    supervised restart.

    It must not confuse that with slowness. iTerm2 answers API requests on
    its main thread, so a loaded Mac stalls every client for seconds at a
    time with nothing wrong; restarting on each of those dropped every phone
    every couple of minutes.
    """

    def setUp(self):
        self._patches = [
            patch.object(server_module, "WATCHDOG_INTERVAL", 0),
            patch.object(server_module, "WATCHDOG_RETRY_INTERVAL", 0),
        ]
        for entry in self._patches:
            entry.start()
            self.addCleanup(entry.stop)

    def _live_connection(self):
        connection = _FakeConnection()
        self.addCleanup(connection.cleanup)
        return patch.object(server_module, "connection", connection)

    async def test_one_stalled_probe_does_not_restart(self):
        stop = asyncio.Event()
        probes = 0

        async def stall_once(_connection):
            nonlocal probes
            probes += 1
            if probes == 1:
                raise asyncio.TimeoutError()
            return object()

        with self._live_connection(), \
             patch("iterm2.rpc.async_list_sessions", side_effect=stall_once):
            task = asyncio.create_task(
                server_module.connection_watchdog(stop))
            await _spin_until(lambda: probes >= 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(stop.is_set())

    async def test_intermittent_stalls_never_accumulate_into_a_restart(self):
        """The regression this guards: a Mac busy enough to stall iTerm2 every
        so often must not be restarted out from under its clients, however
        long it stays busy."""
        stop = asyncio.Event()
        probes = 0

        async def stall_every_other_probe(_connection):
            nonlocal probes
            probes += 1
            if probes % 2:
                raise asyncio.TimeoutError()
            return object()

        with self._live_connection(), \
             patch("iterm2.rpc.async_list_sessions",
                   side_effect=stall_every_other_probe):
            task = asyncio.create_task(
                server_module.connection_watchdog(stop))
            await _spin_until(
                lambda: probes >= 10 * server_module.WATCHDOG_FAILURES_BEFORE_RESTART)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertFalse(stop.is_set())

    async def test_restarts_once_stalls_run_consecutively(self):
        """A connection that answers nothing at all, but whose socket still
        looks open, is the one failure we can only detect by persistence."""
        stop = asyncio.Event()
        probes = 0

        async def always_stalls(_connection):
            nonlocal probes
            probes += 1
            raise asyncio.TimeoutError()

        with self._live_connection(), \
             patch("iterm2.rpc.async_list_sessions", side_effect=always_stalls):
            await server_module.connection_watchdog(stop)

        self.assertTrue(stop.is_set())
        self.assertEqual(probes,
                         server_module.WATCHDOG_FAILURES_BEFORE_RESTART)

    async def test_restarts_immediately_when_the_connection_is_dead(self):
        stop = asyncio.Event()
        probes = 0

        async def fail(_connection):
            nonlocal probes
            probes += 1
            raise RuntimeError("boom")

        connection = _FakeConnection(closed=True)
        self.addCleanup(connection.cleanup)
        with patch.object(server_module, "connection", connection), \
             patch("iterm2.rpc.async_list_sessions", side_effect=fail):
            await server_module.connection_watchdog(stop)

        self.assertTrue(stop.is_set())
        self.assertEqual(probes, 1)

    async def test_keeps_running_while_iterm2_answers(self):
        stop = asyncio.Event()
        calls = 0

        async def fake_list_sessions(_connection):
            nonlocal calls
            calls += 1
            return object()

        with self._live_connection(), \
             patch("iterm2.rpc.async_list_sessions",
                   side_effect=fake_list_sessions):
            task = asyncio.create_task(
                server_module.connection_watchdog(stop))
            await _spin_until(lambda: calls >= 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertGreaterEqual(calls, 1)
        self.assertFalse(stop.is_set())


class ContentLineRangeTest(unittest.TestCase):
    def test_returns_latest_bounded_page(self):
        class Info:
            overflow = 275
            scrollback_buffer_height = 10_000
            mutable_area_height = 40

        self.assertEqual(
            _content_line_range(Info(), 250),
            (10_065, 250, 275, 10_315),
        )

    def test_pages_back_to_the_first_retained_line(self):
        class Info:
            overflow = 275
            scrollback_buffer_height = 10_000
            mutable_area_height = 40

        self.assertEqual(
            _content_line_range(Info(), 500, before_line=600),
            (275, 325, 275, 10_315),
        )


class BoundedDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clients.update({"watching", "other"})
        watched_by_sid.update({"watching": {"session-1"}, "other": {"session-2"}})
        for sid in clients:
            pending_events[sid] = {}
            delivery_wakeups[sid] = asyncio.Event()

    async def asyncTearDown(self):
        clients.clear()
        watched_by_sid.clear()
        pending_events.clear()
        delivery_wakeups.clear()

    async def test_replaces_an_undelivered_snapshot_instead_of_growing(self):
        queue_client_event("watching", "content:session-1", "content", {"value": 1})
        queue_client_event("watching", "content:session-1", "content", {"value": 2})

        self.assertEqual(
            pending_events["watching"],
            {"content:session-1": ("content", {"value": 2})},
        )

    async def test_terminal_content_is_queued_only_for_its_watchers(self):
        queue_content_for_watchers("session-1", {"lines": ["latest"]})

        self.assertEqual(len(pending_events["watching"]), 1)
        self.assertEqual(pending_events["other"], {})


class InitialWatchSnapshotTest(unittest.IsolatedAsyncioTestCase):
    """A client that starts watching a pane another connection is already
    streaming must get an immediate content snapshot — not sit on WAITING FOR
    OUTPUT until the pane next changes.
    """

    def _frame(self, text):
        return {
            "lines": [[{"t": text}]], "fg": "#fff", "bg": "#000",
            "firstLine": 0, "availableFirstLine": 0, "terminalEnd": 1,
            "isLatest": True,
        }

    async def asyncSetUp(self):
        # A pane already being streamed for an existing connection, with a frame
        # cached by that stream.
        self._alive = asyncio.create_task(asyncio.Event().wait())  # never resolves
        stream_tasks["session-1"] = self._alive
        last_content["session-1"] = self._frame("hello")
        # The freshly-connected client.
        clients.add("late")
        pending_events["late"] = {}
        delivery_wakeups["late"] = asyncio.Event()

    async def asyncTearDown(self):
        self._alive.cancel()
        await asyncio.gather(self._alive, return_exceptions=True)
        for task in list(stream_tasks.values()):
            task.cancel()
        await asyncio.gather(*stream_tasks.values(), return_exceptions=True)
        clients.clear()
        watched_by_sid.clear()
        pending_events.clear()
        delivery_wakeups.clear()
        stream_tasks.clear()
        last_content.clear()

    async def test_late_watcher_is_seeded_from_the_running_stream(self):
        await on_watch("late", {"sessionIds": ["session-1"]})

        self.assertIn("content:session-1", pending_events["late"])
        event, payload = pending_events["late"]["content:session-1"]
        self.assertEqual(event, "content")
        self.assertEqual(payload["sessionId"], "session-1")
        self.assertEqual(payload["lines"], [[{"t": "hello"}]])

    async def test_unstreamed_pane_is_not_seeded_here(self):
        # A pane with no existing stream is left to stream_session's own initial
        # read (started by apply_watches), so on_watch must not pre-send it — that
        # would double up with the stream's first frame.
        await on_watch("late", {"sessionIds": ["session-2"]})
        await asyncio.sleep(0)  # let apply_watches' no-op stream task settle

        self.assertNotIn("content:session-2", pending_events["late"])


if __name__ == "__main__":
    unittest.main()
