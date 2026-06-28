"""Doctor checks for the Inkbox Hermes plugin."""

from __future__ import annotations

import json
from typing import Any, Dict, List

try:
    from .config import inkbox_client_kwargs, object_summary, read_config
except ImportError:  # pragma: no cover - direct local import/test fallback
    from config import inkbox_client_kwargs, object_summary, read_config


def run_doctor() -> Dict[str, Any]:
    cfg = read_config()
    findings: List[Dict[str, str]] = []

    if not cfg.api_key:
        findings.append({
            "id": "inkbox/config-missing-api-key",
            "severity": "error",
            "message": "INKBOX_API_KEY is not set.",
        })
    if not cfg.identity:
        findings.append({
            "id": "inkbox/config-missing-identity",
            "severity": "error",
            "message": "INKBOX_IDENTITY is not set.",
        })
    if not cfg.signing_key:
        findings.append({
            "id": "inkbox/config-missing-signing-key",
            "severity": "warning",
            "message": "INKBOX_SIGNING_KEY is not set. Inbound webhook signature verification will fail unless disabled.",
        })

    sdk_available = True
    try:
        from inkbox import Inkbox
    except Exception as exc:
        sdk_available = False
        findings.append({
            "id": "inkbox/sdk-missing",
            "severity": "error",
            "message": f"Python inkbox SDK is not importable: {exc}. Install with `pip install inkbox aiohttp`.",
        })

    summary: Dict[str, Any] = {
        "configured": bool(cfg.api_key and cfg.identity),
        "sdkAvailable": sdk_available,
        "baseUrl": cfg.base_url,
        "identity": cfg.identity or None,
        "publicUrl": cfg.public_url or None,
        "realtimeConfigured": bool(cfg.realtime_api_key),
        "findings": findings,
    }

    if sdk_available and cfg.api_key:
        try:
            client = Inkbox(**inkbox_client_kwargs(cfg.api_key, cfg.base_url))
            summary["whoami"] = object_summary(client.whoami())
            if cfg.identity:
                identity = client.get_identity(cfg.identity)
                summary["identityRecord"] = object_summary(identity)
        except Exception as exc:
            findings.append({
                "id": "inkbox/api-check-failed",
                "severity": "error",
                "message": str(exc),
            })

    summary["ok"] = not any(f["severity"] == "error" for f in findings)
    return summary


def print_doctor() -> None:
    print(json.dumps(run_doctor(), indent=2, sort_keys=True))
