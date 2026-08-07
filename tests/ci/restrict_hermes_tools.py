#!/usr/bin/env python3
"""Restrict live gateway turns to this plugin's tools."""

from __future__ import annotations

import os
from pathlib import Path


def restrict_inkbox_platform(config: dict) -> dict:
    platform_toolsets = config.setdefault("platform_toolsets", {})
    if not isinstance(platform_toolsets, dict):
        platform_toolsets = {}
        config["platform_toolsets"] = platform_toolsets
    platform_toolsets["inkbox"] = ["inkbox", "no_mcp"]
    return config


def main() -> None:
    import yaml

    config_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    restrict_inkbox_platform(config)
    temporary = config_path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    temporary.replace(config_path)


if __name__ == "__main__":
    main()
