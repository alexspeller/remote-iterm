import json
import tempfile
import unittest
from pathlib import Path

from server import snapshot as S
from server.restore import pane_ids, split_count


def make_clean(cwd="/Users/x/proj", captured="2026-08-19T10:00:00+00:00"):
    return {
        "version": 1,
        "capturedAt": captured,
        "windows": [{
            "id": "w1", "isFront": True, "bounds": {"x": 0, "y": 0, "w": 9, "h": 9},
            "tabs": [{
                "index": 1, "title": "proj", "isSelected": True,
                "currentSessionId": "p:A", "aspect": 1.7, "maximized": False,
                "usingCachedLayout": False,
                "tree": {"type": "pane", "id": "p:A"},
                "layout": "┌┐\n└┘",
                "panes": [{
                    "id": "p:A", "name": "fish", "cwd": cwd, "job": "vim",
                    "cmd": "vim file", "cols": 80, "rows": 24,
                    "rect": {"x": 0, "y": 0, "w": 1, "h": 1},
                    "contentFile": "panes/p_A.txt", "contentLines": 3,
                }],
            }],
        }],
    }


class HelperTest(unittest.TestCase):
    def test_tilde(self):
        home = str(Path.home())
        self.assertEqual(S._tilde(home), "~")
        self.assertEqual(S._tilde(home + "/dexory"), "~/dexory")
        self.assertEqual(S._tilde("/etc/hosts"), "/etc/hosts")

    def test_safe_id(self):
        self.assertEqual(S._safe_id("w0t1p2:AB-cd.ef"), "w0t1p2_AB-cd.ef")

    def test_history_record_strips_content_keeps_tree(self):
        rec = S._history_record(make_clean())
        pane = rec["windows"][0]["tabs"][0]["panes"][0]
        self.assertNotIn("contentFile", pane)
        self.assertNotIn("contentLines", pane)
        self.assertNotIn("_content", pane)
        self.assertIn("cwd", pane)
        self.assertIn("tree", rec["windows"][0]["tabs"][0])
        self.assertIn("layout", rec["windows"][0]["tabs"][0])

    def test_render_snapshot_text(self):
        text = S.render_snapshot_text(make_clean())
        self.assertIn("Window 1", text)
        self.assertIn("Tab 1: proj", text)
        self.assertIn("* ", text)          # focused-pane marker
        self.assertIn("$ vim file", text)  # last command in the table


class GridTreeTest(unittest.TestCase):
    def test_grid_tree_covers_every_pane(self):
        for n in range(1, 10):
            ids = [f"p{i}" for i in range(n)]
            tree = S._grid_tree(ids)
            self.assertEqual(pane_ids(tree), ids)           # all panes, in order
            self.assertEqual(split_count(tree), n - 1)      # nothing lost

    def test_grid_tree_single_and_empty(self):
        self.assertEqual(S._grid_tree(["only"]), {"type": "pane", "id": "only"})
        self.assertEqual(S._grid_tree([])["type"], "pane")


class HistoryFsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = S.HISTORY_DIR
        S.HISTORY_DIR = Path(self._tmp.name)

    def tearDown(self):
        S.HISTORY_DIR = self._orig
        self._tmp.cleanup()

    def _lines(self):
        files = sorted(S.HISTORY_DIR.glob("*.jsonl"))
        return [ln for f in files for ln in f.read_text().splitlines() if ln]

    def test_append_dedupes_unchanged(self):
        snap = S.Snapshotter(None, None)
        snap._append_history(make_clean(cwd="/a"))
        snap._append_history(make_clean(cwd="/a"))  # identical -> no new line
        self.assertEqual(len(self._lines()), 1)
        snap._append_history(make_clean(cwd="/b"))  # changed -> new line
        self.assertEqual(len(self._lines()), 2)

    def test_prune_drops_files_older_than_retention(self):
        from datetime import date, timedelta
        old = (date.today() - timedelta(days=S.RETENTION_DAYS + 1)).isoformat()
        fresh = date.today().isoformat()
        (S.HISTORY_DIR / f"{old}.jsonl").write_text('{"x":1}\n')
        (S.HISTORY_DIR / f"{fresh}.jsonl").write_text('{"x":1}\n')
        S._prune_history()
        self.assertFalse((S.HISTORY_DIR / f"{old}.jsonl").exists())
        self.assertTrue((S.HISTORY_DIR / f"{fresh}.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
