import unittest

from server.server import _DEFAULT_PALETTE, _line_runs


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


if __name__ == "__main__":
    unittest.main()
