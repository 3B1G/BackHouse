"""bk_runner.tree_run: a timeout must kill the child's children too (uv run -> the Bookkeeper's Python). Uses a
real, harmless subprocess tree of plain `python -c` processes; no uv, no network, no ASI."""
import os
import subprocess
import sys
import time

import pytest

from orchestrator import bk_runner as R

PARENT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


def alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_timeout_kills_the_whole_tree(tmp_path):
    out_file = tmp_path / "child_pid.txt"
    script = PARENT.replace("print(child.pid, flush=True)", f"open({str(out_file)!r}, 'w').write(str(child.pid))")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        R.tree_run([sys.executable, "-c", script], timeout=3, capture_output=True, text=True)
    assert time.monotonic() - started < 20
    child = int(out_file.read_text())
    for _ in range(20):                                   # give the OS a moment to reap it
        if not alive(child):
            break
        time.sleep(0.25)
    assert not alive(child), "the grandchild process survived the timeout"


def test_normal_run_returns_output():
    r = R.tree_run([sys.executable, "-c", "print('ok')"], timeout=20, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok"
