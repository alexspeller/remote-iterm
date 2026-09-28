"""The live tail of one pane, read from iTerm2 incrementally.

A watched pane is re-read every time its screen changes, and each read used to
fetch its whole live tail (250 lines) plus its visible screen again. That is
far more expensive than its byte count suggests: iTerm2 renders every API
response as protobuf text for its Script Console, on its main thread, before
sending it, and for styled lines that text is about seven times the wire size
— measured on this Mac, one 250-line read of a Claude Code pane is 71-92 KB on
the wire and 476-675 KB of text. At several frames a second that alone keeps
iTerm2's main thread busy, and every API client (including this server's own
keystrokes) waits behind it.

Most of that work is redundant. Rows that have scrolled off the screen into
scrollback are immutable: a program can only write to the screen. So between
two reads only the rows that were on screen last time, plus whatever has
scrolled in since, can differ, and those are the only rows re-read. The cached
remainder is reused.

Scrollback is immutable only until iTerm2 itself rewrites it. It reflows the
whole buffer when a pane's width changes, and clearing the scrollback removes
it. A changed grid size or screen height, or a buffer that shrank, is answered
with a full read. A clear that is refilled before the next read (`clear`, or
Claude Code redrawing everything) leaves the line counts where they were —
iTerm2 does not advance its overflow count for it — so each incremental read
also re-reads the VERIFY_ROWS kept rows nearest the new ones and compares them
with the cache: any difference, or an overlap too blank to prove anything,
means a full read. A full read also happens every FULL_REFRESH_SECONDS
regardless, so a change nothing here can see still lasts at most that long.
"""
import time
from dataclasses import dataclass

try:
    from .terminal_lines import async_get_lines_and_cursor
except ImportError:  # Running from the server directory.
    from terminal_lines import async_get_lines_and_cursor

FULL_REFRESH_SECONDS = 30.0
VERIFY_ROWS = 4


@dataclass
class TailFrame:
    """One read of a pane's tail, in iTerm2's absolute line numbering."""

    first: int                 # absolute line number of lines[0]
    lines: list                # terminal_lines.Line, oldest first
    cursor: tuple[int, int]    # (column, absolute line)
    available_first: int       # oldest line iTerm2 still retains
    terminal_end: int          # one past the last line


def _row_key(line):
    """What makes two reads of a row the same row: its whole proto (text and
    styles) where there is one, else its text."""
    proto = getattr(line, "proto", None)
    return proto if proto is not None else line.string


def _rows_confirm(read: list, cached: list) -> bool:
    """Whether rows read again match the cached ones, and say something:
    blank rows match whatever was cleared as well as what was there."""
    return (len(read) == len(cached)
            and any(line.string.strip() for line in read)
            and all(_row_key(a) == _row_key(b) for a, b in zip(read, cached)))


def _grid_key(session):
    size = getattr(session, "grid_size", None)
    if size is None:
        return None
    return (getattr(size, "width", None), getattr(size, "height", None))


class LiveTail:
    """The last ``capacity`` lines of one pane, kept current across reads."""

    def __init__(self, capacity: int,
                 full_refresh_seconds: float = FULL_REFRESH_SECONDS,
                 clock=time.monotonic):
        self.capacity = capacity
        self.full_refresh_seconds = full_refresh_seconds
        self.clock = clock
        self._first = 0
        self._lines: list = []
        self._screen_top = 0
        self._terminal_end = 0
        self._available_first = 0
        self._screen_height = None
        self._grid = None
        self._full_read_at = None
        # How many lines each read fetched, for the health log.
        self.lines_read = 0

    def _reusable_until(self, want_first: int, available_first: int,
                        terminal_end: int, screen_height: int, grid,
                        now: float) -> int:
        """The end of the cached rows still valid: [want_first, result)."""
        if (self._full_read_at is None
                or now - self._full_read_at >= self.full_refresh_seconds
                or grid != self._grid
                or screen_height != self._screen_height
                or terminal_end < self._terminal_end
                or available_first < self._available_first
                or self._first > want_first):
            return want_first
        cached_end = self._first + len(self._lines)
        # Rows that were on screen last time may have been rewritten before
        # they scrolled off, so only rows already in scrollback then are kept.
        return max(want_first, min(self._screen_top, cached_end))

    async def read(self, session) -> TailFrame:
        info = await session.async_get_line_info()
        available_first = info.overflow
        screen_height = info.mutable_area_height
        terminal_end = (available_first + info.scrollback_buffer_height
                        + screen_height)
        want_first = max(available_first, terminal_end - self.capacity)
        now = self.clock()
        # Captured once: a resize landing during the read below then differs
        # from what is stored, and the next read is a full one.
        grid = _grid_key(session)
        keep_until = self._reusable_until(
            want_first, available_first, terminal_end, screen_height, grid,
            now)

        fetched = 0
        if keep_until > want_first:
            check_from = max(want_first, keep_until - VERIFY_ROWS)
            fresh, cursor = await async_get_lines_and_cursor(
                session, check_from, terminal_end - check_from)
            fetched += len(fresh)
            overlap = keep_until - check_from
            if _rows_confirm(fresh[:overlap],
                             self._lines[check_from - self._first:
                                         keep_until - self._first]):
                fresh = fresh[overlap:]
            else:
                # The rows under the cache have been rewritten: trust none of it.
                keep_until = want_first
        if keep_until == want_first:
            fresh, cursor = await async_get_lines_and_cursor(
                session, want_first, terminal_end - want_first)
            fetched += len(fresh)

        kept = self._lines[want_first - self._first:keep_until - self._first] \
            if keep_until > want_first else []
        lines = kept + fresh
        self._first = want_first
        self._lines = lines
        self._screen_top = terminal_end - screen_height
        self._terminal_end = terminal_end
        self._available_first = available_first
        self._screen_height = screen_height
        self._grid = grid
        if keep_until == want_first:
            self._full_read_at = now
        if len(fresh) != terminal_end - keep_until:
            # A short read (the buffer changed between the two requests) may
            # not start where it was asked to; the next read starts afresh.
            self._full_read_at = None
        self.lines_read += fetched
        return TailFrame(first=want_first, lines=lines, cursor=cursor,
                         available_first=available_first,
                         terminal_end=terminal_end)
