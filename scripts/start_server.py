from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


def _background_process_options() -> dict[str, object]:
    if os.name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        }
    return {"start_new_session": True}


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    stdout_path = ARTIFACTS / "server.log"
    stderr_path = ARTIFACTS / "server.err.log"
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-u", "run.py", "serve"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            **_background_process_options(),
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
