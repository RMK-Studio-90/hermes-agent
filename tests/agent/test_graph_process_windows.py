import asyncio
from pathlib import Path
import sys

import psutil
import pytest

from agent.graph.process import run_process


@pytest.mark.windows_only
def test_timeout_closes_windows_job_including_child(tmp_path):
    pid_file = tmp_path / "child.pid"
    child = "import time; time.sleep(60)"
    parent = ("import subprocess,sys,time; from pathlib import Path; "
              f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
              f"Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)")
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run_process([sys.executable, "-c", parent], timeout=2))
    assert pid_file.exists()
    assert not psutil.pid_exists(int(pid_file.read_text()))
