from __future__ import annotations

from pathlib import Path
import time
from typing import Any


def delayed_write(arguments: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    time.sleep(float(arguments["delay_seconds"]))
    Path(str(arguments["marker_path"])).write_text("completed", encoding="utf-8")
    return {"written": True}
