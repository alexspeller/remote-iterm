#!/usr/bin/env python3
"""CLI for the iTerm2 state snapshotter / restore.

  iterm-snapshot show                 print the latest human-readable layout
  iterm-snapshot list                 list snapshots retained in history
  iterm-snapshot snapshot             capture one snapshot now
  iterm-snapshot restore [--at TS]    rebuild windows/panes into NEW windows
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


def cmd_show(args) -> int:
    path = snap.LATEST_DIR / "layout.txt"
    if not path.exists():
        print("No snapshot yet. Is remote-iterm running?", file=sys.stderr)
        return 1
    sys.stdout.write(path.read_text())
    return 0


def cmd_list(args) -> int:
    records = _history_lines()
    if not records:
        print("No history yet.", file=sys.stderr)
        return 1
    for r in records[-args.limit:]:
        windows = r.get("windows", [])
        n_tabs = sum(len(w.get("tabs", [])) for w in windows)
        n_panes = sum(len(t.get("panes", []))
                      for w in windows for t in w.get("tabs", []))
        print(f"{r.get('capturedAt','?'):32}  "
              f"{len(windows)}w {n_tabs}t {n_panes}p")
    return 0


def cmd_snapshot(args) -> int:
    async def _main(connection):
        app = await iterm2.async_get_app(connection)
        await snap.Snapshotter(connection, app).snapshot()
        print(f"Snapshot written to {snap.LATEST_DIR}")
    iterm2.run_until_complete(_main)
    return 0


def cmd_restore(args) -> int:
    if args.at:
        snapshot = _find_at(args.at)
        content_dir = None  # history keeps no content
        source = f"history near {args.at}"
    else:
        snapshot = _load_latest()
        content_dir = snap.LATEST_DIR
        source = "latest"
    if not snapshot:
        print("No snapshot found to restore.", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"[{source}]")
        sys.stdout.write(plan_text(snapshot))
        return 0

    async def _main(connection):
        n = await restore(connection, snapshot, content_dir)
        print(f"Restored {n} pane(s) from {source} into new window(s).")
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
    p_restore = sub.add_parser("restore", help="rebuild into new windows")
    p_restore.add_argument("--at", help="restore nearest history record at/before TS")
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
