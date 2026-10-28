"""How the iterm-server launcher keeps one stuck operation from blocking all
the others: a loopback probe that never connects is cut off, and a lock held
by a launcher wedged past every budget is taken back.

Each test sources a copy of the launcher from a temporary directory, so its
lock, pidfile and log live there. Nothing here runs start, stop or restart:
those act on whatever holds ports 7291/7292, which on a working Mac is the
live stack.
"""
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "iterm-server"

# Makes connect_port hang the way the 2026-10-21 probe did, as a process that
# never returns until killed.
HUNG_CONNECT = 'connect_port() { exec sleep 30.5; }'


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="iterm-server-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.launcher = self.dir / "iterm-server"
        shutil.copy2(LAUNCHER, self.launcher)
        self.lockdir = self.dir / ".iterm-server.lock"
        self.addCleanup(self.assert_no_command_ran)

    def assert_no_command_ran(self):
        # The launcher logs every command before running it. A sourced copy
        # that ran one would be running `start`, whose orphan sweep kills
        # stray server.py and snapshot.py processes Mac-wide.
        self.assertFalse((self.dir / ".iterm-server.log").exists(),
                         "sourcing the launcher ran a command")

    def bash(self, script, env=None, timeout=30):
        """Run `script` in bash with the launcher's functions sourced, its
        output captured: run() only returns once nothing holds the pipes,
        so a leftover process would show up as lost time."""
        started = time.monotonic()
        result = subprocess.run(
            ["/bin/bash", "-c", f'source "$0"\n{script}', str(self.launcher)],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})})
        return result, time.monotonic() - started

    def spawn_holder(self, script):
        """Start a launcher that takes the lock and runs `script` under it,
        returning once it holds the lock."""
        holder = subprocess.Popen(
            ["/bin/bash", "-c", f'source "$0"\n{script}', str(self.launcher)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, holder)
        deadline = time.monotonic() + 10
        while not (self.lockdir / "pid").exists():
            self.assertLess(time.monotonic(), deadline, "holder never took the lock")
            time.sleep(0.05)
        return holder

    def _reap(self, proc):
        for pid in self._children(proc.pid):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()

    def _children(self, pid):
        out = subprocess.run(["pgrep", "-P", str(pid)],
                             capture_output=True, text=True).stdout
        return [int(p) for p in out.split()]

    def test_probe_tells_a_listening_port_from_a_closed_one(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            result, _ = self.bash(f"port_listening {port}")
            self.assertEqual(result.returncode, 0, result.stderr)
        result, _ = self.bash(f"port_listening {port}")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_a_connect_that_never_returns_is_cut_off(self):
        result, elapsed = self.bash(
            f"{HUNG_CONNECT}\nport_listening 7291; echo rc=$?",
            env={"PROBE_SECONDS": "1"})
        self.assertNotIn("rc=0", result.stdout)
        self.assertIn("rc=", result.stdout)
        self.assertLess(elapsed, 5)
        self.assertEqual(result.stderr, "")
        leftover = subprocess.run(["pgrep", "-f", "sleep 30.5"],
                                  capture_output=True, text=True).stdout
        self.assertEqual(leftover, "")

    def test_waiting_for_a_port_keeps_its_budget_when_connects_hang(self):
        result, elapsed = self.bash(
            f'{HUNG_CONNECT}\nwait_for_port 7291 "" 2; echo rc=$?',
            env={"PROBE_SECONDS": "1"})
        self.assertIn("rc=1", result.stdout)
        self.assertLess(elapsed, 6)

    def test_a_holder_wedged_past_the_hold_limit_is_killed(self):
        # The shape of the 2026-10-21 incident: the launcher holding the lock
        # is alive, blocked in a subshell of its own that never returns.
        fifo = self.dir / "never-written"
        os.mkfifo(fifo)
        holder = self.spawn_holder(f'with_lock eval \'( read -r _ < "{fifo}" )\'')
        deadline = time.monotonic() + 5
        while not (stuck := self._children(holder.pid)):
            self.assertLess(time.monotonic(), deadline, "holder's subshell never started")
            time.sleep(0.05)
        result, _ = self.bash("with_lock echo acquired",
                              env={"LOCK_HOLD_SECONDS": "1", "LOCK_WAIT_SECONDS": "15"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acquired", result.stdout)
        self.assertIn(f"Killing iterm-server {holder.pid}", result.stderr)
        self.assertEqual(holder.wait(timeout=5), -9)
        for pid in stuck:
            deadline = time.monotonic() + 5
            while _alive(pid):
                self.assertLess(time.monotonic(), deadline, f"subshell {pid} survived")
                time.sleep(0.05)
        self.assertFalse(self.lockdir.exists())

    def test_a_holder_within_the_hold_limit_is_waited_for(self):
        fifo = self.dir / "never-written"
        os.mkfifo(fifo)
        holder = self.spawn_holder(f'with_lock eval \'( read -r _ < "{fifo}" )\'')
        result, _ = self.bash("with_lock echo acquired",
                              env={"LOCK_HOLD_SECONDS": "600", "LOCK_WAIT_SECONDS": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("acquired", result.stdout)
        self.assertIn("Timed out waiting", result.stderr)
        self.assertIsNone(holder.poll())

    def test_a_lock_whose_pid_now_belongs_to_another_process_is_cleared(self):
        # This test process stands in for whatever inherited the pid.
        self.lockdir.mkdir()
        (self.lockdir / "started").write_text("Thu Jan  1 00:00:00 1970\n")
        (self.lockdir / "pid").write_text(f"{os.getpid()}\n")
        result, _ = self.bash("with_lock echo acquired",
                              env={"LOCK_HOLD_SECONDS": "0", "LOCK_WAIT_SECONDS": "5"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acquired", result.stdout)
        self.assertIn(f"Cleared stale lock left by dead process {os.getpid()}",
                      result.stderr)

    def test_a_holder_that_cannot_be_identified_is_never_killed(self):
        # A lock from a launcher that predates the recorded start time.
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap, sleeper)
        self.lockdir.mkdir()
        (self.lockdir / "pid").write_text(f"{sleeper.pid}\n")
        time.sleep(1.1)
        result, _ = self.bash("with_lock echo acquired",
                              env={"LOCK_HOLD_SECONDS": "0", "LOCK_WAIT_SECONDS": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Timed out waiting", result.stderr)
        self.assertIsNone(sleeper.poll())


if __name__ == "__main__":
    unittest.main()
