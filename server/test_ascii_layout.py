import unittest

from server.ascii_layout import render


class AsciiLayoutTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(render({}), "")

    def test_single_pane_is_a_box(self):
        out = render({"a": (0.0, 0.0, 1.0, 1.0)}, {"a": ["hello"]}, cols=20)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("┌"))
        self.assertTrue(lines[0].endswith("┐"))
        self.assertTrue(lines[-1].startswith("└"))
        self.assertTrue(lines[-1].endswith("┘"))
        self.assertIn("hello", out)

    def test_side_by_side_has_vertical_tee_junctions(self):
        rects = {"a": (0.0, 0.0, 0.5, 1.0), "b": (0.5, 0.0, 0.5, 1.0)}
        out = render(rects, cols=24)
        lines = out.splitlines()
        self.assertIn("┬", lines[0])   # top divider
        self.assertIn("┴", lines[-1])  # bottom divider
        self.assertTrue(any("│" in ln for ln in lines[1:-1]))

    def test_four_grid_has_cross_junction(self):
        rects = {
            "a": (0.0, 0.0, 0.5, 0.5), "b": (0.5, 0.0, 0.5, 0.5),
            "c": (0.0, 0.5, 0.5, 0.5), "d": (0.5, 0.5, 0.5, 0.5),
        }
        out = render(rects, cols=24)
        self.assertIn("┼", out)  # the shared center junction

    def test_labels_are_clipped_not_overflowing(self):
        out = render({"a": (0.0, 0.0, 1.0, 1.0)},
                     {"a": ["x" * 500]}, cols=20)
        widest = max(len(ln) for ln in out.splitlines())
        self.assertLessEqual(widest, 2 * 20 + 1)


if __name__ == "__main__":
    unittest.main()
