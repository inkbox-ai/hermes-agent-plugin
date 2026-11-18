"""Read the managed runtime's secret-free diagnostic snapshot."""

import json
import os
import stat
import time


def hosted_diagnostics(identity, harness, signing_key):
    if os.getenv("INKBOX_HOSTED_MODE") != "1":
        return None
    context = os.getenv("INKBOX_DIAGNOSTIC_CONTEXT", "runtime")
    checks = [("delivery mode", True, "Managed delivery; ordinary webhook subscriptions are not required.")]
    if context != "terminal":
        checks.append(("runtime signing key", bool(signing_key),
                       "Configured." if signing_key else "Missing from this runtime; signed inbound delivery cannot be verified."))
    else:
        checks.append(("terminal signing key", True, "Not exposed to the terminal; checked in the runtime report."))
    try:
        fd = os.open(os.environ["INKBOX_DIAGNOSTICS_FILE"], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise ValueError()
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError()
        value = json.loads(raw)
        if (value.get("version") != 1 or not identity or value.get("identity") != identity.lstrip("@")
                or value.get("harness") != harness or type(value.get("ready")) is not bool
                or type(value.get("signing_key_configured")) is not bool
                or type(value.get("observed_at")) not in (int, float)
                or not 0 <= time.time() - value["observed_at"] <= 60
                or type(value.get("pid")) is not int or value["pid"] <= 0):
            raise ValueError()
        os.kill(value["pid"], 0)
        ready = value["ready"] and value["signing_key_configured"]
        checks.append(("hosted runtime", ready, "Ready; signing key configured." if ready else
                       "Not ready; inspect runtime startup and delivery health."))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        checks.append(("hosted runtime", False, "Status unavailable, stale, or mismatched; readiness is unverified."))
    return {"mode": "hosted", "context": context, "ready": all(row[1] for row in checks), "checks": checks}
