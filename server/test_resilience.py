"""How the server behaves while iTerm2 is slow or a phone's connection is not
to be trusted: input is never lost, reordered or typed twice, a stall is
reported rather than hidden, and a reconnecting phone draws at once."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import server.server as server_module
from server.server import (
    clients,
    content_cache,
    delivered_by_page,
    delivery_wakeups,
    input_streams,
    iterm_status,
    last_content,
    on_execute,
    on_get_content,
    on_hello,
    on_send_keys,
    on_split_pane,
    on_watch,
    palette_cache,
    pending_events,
    stream_tasks,
    watched_by_sid,
)


async def _spin_until(predicate, max_iterations=20_000):
    for _ in range(max_iterations):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


class _Pane:
    """iterm2.Session double that records what was typed into it. With a
    ``gate``, each send waits for it first, the way a send waits out an
    iTerm2 stall."""

    def __init__(self, session_id, gate=None):
        self.session_id = session_id
        self.typed: list[str] = []
        self.gate = gate

    async def async_send_text(self, text):
        if self.gate is not None:
            await self.gate.wait()
        self.typed.append(text)


class _App:
    """iterm2.App double. ``focused`` is the pane the Mac has in front."""

    def __init__(self, *panes, focused=None):
        self.panes = {pane.session_id: pane for pane in panes}
        focused = focused or (panes[0] if panes else None)
        tab = SimpleNamespace(current_session=focused)
        self.current_window = SimpleNamespace(current_tab=tab)

    def get_session_by_id(self, session_id):
        return self.panes.get(session_id)


def _connect(*sids):
    for sid in sids:
        clients.add(sid)
        pending_events[sid] = {}
        delivery_wakeups[sid] = asyncio.Event()


async def _reset():
    for stream in list(input_streams.values()):
        await stream.stop()
    input_streams.clear()
    delivered_by_page.clear()
    for task in list(stream_tasks.values()):
        task.cancel()
    await asyncio.gather(*stream_tasks.values(), return_exceptions=True)
    stream_tasks.clear()
    last_content.clear()
    content_cache.clear()
    palette_cache.clear()
    clients.clear()
    watched_by_sid.clear()
    pending_events.clear()
    delivery_wakeups.clear()
    server_module.browser_clients.clear()
    server_module.sid_by_page.clear()
    iterm_status.update(ok=True, stalledSince=None, latencyMs=None)


def _delivered(sid, page="page-1"):
    event = pending_events[sid].get(f"inputDelivered:{page}")
    return event[1] if event else None


class SequencedInputTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _connect("phone")

    async def asyncTearDown(self):
        await _reset()

    async def _keys(self, sid, seq, keys, session="pane-1", page="page-1"):
        return await on_send_keys(sid, {
            "sessionId": session, "keys": keys, "page": page, "seq": seq})

    async def test_input_reaches_iterm2_in_the_order_it_was_typed(self):
        gate = asyncio.Event()
        pane = _Pane("pane-1", gate)
        with patch.object(server_module, "itermapp", _App(pane)):
            for seq, key in enumerate("abc", start=1):
                await self._keys("phone", seq, key)
            gate.set()
            await _spin_until(lambda: len(pane.typed) == 3)
        self.assertEqual(pane.typed, ["a", "b", "c"])

    async def test_it_is_acknowledged_at_once_even_while_iterm2_is_stalled(self):
        pane = _Pane("pane-1", asyncio.Event())  # never answers
        with patch.object(server_module, "itermapp", _App(pane)):
            ack = await asyncio.wait_for(self._keys("phone", 1, "x"), timeout=1)
        self.assertEqual(ack, {"ok": True, "instance": server_module.SERVER_INSTANCE,
                               "delivered": 0, "failed": []})
        self.assertEqual(pane.typed, [])

    async def test_input_resent_after_a_reconnect_is_typed_only_once(self):
        # The page sent it, its socket died before the acknowledgement came
        # back, and it sends it again on the next connection.
        _connect("phone-again")
        pane = _Pane("pane-1")
        with patch.object(server_module, "itermapp", _App(pane)):
            await self._keys("phone", 1, "rm -rf build\r")
            await self._keys("phone-again", 1, "rm -rf build\r")
            await self._keys("phone-again", 2, "ls\r")
            await _spin_until(lambda: input_streams["page-1"].delivered == 2)
        self.assertEqual(pane.typed, ["rm -rf build\r", "ls\r"])

    async def test_delivery_is_reported_to_the_page(self):
        pane = _Pane("pane-1")
        with patch.object(server_module, "itermapp", _App(pane)):
            await on_execute("phone", {"sessionId": "pane-1", "command": "make",
                                       "page": "page-1", "seq": 1})
            await _spin_until(lambda: _delivered("phone") is not None)
        self.assertEqual(pane.typed, ["make\r"])
        self.assertEqual(_delivered("phone"),
                         {"page": "page-1", "upto": 1, "failed": []})

    async def test_a_page_that_reconnects_hears_what_was_delivered_meanwhile(self):
        gate = asyncio.Event()
        pane = _Pane("pane-1", gate)
        with patch.object(server_module, "itermapp", _App(pane)):
            await self._keys("phone", 1, "y")
            # The phone's socket drops while iTerm2 is still busy.
            clients.discard("phone")
            gate.set()
            await _spin_until(lambda: input_streams["page-1"].delivered == 1)
            _connect("phone-again")
            await on_hello("phone-again", {"page": "page-1"})
        self.assertEqual(_delivered("phone-again")["upto"], 1)

    async def test_input_for_a_closed_pane_is_reported_and_typed_nowhere(self):
        focused = _Pane("pane-1")
        with patch.object(server_module, "itermapp", _App(focused)):
            await on_execute("phone", {"sessionId": "gone", "command": "deploy",
                                       "page": "page-1", "seq": 1})
            await _spin_until(lambda: _delivered("phone") is not None)
        # Not a fallback to whatever the Mac has in front.
        self.assertEqual(focused.typed, [])
        self.assertEqual(_delivered("phone")["failed"],
                         [{"seq": 1, "reason": "that pane has closed"}])

    async def test_an_acknowledgement_names_the_failures_it_counts(self):
        # `delivered` counts failed inputs too, and an ack can reach the page
        # before the delivery report does: without the failures in it, the
        # page would take a failed input for a delivered one.
        with patch.object(server_module, "itermapp", _App(_Pane("pane-1"))):
            await on_execute("phone", {"sessionId": "gone", "command": "deploy",
                                       "page": "page-1", "seq": 1})
            await _spin_until(lambda: input_streams["page-1"].delivered == 1)
            ack = await self._keys("phone", 2, "q")
        self.assertEqual(ack["delivered"], 1)
        self.assertEqual(ack["failed"], [{"seq": 1, "reason": "that pane has closed"}])

    async def test_reports_go_to_the_connection_the_page_last_introduced(self):
        # A late packet on the page's old, half-dead connection must not
        # divert its delivery reports there.
        _connect("old", "new")
        pane = _Pane("pane-1", asyncio.Event())
        with patch.object(server_module, "itermapp", _App(pane)):
            await on_hello("old", {"page": "page-1"})
            await on_hello("new", {"page": "page-1"})
            await self._keys("old", 1, "x")
        self.assertEqual(input_streams["page-1"].sid, "new")

    async def test_a_broadcast_some_panes_took_is_reported_as_such(self):
        class _Refuses(_Pane):
            async def async_send_text(self, text):
                raise RuntimeError("no")

        good, bad = _Pane("pane-1"), _Refuses("pane-2")
        with patch.object(server_module, "itermapp", _App(good, bad)), \
             patch.object(server_module, "push_state", AsyncMock()):
            await server_module.on_broadcast("phone", {
                "command": "make", "sessionIds": ["pane-1", "pane-2", "gone"],
                "page": "page-1", "seq": 1})
            await _spin_until(lambda: _delivered("phone") is not None)
        self.assertEqual(good.typed, ["make\r"])
        self.assertEqual(_delivered("phone")["failed"],
                         [{"seq": 1, "reason": "2 of 3 panes did not take it"}])

    async def test_a_page_keeps_its_place_after_its_idle_stream_is_dropped(self):
        pane = _Pane("pane-1")
        with patch.object(server_module, "itermapp", _App(pane)), \
             patch.object(server_module, "INPUT_STREAM_IDLE_SECONDS", -1):
            await self._keys("phone", 1, "a")
            await _spin_until(lambda: input_streams["page-1"].delivered == 1)
            await server_module.evict_idle_input_streams()
            self.assertNotIn("page-1", input_streams)
            await self._keys("phone", 1, "a")   # a late resend
            await self._keys("phone", 2, "b")
            await _spin_until(lambda: input_streams["page-1"].delivered == 2)
        self.assertEqual(pane.typed, ["a", "b"])


class UnsequencedInputTest(unittest.IsolatedAsyncioTestCase):
    """Programmatic clients (aveiro) send without a page or sequence."""

    async def asyncTearDown(self):
        await _reset()

    async def test_is_typed_directly(self):
        pane = _Pane("pane-1")
        with patch.object(server_module, "itermapp", _App(pane)):
            ack = await on_send_keys("aveiro", {"sessionId": "pane-1", "keys": "q"})
        self.assertIsNone(ack)
        self.assertEqual(pane.typed, ["q"])

    async def test_a_stale_pane_id_never_types_into_the_focused_pane(self):
        focused = _Pane("pane-1")
        with patch.object(server_module, "itermapp", _App(focused)):
            await on_send_keys("aveiro", {"sessionId": "closed-pane", "keys": "y\r"})
            await on_execute("aveiro", {"sessionId": "closed-pane", "command": "y"})
        self.assertEqual(focused.typed, [])


class FreshContentTest(unittest.IsolatedAsyncioTestCase):
    """getContent is how a programmatic client (aveiro) reads a pane before
    typing into it, so its answer is always a fresh read."""

    async def asyncSetUp(self):
        _connect("aveiro")

    async def asyncTearDown(self):
        await _reset()

    async def test_is_never_answered_from_a_cache(self):
        content_cache["pane-1"] = _frame("an old read")
        with patch.object(server_module, "read_content",
                          AsyncMock(return_value=_frame("now"))) as read:
            await on_get_content("aveiro", {"sessionId": "pane-1"})
        read.assert_awaited_once()
        self.assertEqual(pending_events["aveiro"]["content:pane-1"][1]["lines"],
                         [[{"t": "now"}]])


class ItermStallReportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _connect("phone")
        for name, value in (("STALL_AFTER", 0.01), ("WATCHDOG_INTERVAL", 0),
                            ("WATCHDOG_INTERVAL_BROWSER", 0),
                            ("WATCHDOG_RETRY_INTERVAL", 0)):
            patcher = patch.object(server_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await _reset()

    async def test_a_probe_left_unanswered_is_reported_and_so_is_its_answer(self):
        answer = asyncio.Event()
        stop = asyncio.Event()

        async def probe():
            await answer.wait()

        with patch.object(server_module, "_probe_iterm", side_effect=probe):
            task = asyncio.create_task(server_module.connection_watchdog(stop))
            await _spin_until(lambda: not iterm_status["ok"], 200_000)
            stalled = pending_events["phone"]["itermStatus"][1]
            self.assertFalse(stalled["ok"])
            self.assertIsInstance(stalled["stalledSince"], int)

            answer.set()
            await _spin_until(lambda: iterm_status["ok"], 200_000)
            recovered = pending_events["phone"]["itermStatus"][1]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(recovered["ok"])
        self.assertIsNone(recovered["stalledSince"])
        self.assertFalse(stop.is_set())

    async def test_a_probe_that_fails_at_once_is_not_a_stall(self):
        stop = asyncio.Event()
        probes = 0

        async def probe():
            nonlocal probes
            probes += 1
            raise asyncio.TimeoutError()

        live = SimpleNamespace(websocket=SimpleNamespace(closed=False))
        with patch.object(server_module, "_probe_iterm", side_effect=probe), \
             patch.object(server_module, "connection", live):
            task = asyncio.create_task(server_module.connection_watchdog(stop))
            await _spin_until(lambda: probes >= 3, 200_000)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(iterm_status["ok"])


def _frame(text, first=0):
    return {"lines": [[{"t": text}]], "fg": "#fff", "bg": "#000",
            "firstLine": first, "availableFirstLine": 0,
            "terminalEnd": first + 1, "isLatest": True}


class InstantDrawTest(unittest.IsolatedAsyncioTestCase):
    """What a phone that has just (re)connected can be given without waiting
    for iTerm2."""

    async def asyncSetUp(self):
        _connect("phone")

    async def asyncTearDown(self):
        await _reset()
        server_module._last_state = None

    async def test_watching_a_pane_draws_its_last_frame_even_unstreamed(self):
        content_cache["pane-2"] = _frame("from before the reconnect")
        await on_watch("phone", {"sessionIds": ["pane-2"]})
        event, payload = pending_events["phone"]["content:pane-2"]
        self.assertEqual(payload["lines"], [[{"t": "from before the reconnect"}]])
        # Marked, so a page already holding newer content keeps it.
        self.assertTrue(payload["cached"])

    async def test_a_new_client_gets_a_fresh_state_even_when_nothing_changed(self):
        state = [{"id": "w1", "tabs": []}]
        server_module._last_pushed_state = server_module.json.dumps(state, sort_keys=True)
        with patch.object(server_module, "build_state", AsyncMock(return_value=state)):
            await server_module.push_state(new_client="phone")
        self.assertEqual(pending_events["phone"]["state"], ("state", state))
        server_module._last_pushed_state = None

    async def test_a_new_client_gets_the_last_state_before_iterm2_answers(self):
        server_module._last_state = [{"id": "w1", "tabs": []}]
        emitted = []

        async def emit(event, data=None, to=None):
            emitted.append(event)
            if event == "state" and data is not server_module._last_state:
                raise AssertionError("a fresh state before the cached one")

        async def stalled_build_state():
            await asyncio.Event().wait()

        with patch.object(server_module.sio, "emit", side_effect=emit), \
             patch.object(server_module, "build_state", stalled_build_state), \
             patch.object(server_module, "itermapp", None):
            seed = asyncio.create_task(server_module.seed_client("phone"))
            await _spin_until(lambda: "state" in emitted)
            seed.cancel()
            await asyncio.gather(seed, return_exceptions=True)
        self.assertEqual(emitted[:2], ["serverInfo", "itermStatus"])
        self.assertIn("state", emitted)

    async def test_closed_panes_are_forgotten(self):
        content_cache["open"] = _frame("x")
        content_cache["closed"] = _frame("y")
        palette_cache["open"] = {}
        palette_cache["closed"] = {}
        state = [{"id": "w1", "tabs": [{"sessions": [{"id": "open"}]}]}]
        with patch.object(server_module, "build_state", AsyncMock(return_value=state)):
            await server_module.push_state()
        self.assertEqual(list(content_cache), ["open"])
        self.assertEqual(list(palette_cache), ["open"])

    async def test_the_frame_cache_is_bounded(self):
        with patch.object(server_module, "CONTENT_CACHE_SIZE", 3):
            for i in range(5):
                server_module.remember_content(f"pane-{i}", _frame(str(i)))
        self.assertEqual(list(content_cache), ["pane-2", "pane-3", "pane-4"])


class StreamTeardownTest(unittest.IsolatedAsyncioTestCase):
    """A stream's teardown unsubscribes over an RPC, which during an iTerm2
    stall takes as long as the stall."""

    async def asyncSetUp(self):
        _connect("phone", "other-phone")
        self.release = asyncio.Event()
        self.addAsyncCleanup(self._release)

    async def _release(self):
        self.release.set()
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        await _reset()

    def _stream_with_a_slow_teardown(self):
        release = self.release

        async def stream():
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()
        return asyncio.create_task(stream())

    async def test_one_clients_pane_switch_does_not_hold_up_anothers(self):
        stream_tasks["pane-1"] = self._stream_with_a_slow_teardown()
        watched_by_sid["phone"] = {"pane-1"}
        await asyncio.sleep(0)
        with patch.object(server_module, "stream_session", AsyncMock()):
            # phone moves off pane-1, whose stream then takes ages to end...
            await asyncio.wait_for(on_watch("phone", {"sessionIds": ["pane-2"]}), 1)
            # ...and another phone's watch is not kept waiting behind it.
            await asyncio.wait_for(
                on_watch("other-phone", {"sessionIds": ["pane-3"]}), 1)
        self.assertNotIn("pane-1", stream_tasks)
        self.assertIn("pane-3", stream_tasks)

    async def test_an_ending_stream_leaves_its_successors_frame_alone(self):
        release = self.release

        class _Streamer:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                await release.wait()   # the unsubscribe, stalled

            async def async_get(self):
                await asyncio.Event().wait()

        session = SimpleNamespace(session_id="pane-1",
                                  get_screen_streamer=lambda want_contents: _Streamer())
        app = SimpleNamespace(get_session_by_id=lambda sid: session)
        watched_by_sid["phone"] = {"pane-1"}
        with patch.object(server_module, "itermapp", app), \
             patch.object(server_module, "_read_live_frame",
                          AsyncMock(return_value=_frame("old stream"))):
            old = asyncio.create_task(server_module.stream_session("pane-1"))
            stream_tasks["pane-1"] = old
            await _spin_until(lambda: "pane-1" in last_content)
            await server_module.stop_all_streams()

            # A new stream for the same pane starts while the old one is
            # still unsubscribing.
            new = asyncio.create_task(asyncio.Event().wait())
            stream_tasks["pane-1"] = new
            last_content["pane-1"] = _frame("new stream")
            release.set()
            await asyncio.gather(old, return_exceptions=True)
        self.assertEqual(last_content["pane-1"]["lines"], [[{"t": "new stream"}]])


class SplitPaneTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await _reset()

    async def test_answers_with_the_new_pane(self):
        class _Splittable(_Pane):
            async def async_split_pane(self, vertical):
                self.vertical = vertical
                return SimpleNamespace(session_id="new-pane")

        pane = _Splittable("pane-1")
        with patch.object(server_module, "itermapp", _App(pane)), \
             patch.object(server_module, "push_state", AsyncMock()):
            ack = await on_split_pane("phone", {"sessionId": "pane-1", "vertical": False})
        self.assertEqual(ack, {"sessionId": "new-pane"})
        self.assertFalse(pane.vertical)

    async def test_says_why_when_it_cannot(self):
        class _TooSmall(_Pane):
            async def async_split_pane(self, vertical):
                raise RuntimeError("pane too small")

        with patch.object(server_module, "itermapp", _App(_TooSmall("pane-1"))), \
             patch.object(server_module, "push_state", AsyncMock()):
            ack = await on_split_pane("phone", {"sessionId": "pane-1", "vertical": True})
        self.assertIn("pane too small", ack["error"])

    async def test_never_splits_a_different_pane_than_the_one_named(self):
        focused = _Pane("pane-1")
        with patch.object(server_module, "itermapp", _App(focused)), \
             patch.object(server_module, "push_state", AsyncMock()):
            ack = await on_split_pane("phone", {"sessionId": "gone", "vertical": True})
        self.assertEqual(ack, {"error": "that pane has closed"})


if __name__ == "__main__":
    unittest.main()
