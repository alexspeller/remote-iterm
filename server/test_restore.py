import unittest

from server.restore import clean_title, pane_ids, plan_text, split_count

PANE = lambda i: {"type": "pane", "id": i}  # noqa: E731


class RestorePureTest(unittest.TestCase):
    def test_single_pane(self):
        self.assertEqual(pane_ids(PANE("z")), ["z"])
        self.assertEqual(split_count(PANE("z")), 0)

    def test_visual_order_preserved(self):
        tree = {"type": "split", "vertical": False, "children": [
            {"type": "split", "vertical": True,
             "children": [PANE("A"), PANE("B")]},
            PANE("C"),
        ]}
        self.assertEqual(pane_ids(tree), ["A", "B", "C"])

    def test_split_count_is_panes_minus_one(self):
        grid = {"type": "split", "vertical": True, "children": [
            {"type": "split", "vertical": False,
             "children": [PANE("a"), PANE("b")]},
            {"type": "split", "vertical": False,
             "children": [PANE("c"), PANE("d")]},
        ]}
        self.assertEqual(pane_ids(grid), ["a", "b", "c", "d"])
        self.assertEqual(split_count(grid), len(pane_ids(grid)) - 1)

    def test_clean_title_keeps_project_identity_strips_bell(self):
        self.assertEqual(clean_title("🧰 mailai"), "🧰 mailai")
        self.assertEqual(clean_title("🔔 🧰 mailai"), "🧰 mailai")
        self.assertEqual(clean_title("🔔 🔔 x"), "x")
        self.assertEqual(clean_title(""), "")
        self.assertEqual(clean_title(None), "")

    def test_plan_text_lists_panes(self):
        snap = {"windows": [{"tabs": [{
            "index": 1, "title": "t",
            "tree": {"type": "split", "vertical": True,
                     "children": [PANE("A"), PANE("B")]},
            "panes": [
                {"id": "A", "cwd": "/x", "cmd": "vim"},
                {"id": "B", "cwd": "/y", "job": "fish"},
            ],
        }]}]}
        text = plan_text(snap)
        self.assertIn("2 pane(s)", text)
        self.assertIn("/x", text)
        self.assertIn("$ vim", text)
        self.assertIn("1 splits", text)


if __name__ == "__main__":
    unittest.main()
