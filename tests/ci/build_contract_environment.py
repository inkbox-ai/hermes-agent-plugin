#!/usr/bin/env python3
"""Build real-host tests through Hermes' public, lock-aware PM operations.

The upstream project intentionally supports source imports, not wheel builds.
Its frozen export and lock also carry required upstream resolver overrides;
plain ``uv pip install -e`` does not honor that policy. Never mutate the PM-built
environment afterward. Workflows import the actual host checkout via PYTHONPATH.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tomllib


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    host, out = args.host.resolve(), args.out.absolute()
    if not (host / "pm" / "build_env.py").is_file() or not (host / "uv.lock").is_file():
        parser.error("the real Hermes checkout and its committed lock are required")
    if out.exists() or out.is_symlink():
        parser.error("the independent environment output must not already exist")

    # Only PM's public facade runs here; its manager owns resolution and checks.
    sys.path.insert(0, str(host))
    import pm

    export = out.with_name(out.name + "-host-requirements.txt")
    pm.export_requirements(host, export, explicit=True)
    requirements = [line.strip() for line in export.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    plugin = Path(__file__).resolve().parents[2]
    project = tomllib.loads((plugin / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements.extend(project["dependencies"])
    requirements.extend(project["optional-dependencies"]["test"])
    interpreter = pm.build_requirements_environment(
        requirements, out=out, lock=host / "uv.lock", explicit=True,
    )
    print(interpreter)


if __name__ == "__main__":
    main()
