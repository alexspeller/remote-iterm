#!/usr/bin/env python3
"""iTerm2 AutoLaunch supervisor for remote-iterm.

Installed (symlinked) into iTerm2's AutoLaunch folder so the whole remote-iterm
stack — the phone server (7291), the web client (7292), and the state
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
  Homebrew node for ``npx vite preview``) is present, which an AutoLaunch env lacks.
- iTerm2 quit is detected two ways: a SIGTERM/SIGINT handler, and a watch on
  the iTerm2 process itself. iTerm2 sends AutoLaunch scripts no signal when it
  quits, and runs them as iTerm2 -> login shell -c -> it2_api_wrapper.sh ->
  python. Neither shell execs its last command, so this script's own parent is
  the wrapper, which outlives iTerm2 (it is reparented to launchd, but our
  ppid never changes). Watching only the parent let every iTerm2 restart leave
  one more supervisor behind: five ran at once on 2026-10-27.
- Only one supervisor runs at a time, holding an flock on LOCK_PATH. A newer
  one (iTerm2 relaunched, or the script launched again from the Scripts menu)
  asks the holder to hand over, and the holder exits without stopping the
  stack. iTerm2 only runs AutoLaunch at launch, so the newest supervisor is the
  one whose iTerm2 is current.
"""
import fcntl
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
PIDFILE = REPO / ".iterm-server.pid"
# Holds the owning supervisor's pid while it holds the flock. A supervisor
# waiting for the lock writes its own pid to HANDOVER_PATH, so the holder
# only steps down for one that is still there to take over.
LOCK_PATH = REPO / ".supervisor.lock"
HANDOVER_PATH = REPO / ".supervisor.lock.handover"
LOCK_POLL = 0.5
LOG_PATH = (Path.home() / "Library" / "Application Support" / "remote-iterm"
            / "snapshots" / "supervisor.log")
CHECK_INTERVAL = 5.0
SERVER_PORT = 7291
# A restart drops every connected phone, so it takes sustained evidence:
# the port must fail this many consecutive probes (CHECK_INTERVAL apart)
# first. One failed half-second connect is not a dead server — on
# 2026-09-19 a lone miss ran `start`, whose orphan sweep then killed a
# server whose event loop was demonstrably healthy.
SERVER_DOWN_POLLS = 3
PROBE_TIMEOUT = 2.0
# Must exceed iterm-server's own worst case (LOCK_WAIT_SECONDS +
# SERVER_START_SECONDS + CLIENT_START_SECONDS, plus venv/auth overhead). When
# this kill lands mid-start instead, the shell dies without running the
# launcher's lock-release trap, so the lock is left behind and the next poll
# waits out its whole budget before failing the same way — which is how a
# recoverable slow start turned into a 68-minute outage on 2026-09-02.
# Overshooting costs nothing: while `start` runs it is already doing the work
# this loop would otherwise wake up and ask for again. The kill reaches the
# login shell, which only bash and zsh replace with the launcher; under fish
# the launcher outlives it, so iterm-server's LOCK_HOLD_SECONDS, kept equal
# to this, is what finally ends a launcher that is stuck.
START_TIMEOUT = 600

_original_ppid = os.getppid()
_iterm2_pid = 0
_owner = False
_handover_requested = False
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
                       timeout=START_TIMEOUT, check=False)
    except Exception as err:
        log(f"'{action}' failed: {err}")


def _server_up() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(PROBE_TIMEOUT)
        return s.connect_ex(("127.0.0.1", SERVER_PORT)) == 0


def _snapshot_up() -> bool:
    """snapshot.py has no port of its own (see docs/ARCHITECTURE.md), so its
    liveness can only be checked via the pidfile's third PID — matching
    iterm-server's own _pid_alive_running check. Without this, a
    snapshot.py that self-exits (its connection_watchdog detected a broken
    iTerm2 connection) would never come back: _server_up() alone can't see
    it, since the phone server keeps port 7291 open independently.
    """
    try:
        pids = PIDFILE.read_text().split()
    except OSError:
        return False
    if len(pids) < 3:
        return False
    pid = pids[2]
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            capture_output=True, text=True, timeout=5, check=False)
    except Exception:
        return False
    return "snapshot.py" in result.stdout


def _ps(pid: int, fields: str) -> str:
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", fields],
            capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except Exception:
        return ""


