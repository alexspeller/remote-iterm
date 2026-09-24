import unittest

from iterm2 import api_pb2

from server.terminal_lines import Line, ansi_text, plain_text

# The right half of a double-width character: iTerm2 reports the cell with no
# text of its own.
DWC_RIGHT = ""


def proto_line(cells, styles=None):
    """A buffer line built the way iTerm2's ``stringForLine:`` builds one.

    ``cells`` is each cell's text; iTerm2 measures it in UTF-16 code units.
    ``styles`` is a list of (CellStyle fields, number of cells) runs covering
    the line; omitted, every cell has the default style. A run given no
    foreground or background gets the default one, which iTerm2 always sends
    as an alternate colour rather than leaving it out.
    """
    proto = api_pb2.LineContents()
    proto.text = "".join(cells)
    for cell in cells:
        units = len(cell.encode("utf-16-le")) // 2
        runs = proto.code_points_per_cell
        if runs and runs[-1].num_code_points == units:
            runs[-1].repeats += 1
        else:
            runs.add(num_code_points=units, repeats=1)
    for fields, count in styles or [({}, len(cells))]:
        style = proto.style.add(repeats=count)
        for side in ("fg", "bg"):
            if not any(name.startswith(side) for name in fields):
                setattr(style, side + "Alternate", api_pb2.DEFAULT)
        for name, value in fields.items():
            if name in ("fgRgb", "bgRgb", "underlineColor"):
                getattr(style, name).CopyFrom(api_pb2.RGBColor(
                    red=value[0], green=value[1], blue=value[2]))
            else:
                setattr(style, name, value)
    return proto


def line(cells, styles=None):
    return Line(proto_line(cells, styles))


def chars(text):
    return list(text)


class CellAlignmentTest(unittest.TestCase):
    # "📣" is outside the Basic Multilingual Plane: two UTF-16 units, one
    # Python character, and two cells wide.
    CELLS = ["│", " ", "📣", DWC_RIGHT, " ", "J", "i", "r", "a"]

    def test_each_cell_reads_its_own_text_past_an_astral_emoji(self):
        row = line(self.CELLS)
        self.assertEqual([row.string_at(x) for x in range(len(self.CELLS))],
                         self.CELLS)

    def test_runs_split_the_text_exactly_at_style_boundaries(self):
        row = line(self.CELLS, [({}, 2), ({"fgStandard": 1}, 2),
                                ({"bold": True}, 5)])
        self.assertEqual([text for text, _ in row.runs()],
                         ["│ ", "📣", " Jira"])

    def test_a_line_read_without_styles_is_one_run(self):
        proto = proto_line(chars("plain"))
        del proto.style[:]
        self.assertEqual([(text, style) for text, style in Line(proto).runs()],
                         [("plain", None)])

    def test_text_after_several_emoji_is_not_truncated(self):
        cells = ["💬", DWC_RIGHT, " ", "1", " ", "👥", DWC_RIGHT, " ", "0"]
        row = line(cells, [({"fgStandard": 2}, len(cells))])
        self.assertEqual(ansi_text(row), "\x1b[0;32m💬 1 👥 0\x1b[0m")


class PlainTextTest(unittest.TestCase):
    def test_cells_skipped_by_the_cursor_read_as_spaces(self):
        # Claude Code pads with cursor-forward, leaving NUL cells between words.
        cells = chars("Confirmed") + ["\x00"] + chars("genuinely") + ["\x00"] * 3
        self.assertEqual(plain_text(line(cells)), "Confirmed genuinely")


class AnsiTextTest(unittest.TestCase):
    def test_default_styled_text_has_no_escapes(self):
        self.assertEqual(ansi_text(line(chars("hello"))), "hello")

    def test_palette_colours_stay_palette_indices(self):
        cases = [
            ({"fgStandard": 1}, "31"),
            ({"fgStandard": 9}, "91"),
            ({"fgStandard": 208}, "38;5;208"),
            ({"bgStandard": 4}, "44"),
            ({"bgStandard": 12}, "104"),
            ({"bgStandard": 236}, "48;5;236"),
        ]
        for fields, params in cases:
            with self.subTest(fields=fields):
                self.assertEqual(ansi_text(line(["x"], [(fields, 1)])),
                                 f"\x1b[0;{params}mx\x1b[0m")

    def test_true_colour_is_kept_exactly(self):
        row = line(["x"], [({"fgRgb": (215, 119, 87),
                             "bgRgb": (1, 2, 3)}, 1)])
        self.assertEqual(ansi_text(row),
                         "\x1b[0;38;2;215;119;87;48;2;1;2;3mx\x1b[0m")

    def test_attributes(self):
        row = line(["x"], [({"bold": True, "faint": True, "italic": True,
                             "underline": True, "blink": True,
                             "inverse": True, "invisible": True,
                             "strikethrough": True,
                             "underlineColor": (9, 8, 7)}, 1)])
        self.assertEqual(ansi_text(row),
                         "\x1b[0;1;2;3;4;5;7;8;9;58;2;9;8;7mx\x1b[0m")

    def test_an_underline_colour_is_written_only_for_underlined_text(self):
        row = line(chars("ab"), [({"fgStandard": 1,
                                   "underlineColor": (0, 0, 0)}, 2)])
        self.assertEqual(ansi_text(row), "\x1b[0;31mab\x1b[0m")

    def test_default_colours_need_no_parameters(self):
        row = line(["x"], [({"fgAlternate": api_pb2.DEFAULT,
                             "bgAlternate": api_pb2.DEFAULT}, 1)])
        self.assertEqual(ansi_text(row), "x")

    def test_returning_to_the_default_style_resets(self):
        row = line(chars("red plain"), [({"fgStandard": 1}, 3), ({}, 6)])
        self.assertEqual(ansi_text(row), "\x1b[0;31mred\x1b[0m plain")

    def test_runs_that_render_identically_are_merged(self):
        # iTerm2 splits runs on things SGR cannot express (here a block id).
        row = line(chars("abcd"), [({"fgStandard": 3, "blockID": "one"}, 2),
                                   ({"fgStandard": 3, "blockID": "two"}, 2)])
        self.assertEqual(ansi_text(row), "\x1b[0;33mabcd\x1b[0m")

    def test_skipped_cells_inside_a_coloured_run_become_spaces(self):
        row = line(["a", "\x00", "b"], [({"fgStandard": 2}, 3)])
        self.assertEqual(ansi_text(row), "\x1b[0;32ma b\x1b[0m")

    def test_invisible_trailing_blanks_are_trimmed(self):
        row = line(chars("ab") + [" ", "\x00", " "],
                   [({"fgStandard": 1}, 2), ({}, 3)])
        self.assertEqual(ansi_text(row), "\x1b[0;31mab\x1b[0m")

    def test_a_highlighted_row_keeps_its_trailing_highlight(self):
        row = line(chars("ab   "), [({}, 2), ({"bgStandard": 4}, 3)])
        self.assertEqual(ansi_text(row), "ab\x1b[0;44m   \x1b[0m")

    def test_an_inverse_blank_cursor_block_is_kept(self):
        row = line(chars("> "), [({}, 1), ({"inverse": True}, 1)])
        self.assertEqual(ansi_text(row), ">\x1b[0;7m \x1b[0m")


if __name__ == "__main__":
    unittest.main()
