#!/usr/bin/env python3
"""CLI for the iTerm2 state snapshotter / restore.

  iterm-snapshot show                 print the latest human-readable layout
  iterm-snapshot list                 list restorable sessions + history
  iterm-snapshot snapshot             capture one snapshot now
  iterm-snapshot restore              rebuild your LAST completed session (with
                                      content) into NEW windows — this is the
                                      post-crash default
  iterm-snapshot restore --current    rebuild the current live session instead
  iterm-snapshot restore --session TS rebuild a specific archived session
  iterm-snapshot restore --at TS      rebuild from history (layout + cwd only)
  iterm-snapshot restore --dry-run    print what a restore would create
  iterm-snapshot install              install the AutoLaunch supervisor
"""
import argparse
import json
import os
import sys
from pathlib import Path

import iterm2

try:
    from . import snapshot as snap
    from .restore import plan_text, restore
except ImportError:
    import snapshot as snap
    from restore import plan_text, restore


def _load_latest() -> dict | None:
    path = snap.LATEST_DIR / "state.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _history_lines() -> list[dict]:
    records = []
    if not snap.HISTORY_DIR.exists():
        return records
    for f in sorted(snap.HISTORY_DIR.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    records.sort(key=lambda r: r.get("capturedAt", ""))
    return records


def _find_at(at: str) -> dict | None:
    """Latest history record whose capturedAt <= ``at`` (prefix-friendly)."""
    records = _history_lines()
    chosen = None
    for r in records:
        if r.get("capturedAt", "") <= at or r.get("capturedAt", "").startswith(at):
            chosen = r
    return chosen or (records[-1] if records else None)


def _load_state(state_dir: Path) -> dict | None:
    path = state_dir / "state.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _summary(state: dict) -> str:
    windows = state.get("windows", [])
    n_tabs = sum(len(w.get("tabs", [])) for w in windows)
    n_panes = sum(len(t.get("panes", [])) for w in windows for t in w.get("tabs", []))
    return f"{len(windows)}w {n_tabs}t {n_panes}p"


def _find_session(token: str) -> Path | None:
    """Match a session archive by capturedAt prefix (raw or _safe_id form)."""
    safe = snap._safe_id(token)
    for d in snap.session_archives():
        if d.name.startswith(safe) or d.name.startswith(token):
            return d
    return None


def cmd_show(args) -> int:
    path = snap.LATEST_DIR / "layout.txt"
    if not path.exists():
        print("No snapshot yet. Is remote-iterm running?", file=sys.stderr)
        return 1
    sys.stdout.write(path.read_text())
    return 0


def cmd_list(args) -> int:
    archives = snap.session_archives()
    printed = False
    if archives:
        printed = True
        print("Completed sessions (restorable with content — newest first):")
        for i, d in enumerate(archives):
            state = _load_state(d)
            summ = _summary(state) if state else "?"
            captured = state.get("capturedAt", d.name) if state else d.name
            tag = "  <- default `restore`" if i == 0 else ""
            print(f"  {captured:32}  {summ}{tag}")
            print(f"       restore with: iterm-snapshot restore --session {d.name}")
    live = _load_latest()
    if live:
        printed = True
        print(f"\nCurrent live session: {_summary(live)}  "
              f"(restore with `--current`)")
    records = _history_lines()
    if records:
        printed = True
        print(f"\nHistory (layout + cwd only, no content): "
              f"{len(records)} record(s), restore with `--at <timestamp>`")
    if not printed:
        print("No snapshots yet. Is remote-iterm running?", file=sys.stderr)
        return 1
    return 0


def cmd_snapshot(args) -> int:
    async def _main(connection):
        app = await iterm2.async_get_app(connection)
        await snap.Snapshotter(connection, app).snapshot()
        print(f"Snapshot written to {snap.LATEST_DIR}")
    iterm2.run_until_complete(_main)
    return 0


def _resolve_restore_source(args):
    """Pick (snapshot_dict, content_dir, source_label) for restore.

    Default is the most recent COMPLETED session archive — after a crash+reopen
    that is your pre-crash session (with content), not the blank one now live in
    `latest/`. --current forces the live session; --session/--at select others.
    """
    if args.at:
        return _find_at(args.at), None, f"history near {args.at}"
    if args.current:
        return _load_latest(), snap.LATEST_DIR, "current (live) session"
    if args.session:
        d = _find_session(args.session)
        if d is None:
            return None, None, f"session {args.session}"
        return _load_state(d), d, f"session {d.name}"
    archives = snap.session_archives()
    if archives:
        d = archives[0]
        return _load_state(d), d, f"last session ({d.name})"
    # No prior session archived yet — fall back to the live one.
    return _load_latest(), snap.LATEST_DIR, "current (live) session"


def cmd_restore(args) -> int:
    snapshot, content_dir, source = _resolve_restore_source(args)
    if not snapshot:
        print("No snapshot found to restore.", file=sys.stderr)
        return 1
    summary = _summary(snapshot)
    if args.dry_run:
        print(f"[{source} — {summary}]")
        sys.stdout.write(plan_text(snapshot))
        return 0

    async def _main(connection):
        n = await restore(connection, snapshot, content_dir)
        print(f"Restored {n} pane(s) from {source} ({summary}) into new window(s).")
    iterm2.run_until_complete(_main)
    return 0


def _autolaunch_dir() -> Path:
    custom = Path.home() / ".config" / "iterm2" / "AppSupport"
    if custom.exists():
        return custom / "Scripts" / "AutoLaunch"
    return (Path.home() / "Library" / "Application Support" / "iTerm2"
            / "Scripts" / "AutoLaunch")


def cmd_install(args) -> int:
    repo = Path(__file__).resolve().parent.parent
    source = repo / "autolaunch" / "remote-iterm.py"
    if not source.exists():
        print(f"Supervisor script missing: {source}", file=sys.stderr)
        return 1
    dest_dir = _autolaunch_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "remote-iterm.py"
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() and os.path.realpath(dest) == str(source):
            print(f"Already installed: {dest} -> {source}")
            return 0
        print(f"Refusing to overwrite existing {dest}", file=sys.stderr)
        return 1
    dest.symlink_to(source)
    print(f"Installed AutoLaunch supervisor: {dest} -> {source}")
    print("Restart iTerm2 (or run it from Scripts menu) to start it now.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="iterm-snapshot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the latest layout")
    p_list = sub.add_parser("list", help="list retained snapshots")
    p_list.add_argument("--limit", type=int, default=50)
    sub.add_parser("snapshot", help="capture one snapshot now")
    p_restore = sub.add_parser(
        "restore",
        help="rebuild into new windows (default: your last completed session)")
    grp = p_restore.add_mutually_exclusive_group()
    grp.add_argument("--current", action="store_true",
                     help="restore the current live session instead of the last one")
    grp.add_argument("--session", metavar="TS",
                     help="restore a specific archived session (see `list`)")
    grp.add_argument("--at", metavar="TS",
                     help="restore nearest history record at/before TS (layout only)")
    p_restore.add_argument("--dry-run", action="store_true",
                           help="print what would be restored, do nothing")
    sub.add_parser("install", help="install the AutoLaunch supervisor")

    args = parser.parse_args()
    handlers = {
        "show": cmd_show, "list": cmd_list, "snapshot": cmd_snapshot,
        "restore": cmd_restore, "install": cmd_install,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
