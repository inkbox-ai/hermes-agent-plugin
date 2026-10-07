"""Stage the committed plugin outside the native host's managed workspace."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def stage_plugin(source: Path, runtime_home: Path, temp_root: Path) -> Path:
    """Archive HEAD without copying runtime state or recursing into its destination."""
    source = source.resolve()
    runtime_home = runtime_home.resolve()
    temp_root = temp_root.resolve()
    if temp_root.is_relative_to(source) or temp_root.is_relative_to(runtime_home):
        raise ValueError("Plugin staging must be outside the checkout and Hermes home")
    temp_root.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="inkbox-plugin-", dir=temp_root))
    try:
        with tempfile.TemporaryFile(dir=temp_root) as archive:
            subprocess.run(
                ["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
                stdout=archive, stderr=subprocess.PIPE, check=True,
            )
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r:") as members:
                members.extractall(destination, filter="data")
        if not (destination / "plugin.yaml").is_file() or not (destination / "pyproject.toml").is_file():
            raise ValueError("Committed plugin manifest and Python declaration are required")
        return destination
    except BaseException:
        shutil.rmtree(destination)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    args = parser.parse_args()
    print(stage_plugin(args.source, args.runtime_home, args.temp_root))


if __name__ == "__main__":
    main()
