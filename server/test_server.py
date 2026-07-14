import asyncio
import unittest

from server.server import (
    _DEFAULT_PALETTE,
    _content_line_range,
    _line_runs,
    clients,
    delivery_wakeups,
    pending_events,
    queue_client_event,
    queue_content_for_watchers,
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


if __name__ == "__main__":
    unittest.main()
