#!/usr/bin/env python3
"""Rebuild iTerm2 windows/tabs/panes from a snapshot.

Loads a snapshot (``latest/state.json`` by default, or a history record via
``--at``) and replays it into **new** windows — it never touches your existing
windows. Each restored pane is split into the saved arrangement, ``cd``'d back to
its directory, and has its previous output *echoed* (via ``async_inject``, which
writes to the terminal without running it) plus a note of the command that was
running. Running processes cannot be revived; split proportions are approximate
(the iTerm2 split API always halves), but structure, orientation, cwd, and
content are faithful.
"""
import shlex

import iterm2

# --- pure helpers (unit-tested; mirror the live replay below) ------------------

def pane_ids(node) -> list:
    """Leaf session ids in visual order (left→right / top→bottom)."""
    if node.get("type") == "pane":
        return [node["id"]]
    ids = []
    for child in node.get("children", []):
        ids.extend(pane_ids(child))
    return ids


def split_count(node) -> int:
    """Number of async_split_pane calls needed to realize this tree."""
    if node.get("type") == "pane":
        return 0
    children = node.get("children", [])
    return sum(split_count(c) for c in children) + max(0, len(children) - 1)


_ATTENTION_PREFIX = "🔔 "  # transient highlighter marker, not part of a tab's identity


def clean_title(title) -> str:
    """A tab's restorable title override, minus any transient bell marker."""
    title = title or ""
    while title.startswith(_ATTENTION_PREFIX):
        title = title[len(_ATTENTION_PREFIX):]
    return title


# --- live replay ---------------------------------------------------------------

async def _configure(session, meta, content_dir) -> None:
    if not meta:
        return
    cwd = meta.get("cwd") or ""
    cmd = meta.get("cmd") or meta.get("job") or ""
    content = ""
    ref = meta.get("contentFile")
    if ref and content_dir is not None:
        path = content_dir / ref
        try:
            if path.exists():
                content = path.read_text(errors="replace")
        except OSError:
            content = ""

    parts = []
    if content:
        parts.append("\r\n\x1b[2m─── restored pane · previous output ───\x1b[0m\r\n")
        parts.append(content.replace("\r\n", "\n").replace("\n", "\r\n"))
    if cmd:
        parts.append(f"\r\n\x1b[2m─── was running: {cmd} ───\x1b[0m\r\n")
    if parts:
        try:
            await session.async_inject("".join(parts).encode("utf-8", "replace"))
        except Exception:
            pass
    if cwd:
        # CR (\r), not LF: a real Return submits in the shell.
        await session.async_send_text("cd " + shlex.quote(cwd) + "\r")


async def _realize(node, session, meta_by_id, content_dir) -> None:
    if node.get("type") == "pane":
        await _configure(session, meta_by_id.get(node["id"]), content_dir)
        return
    children = node.get("children", [])
    if not children:
        return
    # First child keeps the current session's region; split off the rest along
    # this splitter's axis, preserving visual order.
    sessions = [session]
    for _ in range(1, len(children)):
        new = await sessions[-1].async_split_pane(vertical=bool(node["vertical"]))
        sessions.append(new)
    for child, sess in zip(children, sessions):
        await _realize(child, sess, meta_by_id, content_dir)


async def restore(connection, snapshot: dict, content_dir=None) -> int:
    """Rebuild ``snapshot`` into new windows. Returns the pane count restored."""
    app = await iterm2.async_get_app(connection)
    restored = 0
    first_window = None
    for win in snapshot.get("windows", []):
        window = None
        for tab_index, tab in enumerate(win.get("tabs", [])):
            if tab_index == 0:
                window = await iterm2.Window.async_create(connection)
                if window is None:
                    break
                if first_window is None:
                    first_window = window
                itab = window.current_tab
            else:
                itab = await window.async_create_tab()
                if itab is None:
                    continue
            # Replay an explicit tab title (e.g. a "🧰 <project>" set by
            # ~/bin/project) so restored project tabs keep their identity.
            override = clean_title(tab.get("titleOverride"))
            if override:
                try:
                    await itab.async_set_title(override)
                except Exception:
                    pass
            root = itab.current_session
            if root is None:
                continue
            meta_by_id = {p["id"]: p for p in tab.get("panes", [])}
            tree = tab.get("tree") or {"type": "pane",
                                       "id": next(iter(meta_by_id), None)}
            await _realize(tree, root, meta_by_id, content_dir)
            restored += len(pane_ids(tree))
    if first_window is not None:
        try:
            await first_window.async_activate()
            await app.async_activate()
        except Exception:
            pass
    return restored


def plan_text(snapshot: dict) -> str:
    """Human-readable description of what a restore would create (dry-run)."""
    out = []
    windows = snapshot.get("windows", [])
    n_tabs = sum(len(w.get("tabs", [])) for w in windows)
    n_panes = sum(len(t.get("panes", [])) for w in windows for t in w.get("tabs", []))
    out.append(f"Would restore {len(windows)} window(s), {n_tabs} tab(s), "
               f"{n_panes} pane(s) into NEW windows:")
    for wi, win in enumerate(windows, start=1):
        out.append(f"  Window {wi}:")
        for tab in win.get("tabs", []):
            title = tab.get("title") or "(untitled)"
            tree = tab.get("tree") or {}
            splits = split_count(tree) if tree else 0
            out.append(f"    Tab {tab.get('index')}: {title} "
                       f"({len(tab.get('panes', []))} panes, {splits} splits)")
            for pane in tab.get("panes", []):
                cwd = pane.get("cwd") or ""
                cmd = pane.get("cmd") or pane.get("job") or ""
                out.append(f"        · {cwd}" + (f"    $ {cmd}" if cmd else ""))
    return "\n".join(out) + "\n"
