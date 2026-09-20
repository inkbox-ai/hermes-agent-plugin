#!/usr/bin/env python3

import sys
from collections import Counter
from pathlib import Path

from tools.plugin_guard import format_scan_report, scan_plugin


plugin_dir = Path(sys.argv[1]).resolve()
result = scan_plugin(plugin_dir, source="inkbox-ai/hermes-agent-plugin")
counts = Counter(finding.severity for finding in result.findings)
print(f"Hermes plugin scan: {result.verdict}; findings by severity: {dict(counts)}")
if result.verdict != "safe":
    raise SystemExit(format_scan_report(result))
