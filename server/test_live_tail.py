import unittest
from types import SimpleNamespace
from unittest.mock import patch

import server.live_tail as live_tail
from server.live_tail import LiveTail

CAPACITY = 20


class _Pane:
    """A pane's buffer in iTerm2's absolute numbering: lines [overflow,
    overflow + len(rows)), of which the last ``screen`` rows are the screen,
    the only rows a program can write to."""

    def __init__(self, rows: int, screen: int = 4, width: int = 80):
        self.rows = [f"line {i}" for i in range(rows)]
        self.screen = screen
        self.overflow = 0
        self.grid_size = SimpleNamespace(width=width, height=screen)
        self.reads: list[tuple[int, int]] = []

    # --- what a program does to the pane ---
    def output(self, *texts: str) -> None:
        self.rows.extend(texts)

    def rewrite_screen_row(self, index: int, text: str) -> None:
        self.rows[len(self.rows) - self.screen + index] = text

    def tail(self, count: int) -> list[str]:
        return self.rows[-count:]

    # --- what iTerm2 answers ---
    async def async_get_line_info(self):
        return SimpleNamespace(
            overflow=self.overflow,
            scrollback_buffer_height=len(self.rows) - self.screen,
            mutable_area_height=self.screen)

    def get_lines(self, first: int, count: int):
        self.reads.append((first, count))
        start = first - self.overflow
        lines = [SimpleNamespace(string=text)
                 for text in self.rows[start:start + count]]
        return lines, (0, self.overflow + len(self.rows) - 1)


async def _fake_read(session, first, count):
    return session.get_lines(first, count)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LiveTailTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(live_tail, "async_get_lines_and_cursor", _fake_read)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = _Clock()
        self.tail = LiveTail(CAPACITY, clock=self.clock)

    async def _read_texts(self, pane):
        frame = await self.tail.read(pane)
        return frame, [line.string for line in frame.lines]

    async def test_first_read_fetches_the_whole_tail(self):
        pane = _Pane(rows=40)
        frame, texts = await self._read_texts(pane)
        self.assertEqual(texts, pane.tail(CAPACITY))
        self.assertEqual(frame.first, 20)
        self.assertEqual(frame.terminal_end, 40)
        self.assertEqual(pane.reads, [(20, 20)])

    async def test_later_reads_fetch_only_what_was_on_screen_and_what_arrived(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.output("a", "b", "c")
        pane.reads.clear()

        frame, texts = await self._read_texts(pane)

        # Rows 36-39 were the screen last time and 40-42 are new; 32-35 are
        # read again only to confirm the cache still matches. Rows 23-31,
        # already in scrollback, are not fetched.
        self.assertEqual(pane.reads, [(32, 11)])
        self.assertEqual(texts, pane.tail(CAPACITY))
        self.assertEqual(frame.first, 23)

    async def test_a_row_rewritten_on_screen_before_it_scrolled_off_is_read_again(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.rewrite_screen_row(0, "spinner done")  # row 36, still on screen
        pane.output("next")                         # row 36 scrolls off

        _, texts = await self._read_texts(pane)

        self.assertEqual(texts, pane.tail(CAPACITY))
        self.assertIn("spinner done", texts)

    async def test_an_unchanged_pane_rereads_only_its_screen(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.reads.clear()
        _, texts = await self._read_texts(pane)
        self.assertEqual(pane.reads, [(32, 8)])  # 4 to confirm, 4 of screen
        self.assertEqual(texts, pane.tail(CAPACITY))

    async def test_a_width_change_rereads_everything(self):
        # iTerm2 reflows the whole buffer when a pane's width changes, so
        # rows already in scrollback can change too.
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.grid_size = SimpleNamespace(width=60, height=4)
        pane.rows = [f"reflowed {i}" for i in range(44)]
        pane.reads.clear()

        _, texts = await self._read_texts(pane)

        self.assertEqual(pane.reads, [(24, 20)])
        self.assertEqual(texts, pane.tail(CAPACITY))

    async def test_a_screen_height_change_rereads_everything(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.screen = 6
        pane.reads.clear()
        await self.tail.read(pane)
        self.assertEqual(pane.reads, [(20, 20)])

    async def test_clearing_the_scrollback_rereads_everything(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.rows = ["after clear 1", "after clear 2", "p", "q"]
        pane.reads.clear()

        _, texts = await self._read_texts(pane)

        self.assertEqual(pane.reads, [(0, 4)])
        self.assertEqual(texts, pane.rows)

    async def test_everything_is_reread_periodically_whatever_happened(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        self.clock.now += live_tail.FULL_REFRESH_SECONDS
        pane.reads.clear()
        await self.tail.read(pane)
        self.assertEqual(pane.reads, [(20, 20)])

    async def test_output_longer_than_the_tail_is_one_full_read(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.output(*[f"burst {i}" for i in range(25)])
        pane.reads.clear()

        _, texts = await self._read_texts(pane)

        self.assertEqual(pane.reads, [(45, 20)])
        self.assertEqual(texts, pane.tail(CAPACITY))

    async def test_lines_dropped_off_the_top_of_a_limited_scrollback(self):
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        # A full scrollback discards its oldest rows as new ones arrive; the
        # absolute numbering of the rest does not change.
        pane.overflow = 2
        pane.rows = pane.rows[2:]
        pane.output("x", "y")
        pane.reads.clear()

        frame, texts = await self._read_texts(pane)

        self.assertEqual(pane.reads, [(32, 10)])
        self.assertEqual(texts, pane.tail(CAPACITY))
        self.assertEqual(frame.available_first, 2)

    async def test_the_cursor_comes_from_the_same_read(self):
        pane = _Pane(rows=40)
        frame = await self.tail.read(pane)
        self.assertEqual(frame.cursor, (0, 39))

    async def test_a_scrollback_cleared_and_refilled_between_reads_is_reread(self):
        # `clear`, or Claude Code redrawing everything: the scrollback is
        # emptied and filled again before the next read, and iTerm2's line
        # counts come back exactly where they were.
        pane = _Pane(rows=40)
        await self.tail.read(pane)
        pane.rows = [f"redrawn {i}" for i in range(40)]
        pane.reads.clear()

        _, texts = await self._read_texts(pane)

        self.assertEqual(pane.reads, [(32, 8), (20, 20)])
        self.assertEqual(texts, pane.tail(CAPACITY))

    async def test_a_blank_overlap_proves_nothing_so_everything_is_reread(self):
        pane = _Pane(rows=40)
        pane.rows[32:36] = ["", "", "", ""]
        await self.tail.read(pane)
        pane.rows = [""] * 36 + [f"after {i}" for i in range(4)]
        pane.reads.clear()

        _, texts = await self._read_texts(pane)

        self.assertEqual(pane.reads, [(32, 8), (20, 20)])
        self.assertEqual(texts, pane.tail(CAPACITY))

    async def test_a_short_read_makes_the_next_one_full(self):
        pane = _Pane(rows=40)
        real = pane.get_lines

        def short_once(first, count):
            pane.get_lines = real
            lines, cursor = real(first, count)
            return lines[:-1], cursor

        pane.get_lines = short_once
        await self.tail.read(pane)
        pane.reads.clear()
        await self.tail.read(pane)
        self.assertEqual(pane.reads, [(20, 20)])


if __name__ == "__main__":
    unittest.main()
