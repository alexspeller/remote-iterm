"""Read iTerm2 buffer lines faithfully: correct per-cell text, and ANSI colour.

Two quirks of iTerm2's buffer API make the obvious reading lossy:

* A cell a program moved the cursor over without writing to (Claude Code / Ink
  pads with cursor-forward instead of spaces) holds character 0, and the API
  returns it as a literal NUL. Written back to a terminal those cells vanish,
  running the words either side of them together.
* iTerm2 reports how much text each cell holds in UTF-16 code units (it builds
  the line from ``unichar``\\ s), but ``LineContents.string_at`` slices the
  decoded Python string, which counts code points, with those numbers. Past any
  character outside the Basic Multilingual Plane (most emoji) every cell then
  reads its neighbour's text, and the line's final characters are dropped.
"""
from itertools import accumulate, chain, repeat

import iterm2
import iterm2.rpc
import iterm2.util


class Line(iterm2.screen.LineContents):
    """A buffer line whose per-cell text lines up with its per-cell styles."""

    def __init__(self, proto):
        super().__init__(proto)
        self._utf16 = proto.text.encode("utf-16-le")
        # _offsets[x] is the UTF-16 unit where cell x starts; one entry past
        # the last cell marks the end of the line.
        self._offsets = list(accumulate(
            chain.from_iterable(repeat(c.num_code_points, c.repeats)
                                for c in proto.code_points_per_cell),
            initial=0))
        self._style_runs = [(iterm2.screen.CellStyle(s), s.repeats)
                            for s in proto.style]

    def _text(self, start_cell: int, end_cell: int) -> str:
        last = len(self._offsets) - 1
        lo = self._offsets[min(start_cell, last)]
        hi = self._offsets[min(end_cell, last)]
        return self._utf16[2 * lo:2 * hi].decode("utf-16-le", "replace")

    def string_at(self, x: int) -> str:
        return self._text(x, x + 1)

    def runs(self) -> list:
        """(text, CellStyle) for each run of identically-styled cells.

        A line read without styles comes back as one unstyled run.
        """
        if not self._style_runs:
            return [(self._text(0, len(self._offsets) - 1), None)]
        out = []
        cell = 0
        for style, count in self._style_runs:
            out.append((self._text(cell, cell + count), style))
            cell += count
        return out


async def async_get_lines(session, first_line: int, count: int) -> list:
    """``session.async_get_contents``, returning correctly-aligned ``Line``\\ s."""
    coord_range = iterm2.util.WindowedCoordRange(
        iterm2.util.CoordRange(
            iterm2.util.Point(0, first_line),
            iterm2.util.Point(0, first_line + count)))
    response = await iterm2.rpc.async_get_screen_contents(
        connection=session.connection,
        session=session.session_id,
        windowed_coord_range=coord_range,
        style=True)
    buffer = response.get_buffer_response
    # pylint: disable=no-member
    ok = iterm2.api_pb2.GetBufferResponse.Status.Value("OK")
    if buffer.status != ok:
        raise iterm2.rpc.RPCException(
            iterm2.api_pb2.GetBufferResponse.Status.Name(buffer.status))
    return [Line(proto) for proto in buffer.contents]


def plain_text(line) -> str:
    """The line as text: NUL cells become the blanks they look like."""
    return line.string.replace("\x00", " ").rstrip()


# --- ANSI (SGR) rendering ------------------------------------------------------
#
# Colours are written back in the form the program chose them — a palette index
# stays an index, so the pane's own theme colours it exactly as before — rather
# than resolved to RGB the way the phone client needs them.

def _color_params(color, base: int) -> tuple:
    """SGR parameters for a foreground (base 30) or background (base 40)."""
    if color is None:
        return ()
    if color.is_standard:
        index = color.standard
        if index < 8:
            return (str(base + index),)
        if index < 16:
            return (str(base + 60 + index - 8),)
        return (f"{base + 8};5;{index}",)
    if color.is_rgb:
        c = color.rgb
        return (f"{base + 8};2;{c.red};{c.green};{c.blue}",)
    # The default colours, and image placements (which reuse the colour fields
    # for coordinates), are both what a reset already gives.
    return ()


_ATTRIBUTES = (("bold", "1"), ("faint", "2"), ("italic", "3"),
               ("underline", "4"), ("blink", "5"), ("inverse", "7"),
               ("invisible", "8"), ("strikethrough", "9"))


def sgr_params(style) -> tuple:
    """The SGR parameters that reproduce ``style`` from a reset state."""
    if style is None:
        return ()
    params = [code for name, code in _ATTRIBUTES if getattr(style, name)]
    params.extend(_color_params(style.fg_color, 30))
    params.extend(_color_params(style.bg_color, 40))
    # An underline colour only shows on underlined text, and terminals that
    # don't know SGR 58 misread its parameters as resets, so it is written only
    # where it has an effect.
    underline = style.underline_color
    if style.underline and underline is not None:
        params.append(f"58;2;{underline.red};{underline.green};{underline.blue}")
    return tuple(params)


def _blank_is_visible(style) -> bool:
    """Whether a space in this style shows (a coloured bar, a cursor block)."""
    if style is None:
        return False
    bg = style.bg_color
    has_bg = bg is not None and (bg.is_standard or bg.is_rgb)
    return (has_bg or bool(style.inverse) or bool(style.underline)
            or bool(style.strikethrough))


def ansi_text(line) -> str:
    """The line with its colours and attributes as SGR escape sequences.

    Trailing blanks are trimmed only where they would be invisible, so a
    full-width highlighted row keeps its highlight. The line always ends in
    the default style, so nothing bleeds into the next line (or, through
    background-colour erase, into the row a newline scrolls in).
    """
    pieces = []  # [text, params, blank_is_visible]
    for text, style in line.runs():
        if not text:
            continue
        text = text.replace("\x00", " ")
        params = sgr_params(style)
        if pieces and pieces[-1][1] == params:
            pieces[-1][0] += text
        else:
            pieces.append([text, params, _blank_is_visible(style)])
    while pieces and not pieces[-1][2]:
        trimmed = pieces[-1][0].rstrip()
        if trimmed:
            pieces[-1][0] = trimmed
            break
        pieces.pop()
    out = []
    current = ()
    for text, params, _ in pieces:
        if params != current:
            out.append("\x1b[" + ";".join(("0",) + params) + "m")
            current = params
        out.append(text)
    if current:
        out.append("\x1b[0m")
    return "".join(out)