def _iterm2_ancestor() -> int:
    """The pid of the iTerm2 that launched this script, or 0 if none is found
    (run by hand from a shell, say), in which case the parent is watched."""
    pid = os.getppid()
    for _ in range(10):
        if pid <= 1:
            return 0
        ppid, _, command = _ps(pid, "ppid=,comm=").partition(" ")
        if os.path.basename(command.strip()) == "iTerm2":
            return pid
        try:
            pid = int(ppid)
        except ValueError:
            return 0
    return 0


def _iterm2_gone() -> bool:
    if not _iterm2_pid:
        return os.getppid() != _original_ppid
    try:
        os.kill(_iterm2_pid, 0)
    except OSError:
        # PermissionError too: the pid now belongs to another user's process.
        return True
    return False


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def _is_supervisor(pid: int) -> bool:
    return pid > 0 and Path(__file__).name in _ps(pid, "command=")


def _take_lock() -> bool:
    """Become the only supervisor, asking any current one to hand over.
    Returns False if iTerm2 quit while waiting, leaving the stack to the
    holder. The flock goes with this process, so a supervisor that dies by
    any means, SIGKILL included, frees it at once."""
    global _owner
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    asked = 0
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            pass
        if _iterm2_gone():
            if _read_pid(HANDOVER_PATH) == os.getpid():
                HANDOVER_PATH.unlink(missing_ok=True)
            log("iTerm2 quit while waiting to take over; exiting")
            return False
        # Asked again whenever the holder changes: another newcomer may
        # have taken the lock first.
        holder = _read_pid(LOCK_PATH)
        if holder != asked and _is_supervisor(holder):
            HANDOVER_PATH.write_text(f"{os.getpid()}\n")
            log(f"supervisor {holder} is running — asking it to hand over")
            try:
                os.kill(holder, signal.SIGUSR1)
                asked = holder
            except OSError:
                pass
        time.sleep(LOCK_POLL)
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    if _read_pid(HANDOVER_PATH) == os.getpid():
        HANDOVER_PATH.unlink(missing_ok=True)
    _owner = True
    return True


def _handover_due() -> bool:
    """Whether a newer supervisor that asked to take over is still waiting.
    One whose iTerm2 has since quit withdraws, and stepping down for it
    would leave nobody minding the stack."""
    global _handover_requested
    if not _handover_requested:
        return False
    _handover_requested = False
    waiter = _read_pid(HANDOVER_PATH)
    return waiter != os.getpid() and _is_supervisor(waiter)


def _request_handover(*_args) -> None:
    # Only a flag: exiting from inside a running `start` would kill the
    # login shell before the launcher releases its lock.
    global _handover_requested
    _handover_requested = True


def stop_and_exit(*_args) -> None:
    global _stopping
    if _stopping:
        return
    _stopping = True
    # A supervisor still waiting for the lock never ran the stack, and the
    # holder is the one that stops it.
    if _owner:
        log("stopping stack")
        _run("stop")
    sys.exit(0)


def main() -> None:
    global _iterm2_pid
    signal.signal(signal.SIGTERM, stop_and_exit)
    signal.signal(signal.SIGINT, stop_and_exit)
    signal.signal(signal.SIGUSR1, _request_handover)
    _iterm2_pid = _iterm2_ancestor()
    log(f"supervisor starting (repo={REPO}, pid={os.getpid()}, "
        f"ppid={_original_ppid}, iTerm2={_iterm2_pid or 'not found'})")
    if not _take_lock():
        return
    if _iterm2_gone():
        log("iTerm2 quit before the stack started; stopping")
        stop_and_exit()
    _run("start")

    down_polls = 0
    while True:
        time.sleep(CHECK_INTERVAL)
        # Checked first: a relaunched iTerm2's supervisor takes over the
        # running stack without dropping the phones connected to it.
        if _handover_due():
            log(f"supervisor {os.getpid()} handing over to "
                f"{_read_pid(HANDOVER_PATH)}; leaving the stack running")
            return
        if _iterm2_gone():
            log("iTerm2 quit; stopping")
            stop_and_exit()
        # Either the phone server or the independent snapshotter died —
        # `iterm-server start` reconciles each child independently, so
        # calling it here always repairs exactly what's missing without
        # disturbing whatever is still healthy.
        server_ok = _server_up()
        down_polls = 0 if server_ok else down_polls + 1
        snapshot_ok = _snapshot_up()
        if down_polls >= SERVER_DOWN_POLLS or not snapshot_ok:
            log(f"server port down for {down_polls} probes — restarting stack"
                if not server_ok else "snapshotter died — restarting stack")
            _run("start")
            down_polls = 0


if __name__ == "__main__":
    main()
