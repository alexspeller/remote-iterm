#!/usr/bin/env python3
"""iTerm2 AutoLaunch supervisor for remote-iterm.

Installed (symlinked) into iTerm2's AutoLaunch folder so the whole remote-iterm
stack — the phone server (7291), the Vite client (7292), and the state
snapshotter — starts automatically whenever iTerm2 launches, and stops when it
quits. This is what makes it "no use if I forget to start it" a non-issue.

Design notes:
- Deliberately uses ONLY the stdlib (no ``import iterm2``). It doesn't need the
  API, and importing it would consume the single ``ITERM2_COOKIE`` iTerm2 hands
  to AutoLaunch scripts — which the child processes need to auth themselves.
- It scrubs ITERM2_COOKIE/ITERM2_KEY from the children's env so each child does
  its own (already-approved) automation-based auth, exactly as a normal
  ``./iterm-server`` launch from a shell does.
- Children are started through the user's LOGIN shell so their real PATH (mise /
  Homebrew node for ``npx vite``) is present, which an AutoLaunch env lacks.
- iTerm2 quit is detected two ways: a SIGTERM/SIGINT handler, and a parent-pid
  watch (when iTerm2 dies this script reparents to launchd, ppid -> 1).
"""
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.path.realpath(__file__)).parent.parent
ITERM_SERVER = REPO / "iterm-server"
LOG_PATH = (Path.home() / "Library" / "Application Support" / "remote-iterm"
            / "snapshots" / "supervisor.log")
CHECK_INTERVAL = 15.0
SERVER_PORT = 7291

_original_ppid = os.getppid()
_stopping = False


def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def _child_env() -> dict:
    env = dict(os.environ)
    env.pop("ITERM2_COOKIE", None)
    env.pop("ITERM2_KEY", None)
    return env


def _run(action: str) -> None:
    """Run `iterm-server <action>` via the user's login shell for a full PATH."""
    shell = os.environ.get("SHELL", "/bin/bash")
    cmd = f"{shlex.quote(str(ITERM_SERVER))} {action}"
    try:
        subprocess.run([shell, "-l", "-c", cmd], env=_child_env(),
                       timeout=120, check=False)
    except Exception as err:
        log(f"'{action}' failed: {err}")


def _server_up() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", SERVER_PORT)) == 0


def stop_and_exit(*_args) -> None:
    global _stopping
    if _stopping:
        return
    _stopping = True
    log("stopping stack")
    _run("stop")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, stop_and_exit)
    signal.signal(signal.SIGINT, stop_and_exit)
    log(f"supervisor starting (repo={REPO}, ppid={_original_ppid})")
    _run("start")

    while True:
        time.sleep(CHECK_INTERVAL)
        # iTerm2 has quit: our launcher parent died and we reparented to launchd.
        if os.getppid() != _original_ppid:
            log("parent changed — iTerm2 quit; stopping")
            stop_and_exit()
        # Whole stack died (not just one child) — bring it back up.
        if not _server_up():
            log("server port down — restarting stack")
            _run("start")


if __name__ == "__main__":
    main()
