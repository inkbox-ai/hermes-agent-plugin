"""Bounded diagnostics for optional dependency imports."""
import logging
import re


def report_import_failure(dependency: str, error: ImportError) -> None:
    name = error.name
    module = name if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", name) else "unknown"
    logging.getLogger(__name__).warning(
        "Inkbox dependency import failed: dependency=%s category=%s module=%s partially_initialized=%s",
        dependency, "module_not_found" if isinstance(error, ModuleNotFoundError) else "import_error",
        module, "partially initialized module" in str(error),
    )
