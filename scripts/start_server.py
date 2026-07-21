from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    stdout_path = ARTIFACTS / "server.log"
    stderr_path = ARTIFACTS / "server.err.log"
    creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-u", "run.py", "serve"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
            close_fds=True,
        )
    (ARTIFACTS / "server.pid").write_text(str(process.pid), encoding="ascii")
    print(
        json.dumps(
            {
                "pid": process.pid,
                "url": "http://127.0.0.1:8765",
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
