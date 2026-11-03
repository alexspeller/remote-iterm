"""How the AutoLaunch supervisor stays a single instance that ends with its
iTerm2: a second copy takes over from the first without stopping the stack,
and a copy whose iTerm2 has quit stops the stack and exits.

iTerm2 runs an AutoLaunch script as iTerm2 -> shell -c -> it2_api_wrapper.sh
-> python, and sends it no signal when it quits. Each test reproduces that
chain with a stand-in "iTerm2" (a symlink to bash, so ps reports the name) and
a copy of the supervisor in a temporary directory, next to a fake
iterm-server that only records the actions it is asked for. HOME points
there too, so supervisor.log is the copy's own. Nothing here starts or stops
the real stack; the copy's only contact with it is the supervisor's
read-only probe of port 7291.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SUPERVISOR = Path(__file__).resolve().parent.parent / "autolaunch" / "remote-iterm.py"

FAKE_ITERM_SERVER = """#!/bin/bash
here="$(cd "$(dirname "$0")" && pwd)"
echo "$1 begin" >> "$here/actions"
# Only the first start is slow, so a test can catch a supervisor mid-start.
if [ "$1" = start ] && [ -n "${FAKE_FIRST_START_SECONDS:-}" ] \\
    && mkdir "$here/.first-start" 2>/dev/null; then
  sleep "$FAKE_FIRST_START_SECONDS"
fi
echo "$1 end" >> "$here/actions"
"""

# The shape of iTerm2's it2_api_wrapper.sh: bash running the script as its
# last command, which bash does not exec when reading a file.
WRAPPER = """#!/bin/bash
"$@"
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)],
                         capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ri-supervisor-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "autolaunch").mkdir()
        self.supervisor = self.dir / "autolaunch" / "remote-iterm.py"
        shutil.copy2(SUPERVISOR, self.supervisor)
        for name, body in (("iterm-server", FAKE_ITERM_SERVER),
                           ("it2_api_wrapper.sh", WRAPPER)):
            path = self.dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.iterm2 = self.dir / "iTerm2"
        self.iterm2.symlink_to("/bin/bash")
        self.log_path = (self.dir / "Library" / "Application Support"
                         / "remote-iterm" / "snapshots" / "supervisor.log")
        self.env = {**os.environ, "HOME": str(self.dir), "SHELL": "/bin/bash"}
        self.env.pop("FAKE_FIRST_START_SECONDS", None)
        self._pids: list[int] = []
        self._iterm2s: list[subprocess.Popen] = []
        self.addCleanup(self._kill_all)

    def launch(self) -> tuple[subprocess.Popen, int]:
        """Start a stand-in iTerm2 that runs the supervisor the way iTerm2
        does, returning it and the supervisor's pid."""
        iterm2 = subprocess.Popen(
            [str(self.iterm2), "-c", '"$0" "$@" & wait',
             str(self.dir / "it2_api_wrapper.sh"), sys.executable,
             str(self.supervisor)],
            env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._iterm2s.append(iterm2)
        self._pids.append(iterm2.pid)
        wrapper = self.wait_for(lambda: _children(iterm2.pid),
                                "the wrapper never started")[0]
        self._pids.append(wrapper)
        supervisor = self.wait_for(lambda: _children(wrapper),
                                   "the supervisor never started")[0]
        self._pids.append(supervisor)
        return iterm2, supervisor

    def wait_for(self, check, message, timeout=20.0):
        deadline = time.monotonic() + timeout
        while not (result := check()):
            self.assertLess(time.monotonic(), deadline, message)
            time.sleep(0.05)
        return result

    def log_lines(self) -> list[str]:
        try:
            return self.log_path.read_text().splitlines()
        except FileNotFoundError:
            return []

    def actions(self) -> list[str]:
        try:
            return (self.dir / "actions").read_text().splitlines()
        except FileNotFoundError:
            return []

    def _kill_all(self):
        for pid in reversed(self._pids):
            for child in _children(pid):
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for iterm2 in self._iterm2s:
            iterm2.wait()

    def test_a_second_supervisor_takes_over_without_stopping_the_stack(self):
        _, first = self.launch()
        self.wait_for(lambda: "start end" in self.actions(),
                      "the first supervisor never started the stack")
        _, second = self.launch()
        self.wait_for(lambda: not _alive(first),
                      "the first supervisor kept running beside the second")
        self.assertTrue(_alive(second))
        self.assertNotIn("stop begin", self.actions())
        self.assertTrue(any(f"supervisor {first} handing over" in line
                            for line in self.log_lines()), self.log_lines())

    def test_a_start_in_progress_finishes_before_the_handover(self):
        # Ending the supervisor mid-start would kill the login shell before
        # the launcher released its lock.
        self.env["FAKE_FIRST_START_SECONDS"] = "3"
        _, first = self.launch()
        self.wait_for(lambda: "start begin" in self.actions(),
                      "the first supervisor never began a start")
        _, second = self.launch()
        self.wait_for(lambda: not _alive(first),
                      "the first supervisor kept running beside the second")
        actions = self.actions()
        self.assertEqual(actions[:2], ["start begin", "start end"], actions)
        self.assertNotIn("stop begin", actions)
        self.assertTrue(_alive(second))

    def test_a_supervisor_whose_iterm2_quit_stops_the_stack_and_exits(self):
        iterm2, supervisor = self.launch()
        self.wait_for(lambda: "start end" in self.actions(),
                      "the supervisor never started the stack")
        # A quit sends AutoLaunch scripts no signal; the wrapper outlives
        # iTerm2 and is reparented to launchd.
        iterm2.kill()
        iterm2.wait()
        self.wait_for(lambda: not _alive(supervisor),
                      "the supervisor outlived its iTerm2")
        self.assertIn("stop end", self.actions())

    def test_a_waiting_supervisor_whose_iterm2_quit_leaves_the_stack_alone(self):
        self.env["FAKE_FIRST_START_SECONDS"] = "8"
        _, first = self.launch()
        self.wait_for(lambda: "start begin" in self.actions(),
                      "the first supervisor never began a start")
        iterm2, second = self.launch()
        self.wait_for(lambda: any("asking it to hand over" in line
                                  for line in self.log_lines()),
                      "the second supervisor never asked for the lock")
        iterm2.kill()
        iterm2.wait()
        self.wait_for(lambda: not _alive(second),
                      "the waiting supervisor outlived its iTerm2")
        # Its request went with it, so the first keeps the stack.
        time.sleep(10)
        self.assertTrue(_alive(first))
        self.assertNotIn("stop begin", self.actions())


if __name__ == "__main__":
    unittest.main()
