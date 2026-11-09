"""Report log availability and counts without rendering log contents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize(path: Path) -> dict[str, bool | int]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        text = ""
    lines = text.splitlines()
    return {
        "present": bool(text),
        "line_count": len(lines),
        "error_lines": sum("error" in line.casefold() for line in lines),
        "warning_lines": sum("warning" in line.casefold() for line in lines),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("gateway", "hermes", "driver", "mock"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(json.dumps({args.kind: summarize(args.path)}))
