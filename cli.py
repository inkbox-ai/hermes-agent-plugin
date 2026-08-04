"""CLI commands for the Inkbox Hermes plugin."""

from __future__ import annotations

import json

try:
    from .doctor import run_doctor
    from .setup_wizard import interactive_setup
    from .tools import inkbox_whoami
except ImportError:  # pragma: no cover - direct local import/test fallback
    from doctor import run_doctor
    from setup_wizard import interactive_setup
    from tools import inkbox_whoami


def setup_argparse(subparser) -> None:
    subs = subparser.add_subparsers(dest="inkbox_command")
    subs.add_parser("setup", help="Run the Inkbox setup wizard")
    subs.add_parser("doctor", help="Run Inkbox readiness checks")
    subs.add_parser("whoami", help="Show the configured Inkbox identity")
    subparser.set_defaults(func=handle_cli)


def handle_cli(args) -> None:
    command = getattr(args, "inkbox_command", None)
    if command == "setup":
        interactive_setup()
        return
    if command == "doctor":
        print(json.dumps(run_doctor(), indent=2, sort_keys=True))
        return
    if command == "whoami":
        print(json.dumps(json.loads(inkbox_whoami({})), indent=2, sort_keys=True))
        return
    print("Usage: hermes inkbox <setup|doctor|whoami>")


def slash_handler(raw_args: str) -> str:
    command = (raw_args or "").strip().lower()
    if command in {"", "doctor", "status"}:
        return json.dumps(run_doctor(), indent=2, sort_keys=True)
    if command == "whoami":
        return json.dumps(json.loads(inkbox_whoami({})), indent=2, sort_keys=True)
    if command == "setup":
        return "Run setup from a terminal: hermes inkbox setup"
    return "Usage: /inkbox [doctor|whoami]"
