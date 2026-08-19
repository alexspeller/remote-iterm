"""Pane (split) geometry for iTerm2 tabs — shared by the phone server and the
snapshotter.

A tab's panes live in a tree of Splitters (verified against the live API):
  * Splitter.vertical=True  -> children are arranged left→right (divide x)
  * Splitter.vertical=False -> children are arranged top→bottom (divide y)
  * child order is visual order; Session.frame is a top-left origin but LOCAL to
    its parent splitter, so tab-wide rects are reconstructed by walking the tree
    and weighting each child by its natural (frame) size along the split axis.

This module intentionally depends only on ``math`` + ``iterm2`` so it can be
imported by the AutoLaunch-spawned snapshotter as well as ``server.py``.
"""
import math

import iterm2


def _natural_size(node) -> tuple:
    if isinstance(node, iterm2.Session):
        f = node.frame
        if f is None:
            return (1.0, 1.0)
        return (float(f.size.width), float(f.size.height))
    sizes = [_natural_size(c) for c in node.children] or [(1.0, 1.0)]
    if node.vertical:
        return (sum(s[0] for s in sizes), max(s[1] for s in sizes))
    return (max(s[0] for s in sizes), sum(s[1] for s in sizes))


def _assign_rects(node, x: float, y: float, w: float, h: float, out: dict) -> None:
    if isinstance(node, iterm2.Session):
        out[node.session_id] = (x, y, w, h)
        return
    sizes = [_natural_size(c) for c in node.children]
    if node.vertical:
        total = sum(s[0] for s in sizes) or 1.0
        cx = x
        for c, s in zip(node.children, sizes):
            cw = w * s[0] / total
            _assign_rects(c, cx, y, cw, h, out)
            cx += cw
    else:
        total = sum(s[1] for s in sizes) or 1.0
        cy = y
        for c, s in zip(node.children, sizes):
            ch = h * s[1] / total
            _assign_rects(c, x, cy, w, ch, out)
            cy += ch


def pane_layout(tab) -> tuple:
    """Returns ({session_id: (x, y, w, h) in 0..1, top-left}, aspect, maximized).

    When a pane is maximized the others are "minimized": they have no frame and
    the split tree collapses to just the maximized pane, so the true geometry is
    gone. In that case we fall back to an even grid over all panes. (The
    snapshotter layers a "last known good layout" cache on top of this so a
    currently-maximized tab still renders its real split geometry.)
    """
    try:
        if tab.minimized_sessions:
            panes = tab.all_sessions
            n = len(panes)
            cols = max(1, math.ceil(math.sqrt(n)))
            rows = max(1, math.ceil(n / cols))
            rects = {
                s.session_id: ((i % cols) / cols, (i // cols) / rows,
                               1 / cols, 1 / rows)
                for i, s in enumerate(panes)
            }
            aspect = 1.6
            for s in panes:
                f = s.frame
                if f is not None and f.size.height:
                    aspect = f.size.width / f.size.height
                    break
            return rects, aspect, True

        root = tab.root
        nat = _natural_size(root)
        aspect = (nat[0] / nat[1]) if nat[1] else 1.0
        rects: dict = {}
        _assign_rects(root, 0.0, 0.0, 1.0, 1.0, rects)
        return rects, aspect, False
    except Exception:
        return {}, 1.0, False


def serialize_tree(node) -> dict:
    """Serialize a tab's splitter tree into plain dicts for restore.

    Leaves carry only the session id; the snapshot layer enriches them with cwd,
    job, and content refs by id. Structure + orientation are captured faithfully
    so restore can replay the exact nesting (split proportions are not — the
    iTerm2 split API has no proportion argument).
    """
    if isinstance(node, iterm2.Session):
        return {"type": "pane", "id": node.session_id}
    return {
        "type": "split",
        "vertical": bool(node.vertical),
        "children": [serialize_tree(c) for c in node.children],
    }
