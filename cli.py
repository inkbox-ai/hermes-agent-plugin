"""CLI commands for the Inkbox Hermes plugin."""

from __future__ import annotations

import json
import os
import sys

try:
    from .bootstrap import bootstrap
    from .doctor import run_doctor
    from .setup_wizard import interactive_setup
    from .tools import inkbox_whoami
except ImportError:  # pragma: no cover - direct local import/test fallback
    from bootstrap import bootstrap
    from doctor import run_doctor
    from setup_wizard import interactive_setup
    from tools import inkbox_whoami


def setup_argparse(subparser) -> None:
    subs = subparser.add_subparsers(dest="inkbox_command")
    subs.add_parser("setup", help="Run the Inkbox setup wizard")
    bootstrap_parser = subs.add_parser(
        "bootstrap",
        help="Configure an existing Inkbox identity without interactive prompts",
    )
    bootstrap_parser.add_argument("--identity", required=True, help="Existing Inkbox identity handle")
    bootstrap_parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read the API key from standard input instead of INKBOX_API_KEY",
    )
    bootstrap_parser.add_argument("--base-url", default="", help="Optional Inkbox API base URL")
    bootstrap_parser.add_argument(
        "--voice-ai",
        action="store_true",
        help="Use Inkbox Voice AI for incoming calls",
    )
    bootstrap_parser.add_argument(
        "--voice-ai-instructions-file",
        help="Optional UTF-8 file containing Voice AI instructions",
    )
    bootstrap_parser.add_argument(
        "--rotate-signing-key",
        action="store_true",
        help="Rotate an existing identity signing key when it is unavailable locally",
    )
    bootstrap_parser.add_argument(
        "--start-gateway",
        action="store_true",
        help="Start or restart the Hermes gateway after configuration",
    )
    subs.add_parser("doctor", help="Run Inkbox readiness checks")
    subs.add_parser("whoami", help="Show the configured Inkbox identity")
    subparser.set_defaults(func=handle_cli)


def handle_cli(args) -> None:
    command = getattr(args, "inkbox_command", None)
    if command == "setup":
        interactive_setup()
        return
    if command == "bootstrap":
        api_key = sys.stdin.read().strip() if args.api_key_stdin else os.getenv("INKBOX_API_KEY", "").strip()
        instructions = None
        if args.voice_ai_instructions_file:
            with open(args.voice_ai_instructions_file, encoding="utf-8") as instructions_file:
                instructions = instructions_file.read()
        result = bootstrap(
            identity_handle=args.identity,
            api_key=api_key,
            base_url=args.base_url,
            voice_ai=args.voice_ai,
            voice_ai_instructions=instructions,
            rotate_signing_key=args.rotate_signing_key,
            start_gateway=args.start_gateway,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["status"] != "configured":
            raise SystemExit(2)
        return
    if command == "doctor":
        print(json.dumps(run_doctor(), indent=2, sort_keys=True))
        return
    if command == "whoami":
        print(json.dumps(json.loads(inkbox_whoami({})), indent=2, sort_keys=True))
        return
    print("Usage: hermes inkbox <setup|bootstrap|doctor|whoami>")


def slash_handler(raw_args: str) -> str:
    command = (raw_args or "").strip().lower()
    if command in {"", "doctor", "status"}:
        return json.dumps(run_doctor(), indent=2, sort_keys=True)
    if command == "whoami":
        return json.dumps(json.loads(inkbox_whoami({})), indent=2, sort_keys=True)
    if command == "setup":
        return "Run setup from a terminal: hermes inkbox setup"
    return "Usage: /inkbox [doctor|whoami]"
