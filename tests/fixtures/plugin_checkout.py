"""Committed plugin checkout with untracked native runtime state for CI tests."""

from pathlib import Path
import shutil
import subprocess


def plugin_checkout(root: Path) -> tuple[Path, Path]:
    source = root / "checkout"
    source.mkdir()
    repository = Path(__file__).resolve().parents[2]
    for name in ("plugin.yaml", "pyproject.toml"):
        shutil.copyfile(repository / name, source / name)
    (source / "__init__.py").write_text('VALUE = "committed"\n')
    for args in (("init", "--quiet"), ("add", "plugin.yaml", "pyproject.toml", "__init__.py"),
                 ("-c", "user.name=Plugin Test", "-c", "user.email=plugin@example.com",
                  "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic plugin")):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    runtime = source / ".hermes"
    (runtime / "installs").mkdir(parents=True)
    (runtime / ".env").write_text("SYNTHETIC_PRIVATE_RUNTIME=must-not-copy\n")
    (source / ".env").write_text("SYNTHETIC_PRIVATE_SOURCE=must-not-copy\n")
    (source / ".venv").mkdir()
    (source / ".venv" / "private-state").write_text("must-not-copy")
    (source / "__init__.py").write_text('VALUE = "uncommitted"\n')
    return source, runtime
