import types
import unittest

import iterm2

from server.geometry import _assign_rects, _natural_size, pane_layout, serialize_tree


class FakeSession(iterm2.Session):
    """A leaf that passes geometry's ``isinstance(node, iterm2.Session)`` check."""

    def __init__(self, sid, w=100.0, h=50.0):
        self._sid, self._w, self._h = sid, w, h

    @property
    def session_id(self):
        return self._sid

    @property
    def frame(self):
        return types.SimpleNamespace(
            size=types.SimpleNamespace(width=self._w, height=self._h))


class FakeSplit:
    def __init__(self, vertical, children):
        self.vertical = vertical
        self.children = children


class FakeTab:
    def __init__(self, root, minimized=None, all_sessions=None):
        self.root = root
        self.minimized_sessions = minimized or []
        self.all_sessions = all_sessions or []


class GeometryTest(unittest.TestCase):
    def test_vertical_split_two_equal(self):
        a, b = FakeSession("a"), FakeSession("b")
        root = FakeSplit(vertical=True, children=[a, b])
        out = {}
        _assign_rects(root, 0.0, 0.0, 1.0, 1.0, out)
        self.assertEqual(out["a"], (0.0, 0.0, 0.5, 1.0))
        self.assertEqual(out["b"], (0.5, 0.0, 0.5, 1.0))

    def test_horizontal_split_top_bottom(self):
        a, b = FakeSession("a"), FakeSession("b")
        root = FakeSplit(vertical=False, children=[a, b])
        out = {}
        _assign_rects(root, 0.0, 0.0, 1.0, 1.0, out)
        self.assertEqual(out["a"], (0.0, 0.0, 1.0, 0.5))
        self.assertEqual(out["b"], (0.0, 0.5, 1.0, 0.5))

    def test_weighted_by_natural_size(self):
        # a is 3x as wide as b along the vertical (x) split axis.
        a, b = FakeSession("a", w=300, h=100), FakeSession("b", w=100, h=100)
        root = FakeSplit(vertical=True, children=[a, b])
        out = {}
        _assign_rects(root, 0.0, 0.0, 1.0, 1.0, out)
        self.assertAlmostEqual(out["a"][2], 0.75)
        self.assertAlmostEqual(out["b"][0], 0.75)
        self.assertAlmostEqual(out["b"][2], 0.25)

    def test_nested_natural_size(self):
        inner = FakeSplit(True, [FakeSession("a"), FakeSession("b")])
        root = FakeSplit(False, [inner, FakeSession("c")])
        # natural width = max(inner width, c width); height = inner + c
        w, h = _natural_size(root)
        self.assertGreater(h, 0)
        self.assertGreater(w, 0)

    def test_pane_layout_normal(self):
        a, b = FakeSession("a"), FakeSession("b")
        tab = FakeTab(root=FakeSplit(True, [a, b]))
        rects, aspect, maximized = pane_layout(tab)
        self.assertFalse(maximized)
        self.assertEqual(set(rects), {"a", "b"})

    def test_pane_layout_maximized_even_grid(self):
        panes = [FakeSession(x) for x in ("a", "b", "c", "d")]
        tab = FakeTab(root=None, minimized=[panes[1]], all_sessions=panes)
        rects, aspect, maximized = pane_layout(tab)
        self.assertTrue(maximized)
        self.assertEqual(set(rects), {"a", "b", "c", "d"})  # every pane present

    def test_serialize_tree(self):
        tree = serialize_tree(FakeSplit(True, [
            FakeSession("a"),
            FakeSplit(False, [FakeSession("b"), FakeSession("c")]),
        ]))
        self.assertEqual(tree["type"], "split")
        self.assertTrue(tree["vertical"])
        self.assertEqual(tree["children"][0], {"type": "pane", "id": "a"})
        self.assertEqual(tree["children"][1]["type"], "split")
        self.assertFalse(tree["children"][1]["vertical"])
        self.assertEqual([c["id"] for c in tree["children"][1]["children"]],
                         ["b", "c"])


if __name__ == "__main__":
    unittest.main()
