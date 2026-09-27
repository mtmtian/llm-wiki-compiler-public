"""Install, disable or inspect the bundled Claude/Pi Wiki adapters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parent.parent
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from deployment.agent_plugins.installer import install, status


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """Add explicit isolated-root and shared-config selectors."""
    parser.add_argument("--home", type=Path, help="home directory used for host paths")
    parser.add_argument("--config", type=Path, help="shared knowledge-flow.json path")


def parser() -> argparse.ArgumentParser:
    """Build the public native adapter management CLI."""
    result = argparse.ArgumentParser(description="Manage optional Claude/Pi llmwiki adapters")
    actions = result.add_subparsers(dest="action", required=True)
    for action in ("install", "disable"):
        command = actions.add_parser(action)
        add_common_options(command)
        command.add_argument("--host", choices=("claude", "pi"), required=True)
        command.add_argument("--profile", action="append", help="native host profile; repeatable")
        command.add_argument("--pi-settings", type=Path, help="custom Pi settings file")
        command.add_argument("--dry-run", action="store_true", help="show changes without writing")
    inspect = actions.add_parser("status")
    add_common_options(inspect)
    inspect.add_argument("--host", choices=("claude", "pi"))
    return result


def run(args: Any) -> dict[str, Any]:
    """Call the read-only status or transactional install operation."""
    return status(args) if args.action == "status" else install(args)


def main() -> int:
    """Print a JSON result and keep errors free of event or credential data."""
    args = parser().parse_args()
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"llmwiki agent {args.action} failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
