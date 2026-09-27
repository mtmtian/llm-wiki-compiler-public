"""Explicitly migrate reviewed pre-manifest shared projection files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions" / "knowledge-flow"))

from common import config_from  # noqa: E402
from replica import read_baseline  # noqa: E402
from replica_recovery import verify_generation  # noqa: E402
from shared_materialize import migrate_legacy_projection  # noqa: E402


def _plan(path: Path, baseline_id: str) -> dict[str, str]:
    """Read the private reviewed inventory and bind it to the immutable baseline."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid migration plan JSON") from error
    if not isinstance(value, dict) or set(value) != {"baselineId", "files"}:
        raise ValueError("migration plan must contain baselineId and files")
    if value["baselineId"] != baseline_id or not isinstance(value["files"], dict):
        raise ValueError("migration plan baseline does not match the verified baseline")
    return value["files"]


def main(argv: list[str] | None = None) -> int:
    """Run a dry-run by default; only --apply writes the shared projection."""
    parser = argparse.ArgumentParser(description="Migrate reviewed legacy shared projection files")
    parser.add_argument("--config", required=True, help="private knowledge-flow configuration")
    parser.add_argument("--plan", required=True, help="private JSON inventory of expected legacy hashes")
    parser.add_argument("--apply", action="store_true", help="apply the migration after preflight")
    args = parser.parse_args(argv)
    try:
        config = config_from(args.config)
        baseline = read_baseline(config)
        expected = _plan(Path(args.plan), baseline["snapshotId"])
        generation = Path(config["wikiRoot"]).resolve(strict=True)
        verify_generation(Path(config["stateDir"]), generation)
        result = migrate_legacy_projection(config, str(generation), baseline, expected, not args.apply)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"current", "dry-run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
