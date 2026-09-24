import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
import socketio

import server.server as server_module
from server.auth import COOKIE_MAX_AGE, COOKIE_NAME
from server.test_terminal_lines import DWC_RIGHT, line as proto_backed_line
from server.server import (
    _DEFAULT_PALETTE,
    _apply_links,
    _autolink,
    _content_line_range,
    _line_runs,
    _link_ranges,
    clients,
    delivery_wakeups,
    last_content,
    on_close_pane,
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


class _URL:
    """iterm2.CellStyle.URL double: an OSC 8 hyperlink on a cell."""

    def __init__(self, url):
        self.url = url
        self.identifier = None


class _Style:
    def __init__(self, *, faint=False, url=None):
        self.fg_color = _DefaultColor()
        self.bg_color = _DefaultColor()
        self.bold = False
        self.faint = faint
        self.inverse = False
        self.url = _URL(url) if url else None


class _Line:
    """iterm2.LineContents double. ``linked`` maps a (start, end) slice of
    the text to the OSC 8 URL its cells carry; ``hard_eol`` False means
    iTerm2 wrapped this row onto the next one."""

    def __init__(self, text, faint_at=(), linked=None, hard_eol=True):
        self.text = text
        self.string = text
        self.hard_eol = hard_eol
        faint_at = set(faint_at)
        url_at = {}
        for (start, end), url in (linked or {}).items():
            for i in range(start, end):
                url_at[i] = url
        self.styles = [
            _Style(faint=i in faint_at, url=url_at.get(i))
            for i in range(len(text))
        ]

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

    def test_osc8_hyperlink_is_its_own_run(self):
        line = _Line("see docs now", linked={(4, 8): "https://example.org/docs"})
        self.assertEqual(
            _line_runs(line, _DEFAULT_PALETTE),
            [{"t": "see "}, {"t": "docs", "u": "https://example.org/docs"},
             {"t": " now"}],
        )

    def test_osc8_links_a_phone_cannot_open_stay_plain_text(self):
        line = _Line("notes.txt", linked={(0, 9): "file:///Users/alex/notes.txt"})
        self.assertEqual(_line_runs(line, _DEFAULT_PALETTE), [{"t": "notes.txt"}])

    def test_text_after_an_emoji_keeps_its_own_colour_and_end(self):
        # A line as iTerm2 sends it: the wide emoji is two UTF-16 units and
        # two cells, and the text after it is bold.
        line = proto_backed_line(
            ["│", " ", "📣", DWC_RIGHT, " ", "J", "i", "r", "a"],
            [({}, 2), ({"fgStandard": 1}, 2), ({"bold": True}, 5)])
        self.assertEqual(
            _line_runs(line, _DEFAULT_PALETTE),
            [{"t": "│ "}, {"t": "📣", "f": "#cd0000"},
             {"t": " Jira", "b": True}],
        )


class AutolinkTest(unittest.TestCase):
    def test_links_a_web_address_inside_a_styled_run(self):
        text = "see https://a.b/c now"
        self.assertEqual(
            _apply_links([{"t": text, "b": True}], _link_ranges(text)),
            [{"t": "see ", "b": True},
             {"t": "https://a.b/c", "b": True, "u": "https://a.b/c"},
             {"t": " now", "b": True}],
        )

    def test_trailing_punctuation_and_brackets_stay_outside_the_link(self):
        self.assertEqual(
            _link_ranges("(see https://x.y/z?q=1)."),
            [(5, 22, "https://x.y/z?q=1")],
        )
        self.assertEqual(_link_ranges("Visit HTTPS://Example.com, then"),
                         [(6, 25, "HTTPS://Example.com")])
        self.assertEqual(_link_ranges("no links: ftp://x.y or example.com"), [])

    def test_a_link_across_styled_runs_keeps_each_style(self):
        text = "https://a.b/c"
        self.assertEqual(
            _apply_links([{"t": "https://a.b", "f": "#ff0000"}, {"t": "/c", "d": True}],
                         _link_ranges(text)),
            [{"t": "https://a.b", "f": "#ff0000", "u": text},
             {"t": "/c", "d": True, "u": text}],
        )

    def test_keeps_the_cursor_where_it_was(self):
        text = "https://a.b/c"
        self.assertEqual(
            _apply_links([{"t": "https://a."}, {"t": "", "c": True}, {"t": "b/c"}],
                         _link_ranges(text)),
            [{"t": "https://a.", "u": text}, {"t": "", "c": True},
             {"t": "b/c", "u": text}],
        )

    def test_an_osc8_link_is_not_overridden_by_the_address_it_shows(self):
        runs = [{"t": "https://a.b/c", "u": "https://explicit.example/"}]
        self.assertEqual(_apply_links(runs, _link_ranges("https://a.b/c")), runs)

    def test_joins_soft_wrapped_rows_into_one_link(self):
        url = "https://example.com/very/long/path"
        lines = [_Line("open https://example.com/ver", hard_eol=False),
                 _Line("y/long/path now")]
        rendered = [_line_runs(line, _DEFAULT_PALETTE) for line in lines]
        _autolink(rendered, lines)
        self.assertEqual(rendered, [
            [{"t": "open "}, {"t": "https://example.com/ver", "u": url}],
            [{"t": "y/long/path", "u": url}, {"t": " now"}],
        ])

    def test_a_hard_newline_ends_the_address(self):
        lines = [_Line("https://a.b/c"), _Line("d")]
        rendered = [_line_runs(line, _DEFAULT_PALETTE) for line in lines]
        _autolink(rendered, lines)
        self.assertEqual(rendered, [
            [{"t": "https://a.b/c", "u": "https://a.b/c"}], [{"t": "d"}],
        ])


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


async def _fake_get_lines(session, first, count):
    """Stands in for terminal_lines.async_get_lines, which reads over RPC."""
    return await session.async_get_contents(first, count)


class _FakeApp:
    def __init__(self, session):
        self._session = session

    def get_session_by_id(self, session_id):
        return self._session if session_id == self._session.session_id else None


class _ClosableSession:
    """iterm2.Session double that records how it was asked to close."""

    def __init__(self, session_id="session-1"):
        self.session_id = session_id
        self.close_calls = []

    async def async_close(self, force=False):
        self.close_calls.append(force)


class ClosePaneTest(unittest.IsolatedAsyncioTestCase):
    """The pane map's close button is the only way to close a pane from the
    phone, and it has already asked the user before this handler runs."""

    async def test_closes_the_pane_the_phone_named(self):
        session = _ClosableSession()
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch.object(server_module, "push_state", AsyncMock()):
            await on_close_pane("sid", {"sessionId": "session-1"})

        # force=True: without it iTerm puts a confirmation alert on the Mac
        # for any pane with a running job, which nobody is there to answer
        # and which blocks iTerm's main thread — and the API with it.
        self.assertEqual(session.close_calls, [True])

    async def test_a_stale_id_closes_nothing(self):
        session = _ClosableSession()
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch.object(server_module, "push_state", AsyncMock()):
            await on_close_pane("sid", {"sessionId": "already-gone"})

        # Emphatically not a fallback to whatever iTerm has focused: that
        # would make a stale tap close some unrelated pane.
        self.assertEqual(session.close_calls, [])

    async def test_a_missing_id_closes_nothing(self):
        session = _ClosableSession()
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch.object(server_module, "push_state", AsyncMock()):
            await on_close_pane("sid", {})
            await on_close_pane("sid", None)

        self.assertEqual(session.close_calls, [])

    async def test_a_close_that_fails_is_survived(self):
        class _Stubborn(_ClosableSession):
            async def async_close(self, force=False):
                raise RuntimeError("iTerm said no")

        session = _Stubborn()
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch.object(server_module, "push_state", AsyncMock()) as push:
            await on_close_pane("sid", {"sessionId": "session-1"})

        # The phone still gets a fresh layout: it is waiting to be told what
        # the tab looks like now.
        push.assert_awaited_once()


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
             patch.object(server_module, "async_get_lines", _fake_get_lines), \
             patch("iterm2.rpc.async_start_transaction",
                   side_effect=AssertionError("must not open a transaction")):
            result = await read_content("session-1")

        self.assertIsNotNone(result)
        self.assertEqual(result["lines"], [[{"t": "hello"}]])

    async def test_cancellation_mid_read_never_opens_a_transaction(self):
        gate = asyncio.Event()  # never set: async_get_contents blocks forever
        session = _FakeSession([_Line("hello")], contents_gate=gate)
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch.object(server_module, "async_get_lines", _fake_get_lines), \
             patch("iterm2.rpc.async_start_transaction",
                   side_effect=AssertionError("must not open a transaction")):
            task = asyncio.create_task(read_content("session-1"))
            await asyncio.sleep(0)  # let it suspend inside async_get_contents
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class ReadContentLinksTest(unittest.IsolatedAsyncioTestCase):
    """read_content wires the link pass in: an address printed across a
    soft wrap reaches the client as one link on both rows."""

    def tearDown(self):
        palette_cache.clear()

    async def test_wrapped_address_is_one_link_across_rows(self):
        session = _FakeSession([
            _Line("open https://example.com/very/lo", hard_eol=False),
            _Line("ng/path now"),
        ])
        with patch.object(server_module, "itermapp", _FakeApp(session)), \
             patch.object(server_module, "async_get_lines", _fake_get_lines):
            result = await read_content("session-1")

        url = "https://example.com/very/long/path"
        self.assertEqual(result["lines"], [
            [{"t": "open "}, {"t": "https://example.com/very/lo", "u": url}],
            [{"t": "ng/path", "u": url}, {"t": " now"}],
        ])


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


class CookieCredentialConnectTest(unittest.IsolatedAsyncioTestCase):
    """A phone whose localStorage Safari has purged still carries the HttpOnly
    cookie from /auth; the Socket.IO handshake must accept it in place of the
    auth-payload key."""

    async def asyncSetUp(self):
        for entry in (
            patch.object(server_module, "shared_key", "the-key"),
            patch.object(server_module, "start_client_delivery", lambda sid: None),
            patch.object(server_module, "seed_client", AsyncMock()),
        ):
            entry.start()
            self.addCleanup(entry.stop)

    async def asyncTearDown(self):
        for task in list(server_module.seed_tasks.values()):
            await task
        server_module.seed_tasks.clear()
        server_module.clients.clear()

    async def test_cookie_alone_authenticates(self):
        await server_module.connect(
            "cookie-client", {"HTTP_COOKIE": f"other=1; {COOKIE_NAME}=the-key"}, None)
        self.assertIn("cookie-client", server_module.clients)

    async def test_auth_payload_key_still_authenticates(self):
        await server_module.connect("key-client", {}, {"key": "the-key"})
        self.assertIn("key-client", server_module.clients)

    async def test_wrong_cookie_and_no_key_is_refused(self):
        with self.assertRaises(socketio.exceptions.ConnectionRefusedError):
            await server_module.connect(
                "stranger", {"HTTP_COOKIE": f"{COOKIE_NAME}=wrong"}, None)
        self.assertNotIn("stranger", server_module.clients)

    async def test_wrong_key_is_not_rescued_by_a_wrong_cookie(self):
        with self.assertRaises(socketio.exceptions.ConnectionRefusedError):
            await server_module.connect(
                "stranger", {"HTTP_COOKIE": f"{COOKIE_NAME}=wrong"}, {"key": "wrong"})


class AuthCookieRouteTest(unittest.IsolatedAsyncioTestCase):
    """POST /auth turns a valid key (or a valid existing cookie) into a fresh
    long-lived HttpOnly cookie, and only for pages served by this machine."""

    async def asyncSetUp(self):
        entry = patch.object(server_module, "shared_key", "the-key")
        entry.start()
        self.addCleanup(entry.stop)
        self.client = TestClient(TestServer(server_module.create_app()))
        await self.client.start_server()
        # The test server listens on 127.0.0.1, so a page on another port of
        # that host is "local"; anything else is a foreign site.
        self.local_origin = "http://127.0.0.1:7292"

    async def asyncTearDown(self):
        await self.client.close()

    async def test_valid_key_from_a_local_page_is_issued_the_cookie(self):
        resp = await self.client.post(
            "/auth", json={"key": "the-key"}, headers={"Origin": self.local_origin})

        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], self.local_origin)
        self.assertEqual(resp.headers["Access-Control-Allow-Credentials"], "true")
        cookie = resp.cookies[COOKIE_NAME]
        self.assertEqual(cookie.value, "the-key")
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["max-age"], str(COOKIE_MAX_AGE))
        self.assertEqual(cookie["path"], "/")

    async def test_an_existing_valid_cookie_is_renewed_without_a_key(self):
        resp = await self.client.post(
            "/auth", headers={"Origin": self.local_origin,
                              "Cookie": f"{COOKIE_NAME}=the-key"})

        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.cookies[COOKIE_NAME].value, "the-key")

    async def test_wrong_key_gets_no_cookie(self):
        resp = await self.client.post(
            "/auth", json={"key": "wrong"}, headers={"Origin": self.local_origin})

        self.assertEqual(resp.status, 401)
        self.assertNotIn(COOKIE_NAME, resp.cookies)

    async def test_foreign_origin_is_refused_even_with_the_right_key(self):
        resp = await self.client.post(
            "/auth", json={"key": "the-key"}, headers={"Origin": "http://evil.example"})

        self.assertEqual(resp.status, 403)
        self.assertNotIn(COOKIE_NAME, resp.cookies)
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)

    async def test_preflight_allows_only_local_pages(self):
        allowed = await self.client.options(
            "/auth", headers={"Origin": self.local_origin,
                              "Access-Control-Request-Method": "POST"})
        self.assertEqual(allowed.status, 204)
        self.assertEqual(allowed.headers["Access-Control-Allow-Origin"], self.local_origin)
        self.assertEqual(allowed.headers["Access-Control-Allow-Credentials"], "true")
        self.assertIn("POST", allowed.headers["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", allowed.headers["Access-Control-Allow-Headers"])

        refused = await self.client.options(
            "/auth", headers={"Origin": "http://evil.example",
                              "Access-Control-Request-Method": "POST"})
        self.assertEqual(refused.status, 403)

    async def test_socket_io_handshake_refuses_a_foreign_origin(self):
        resp = await self.client.get(
            "/socket.io/?EIO=4&transport=polling",
            headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 400)

        local = await self.client.get(
            "/socket.io/?EIO=4&transport=polling",
            headers={"Origin": self.local_origin})
        self.assertEqual(local.status, 200)
        self.assertEqual(local.headers["Access-Control-Allow-Origin"], self.local_origin)
        self.assertEqual(local.headers["Access-Control-Allow-Credentials"], "true")


if __name__ == "__main__":
    unittest.main()
