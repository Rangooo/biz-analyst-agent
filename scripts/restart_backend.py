"""Restart backend with stderr logging to file."""
import subprocess, os, sys

PYTHON = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\biz-analyst\Scripts\python.exe"
BACKEND = r"D:\analyst agent\biz-analyst-agent\backend"
LOG = r"D:\analyst agent\biz-analyst-agent\backend_debug.log"

env = os.environ.copy()
env["PATH"] = r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.12.0;" + env.get("PATH", "")
env.pop("NODE_OPTIONS", None)  # 防止 --use-system-ca 导致 node/mcporter 拒绝启动

with open(LOG, "w") as log:
    p = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000", "--log-level", "info"],
        cwd=BACKEND,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=0x00000008 | 0x00000200,
        env=env,
    )
    print(f"Started backend PID={p.pid}, log={LOG}")
