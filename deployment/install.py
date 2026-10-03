"""Install the portable llm-wiki workflow for one local machine.

The deployment files in this directory are shared policy.  This installer
expands their machine placeholders, preserves local routing additions, and
atomically writes the small set of private host files.  It never changes the
Wiki, Codex trust state, schedules, or credentials.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from install_roles import (MACHINE_ID_PATTERN, configure_roles, validate_runtime_manifest)
from install_events import (directories_to_create, event_enabled, event_paths, event_plan,
                            launchctl_action, remove_owned_files)
from install_scope import migrate_repository_exclusions
from install_helpers import (CODEX_HOOK_LAUNCHER, hook_command, load_inputs, needs_update, own_hook, redact,
                             render_alma_session, render_codex_hook, render_launcher, render_local, render_maintenance,
                             select_wiki, validate_runtime as _validate_runtime, write_plan as _write_plan)


PLACEHOLDER = re.compile(r"\$\{([A-Z_]+)\}")
ENV_KEYS = (
    "LLMWIKI_PROVIDER",
    "LLMWIKI_MODEL",
    "LLMWIKI_EMBEDDING_PROVIDER",
    "LLMWIKI_EMBEDDING_MODEL",
    "OLLAMA_EMBEDDINGS_HOST",
    "LLMWIKI_EMBED_STRICT",
    "LLMWIKI_OUTPUT_LANG",
    "LLMWIKI_COMPILE_CONCURRENCY",
)
RUNTIME_FILES = ("dist/cli.js", "alma-session.py",
    "knowledge-flow/worker.mjs",
    "knowledge-flow/common.py", "knowledge-flow/exchange.py", "knowledge-flow/hooks.py",
    "knowledge-flow/read_routing.py", "knowledge-flow/hook_context.py",
    "knowledge-flow/context_observation.py",
    "knowledge-flow/operational_context.py", "knowledge-flow/semantic_scope.py",
    "knowledge-flow/capture.py", "knowledge-flow/queue_worker.py", "knowledge-flow/wake.py",
    "knowledge-flow/queue_wire.py", "knowledge-flow/queue_recovery.py",
    "knowledge-flow/queue_replica.py", "knowledge-flow/queue_schedule.py",
    "knowledge-flow/notify.py",
    "knowledge-flow/maintenance.py", "knowledge-flow/route_mentions.py", "knowledge-flow/turn_routing.py",
    "knowledge-flow/replica.py", "knowledge-flow/replica_generation.py", "knowledge-flow/replica_cleanup.py",
    "knowledge-flow/replica_records.py", "knowledge-flow/routing.py",
    "knowledge-flow/publication_resolutions.py",
    "knowledge-flow/publication_resolution_contract.py",
    "knowledge-flow/topic_routes.py",
    "knowledge-flow/shared_materialize.py", "knowledge-flow/shared_files.py",
    "knowledge-flow/replica_integrity.py",
    "knowledge-flow/replica_recovery.py",
    "knowledge-flow/writer_records.py", "knowledge-flow/writer_checkpoint.py",
    "knowledge-flow/writer_handoff.py",
)

DEFAULT_ENV = {
    "LLMWIKI_PROVIDER": "codex-agent",
    "LLMWIKI_MODEL": "gpt-5.6-luna",
    "LLMWIKI_EMBEDDING_PROVIDER": "ollama",
    "LLMWIKI_EMBEDDING_MODEL": "nomic-embed-text",
    "OLLAMA_EMBEDDINGS_HOST": "http://127.0.0.1:11434/v1",
    "LLMWIKI_EMBED_STRICT": "1",
    "LLMWIKI_OUTPUT_LANG": "zh-CN",
    "LLMWIKI_COMPILE_CONCURRENCY": "2",
}


def validate_runtime(args: argparse.Namespace) -> tuple[Path, str, str]:
    """Keep the historical helper signature while validating new runtime files."""
    return _validate_runtime(args, RUNTIME_FILES, validate_runtime_manifest, find_binary, check_node)


def write_plan(plan: list[tuple[Path, str, int]], backup_root: Path) -> str | None:
    """Keep the historical helper signature for local installer callers."""
    return _write_plan(plan, backup_root, home_dir())


def home_dir() -> Path:
    """Return HOME explicitly so tests and multiple hosts can isolate state."""
    return Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, distinguishing a missing optional file from malformed data."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON: {path.name}") from error


def expand_value(value: Any, variables: dict[str, str]) -> Any:
    """Expand known placeholders recursively in strings, lists, and objects."""
    if isinstance(value, str):
        current = value
        for _ in range(8):
            updated = PLACEHOLDER.sub(lambda match: variables.get(match.group(1), match.group(0)), current)
            if updated == current:
                break
            current = updated
        if PLACEHOLDER.search(current):
            raise ValueError("unresolved deployment placeholder")
        return current
    if isinstance(value, list):
        return [expand_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: expand_value(item, variables) for key, item in value.items()}
    return value


def find_binary(name: str) -> str:
    """Resolve a required host executable without embedding a host-specific path."""
    value = shutil.which(name)
    if not value:
        raise ValueError(f"required executable not found: {name}")
    return str(Path(value).resolve())


def check_node(node: str) -> None:
    """Require the Node runtime supported by the built compiler."""
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, check=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("unable to check node version") from error
    match = re.search(r"v(\d+)", result.stdout or result.stderr)
    if not match or int(match.group(1)) < 24:
        raise ValueError("node >= 24 is required")


def merge_config(template: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    """Use shared policy while retaining unknown local keys and custom projects."""
    result = copy.deepcopy(template)
    if not isinstance(result.get("projects", {}), dict):
        raise ValueError("knowledge-flow projects must be an object")
    if not existing:
        return result
    result["enabled"] = existing.get("enabled", result.get("enabled", True))
    result["excludedPaths"] = list(dict.fromkeys(result.get("excludedPaths", []) + existing.get("excludedPaths", [])))
    for key, value in existing.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
    old_exchange = existing.get('exchange') or {}
    if old_exchange.get('sharedWriter') is not None:
        for key in ('sharedWriter', 'materializerMachineId'):
            result.setdefault('exchange', {})[key] = copy.deepcopy(old_exchange.get(key))
    template_projects = result.setdefault("projects", {})
    old_projects = existing.get("projects", {})
    if not isinstance(old_projects, dict):
        raise ValueError("existing knowledge-flow projects must be an object")
    for project_id, old_project in old_projects.items():
        if project_id not in template_projects:
            template_projects[project_id] = copy.deepcopy(old_project)
            continue
        if not isinstance(old_project, dict):
            continue
        target = template_projects[project_id]
        for key, value in old_project.items():
            if key not in target:
                target[key] = copy.deepcopy(value)
        # Shared mappings reach installed machines; local additions survive, and
        # machine.json projectPaths remains the exact per-machine override.
        for key in ("topicTerms", "paths"):
            template_values, local_values = target.get(key), old_project.get(key)
            if isinstance(template_values, list) and isinstance(local_values, list):
                target[key] = list(dict.fromkeys(template_values + local_values))
        if old_project.get("pages") and not target.get("pages"):
            target["pages"] = copy.deepcopy(old_project["pages"])
    return result


def configure_shared_writer(config: dict[str, Any], args: argparse.Namespace) -> None:
    """Stage an explicit default change behind a mandatory old-owner release."""
    default = getattr(args, 'materializer_machine_id', None)
    previous = getattr(args, 'shared_writer_bootstrap_machine', None)
    if default is None and previous is None:
        return
    if not default or not previous:
        raise ValueError('default changes require both materializer and bootstrap machine identities')
    if not isinstance(config.get('exchange'), dict):
        raise ValueError('shared writer coordination requires exchange')
    config['exchange'].update(materializerMachineId=default,
                              sharedWriter={'version': 1, 'bootstrapMachineId': previous})


def apply_machine_overrides(config: dict[str, Any], machine: dict[str, Any]) -> None:
    """Apply machine paths and an optional stable identity without inventing one."""
    machine_id = machine.get("machineId")
    if machine_id is not None:
        if not isinstance(machine_id, str) or not MACHINE_ID_PATTERN.fullmatch(machine_id):
            raise ValueError("machineId must be a stable identifier (letters, digits, . _ -)")
        config["machineId"] = machine_id
    if machine.get("wikiRoot"):
        config["wikiRoot"] = machine["wikiRoot"]
    projects = config.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("knowledge-flow projects must be an object")
    for project_id, paths in machine.get("projectPaths", {}).items():
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError("machine projectPaths must map IDs to string lists")
        projects.setdefault(project_id, {"pages": []})["paths"] = paths
    extras = machine.get("excludedPaths", [])
    if not isinstance(extras, list) or not all(isinstance(path, str) for path in extras):
        raise ValueError("machine excludedPaths must be a string list")
    config["excludedPaths"] = list(dict.fromkeys(config.get("excludedPaths", []) + extras))


def load_environment(directory: Path) -> dict[str, str]:
    """Load the non-secret allowlisted compiler environment and pin policy values."""
    source = read_json(directory / "compiler-environment.json", {}) or {}
    if not isinstance(source, dict):
        raise ValueError("compiler environment must be an object")
    values = dict(DEFAULT_ENV)
    for key in ENV_KEYS:
        if key in source:
            if not isinstance(source[key], (str, int, float, bool)):
                raise ValueError("compiler environment values must be scalar")
            values[key] = str(source[key])
    for key, expected in (("LLMWIKI_PROVIDER", "codex-agent"),
                          ("LLMWIKI_MODEL", "gpt-5.6-luna"),
                          ("LLMWIKI_EMBEDDING_PROVIDER", "ollama")):
        if values[key] != expected:
            raise ValueError(f"unsupported {key} policy")
    return values


def build_hooks_for_config(original: dict[str, Any], command: str, config_path: Path) -> dict[str, Any]:
    """Pass the exact config path to hook cleanup without leaking it into hook JSON."""
    result = copy.deepcopy(original)
    groups = result.setdefault("hooks", {})
    for event, matcher, options in (
        ("UserPromptSubmit", None, {"timeout": 8, "additionalContextLimit": 0, "statusMessage": "Wiki：检索项目决策"}),
        ("SessionStart", "resume|clear|compact", {"timeout": 8, "additionalContextLimit": 0, "statusMessage": "Wiki：恢复决策上下文"}),
        ("Stop", None, {"timeout": 600, "statusMessage": "Wiki：采集证据入队"}),
    ):
        groups.setdefault(event, [])
        if not isinstance(groups[event], list):
            raise ValueError("invalid Codex hooks shape")
        kept_groups = []
        for group in groups[event]:
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                raise ValueError("invalid Codex hooks shape")
            kept = [entry for entry in group["hooks"] if not own_hook(entry, config_path)]
            if kept or not group["hooks"]:
                changed = copy.deepcopy(group)
                changed["hooks"] = kept
                kept_groups.append(changed)
        generated = {"hooks": [{"type": "command", "command": command, **options}]}
        if matcher:
            generated["matcher"] = matcher
        groups[event] = kept_groups + [generated]
    return result


def build_config(source: dict[str, Any], machine: dict[str, Any], existing: dict[str, Any] | None,
                 hooks: dict[str, Any], args: argparse.Namespace, runtime: Path, node: str, gh: str,
                 wiki: Path, config_path: Path, home: Path) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Expand shared policy and apply role, runtime, and machine-specific values."""
    variables = {"HOME": str(home), "REPO": str(Path(__file__).resolve().parents[1]),
                 "RUNTIME": str(runtime), "NODE": node, "GH": gh, "WIKI_ROOT": str(wiki)}
    config = merge_config(expand_value(source, variables), existing)
    apply_machine_overrides(config, expand_value(machine, variables))
    config = expand_value(config, variables)
    configure_shared_writer(config, args)
    migrate_repository_exclusions(config, getattr(args, "allow_repository", []))
    intake, publish = configure_roles(config, machine, args, existing, hooks, config_path, own_hook)
    config.update({"wikiRoot": str(wiki), "node": node, "gh": gh,
                   "worker": str(runtime / "knowledge-flow/worker.mjs"),
                   "intakeEnabled": intake})
    exchange = config.get("exchange")
    if isinstance(exchange, dict) and exchange.get("protocolVersion", 1) == 2:
        state_dir = Path(config["stateDir"])
        if not state_dir.is_absolute():
            raise ValueError("stateDir must be an absolute path")
        config["sharedWikiRoot"] = str(wiki)
        config["wikiRoot"] = str(state_dir / "replica/current")
    event = config.setdefault("eventDriven", {})
    if not isinstance(event, dict):
        raise ValueError("eventDriven must be an object")
    if getattr(args, "event_driven", False):
        event["enabled"] = True
    v2_exchange = isinstance(exchange, dict) and exchange.get("protocolVersion", 1) == 2
    if getattr(args, "disable_event_worker", False) or (not intake and not v2_exchange):
        event["enabled"] = False
    event.setdefault("debounceSeconds", 120)
    if config.get("exchange") is not None:
        config["publishEnabled"] = publish
    config.setdefault("enabled", True)
    environment = load_environment(Path(__file__).resolve().parent)
    if config.get("model") != environment["LLMWIKI_MODEL"]:
        raise ValueError("knowledge-flow model does not match compiler environment")
    return config, variables, environment


def output_plan(config: dict[str, Any], environment: dict[str, str],
                runtime: Path, node: str, config_path: Path, hooks: dict[str, Any], home: Path) -> list[tuple[Path, str, int]]:
    """Render every private output in memory before staging or replacing files."""
    hooks_path = home / ".codex/hooks.json"
    command = hook_command(home / CODEX_HOOK_LAUNCHER, config_path)
    outputs = {
        config_path: (json.dumps(config, ensure_ascii=False, indent=2) + "\n", 0o600),
        home / ".config/llmwiki/icloud-wiki.sh": (render_launcher(
            environment, runtime / "dist/cli.js", Path(config["wikiRoot"])), 0o700),
        home / ".local/bin/llmwiki-local": (render_local(
            home / ".config/llmwiki/icloud-wiki.sh", node,
            isinstance(config.get("exchange"), dict) and config["exchange"].get("protocolVersion", 1) == 2), 0o700),
        home / ".local/bin/llmwiki-maintain": (render_maintenance(config_path, sys.executable), 0o700),
        home / ".local/bin/llmwiki-alma-session": (render_alma_session(runtime, config_path, sys.executable), 0o700),
        home / CODEX_HOOK_LAUNCHER: (render_codex_hook(sys.executable), 0o700),
        hooks_path: (json.dumps(build_hooks_for_config(hooks, command, config_path), ensure_ascii=False, indent=2) + "\n", 0o600),
    }
    if event_enabled(config):
        paths, launcher, plist, _ = event_plan(config, home, runtime, sys.executable, config_path)
        outputs[paths.launcher] = (launcher, 0o700)
        outputs[paths.plist] = (plist.decode("utf-8"), 0o600)
    return [(path, content, mode) for path, (content, mode) in outputs.items()]


def prepare(args: argparse.Namespace) -> tuple[list[tuple[Path, str, int]], dict[str, str], dict[str, Any]]:
    """Validate inputs and construct all output files before touching the host."""
    runtime, node, gh = validate_runtime(args)
    home = home_dir()
    source, machine, existing, hooks, config_path = load_inputs(args, home, Path(__file__).resolve().parent, read_json)
    wiki = select_wiki(args, machine, existing, home, expand_value)
    config, variables, environment = build_config(source, machine, existing, hooks, args, runtime, node, gh,
                                                   wiki, config_path, home)
    return output_plan(config, environment, runtime, node, config_path, hooks, home), variables, config


def install(args: argparse.Namespace) -> dict[str, Any]:
    """Run a dry-run or atomically install the prepared private configuration."""
    plan, variables, config = prepare(args)
    changes = []
    for path, content, mode in plan:
        changes.append({"path": redact(path, variables),
                        "action": "update" if needs_update(path, content, mode) else "unchanged"})
    if event_enabled(config):
        for directory in directories_to_create(config):
            changes.append({"path": redact(directory, variables),
                            "action": "create" if not directory.exists() else "unchanged",
                            "kind": "directory"})
    result = {"dryRun": bool(args.dry_run), "intakeEnabled": config["intakeEnabled"],
              "publishEnabled": bool(config.get("publishEnabled", False)), "changes": changes}
    if not args.dry_run:
        created: list[Path] = []
        try:
            if event_enabled(config):
                for directory in directories_to_create(config):
                    if not directory.exists():
                        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        created.append(directory)
            backup = write_plan(plan, home_dir() / ".local/share/llm-wiki-compiler/install-backups")
        except Exception:
            for directory in reversed(created):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        result["backup"] = redact(Path(backup), variables) if backup else None
    return result


def event_worker(args: argparse.Namespace) -> dict[str, Any]:
    """Manage the prepared event worker without changing queue state."""
    home = home_dir()
    config_path = Path(getattr(args, "event_worker_config", "") or
                       home / ".config/llmwiki/knowledge-flow.json").expanduser().resolve()
    paths = event_paths(home, config_path)
    action = args.event_worker_action
    if action == "rollback":
        result = launchctl_action("bootout", paths.plist, dry_run=args.dry_run,
                                  executable=getattr(args, "event_worker_launchctl", None))
        can_remove = bool(args.dry_run or result.get("confirmedInactive"))
        if args.dry_run:
            result["removed"] = []
            result["wouldRemove"] = remove_owned_files(paths, dry_run=True)
        elif can_remove:
            result["removed"] = remove_owned_files(paths)
        else:
            result["removed"] = []
        if not can_remove:
            result["rollbackBlocked"] = "launchd service state could not be confirmed inactive"
        return result
    if action == "enable":
        config = read_json(config_path, None)
        if not isinstance(config, dict):
            raise ValueError("prepared knowledge-flow.json is missing or invalid")
        if not event_enabled(config) or not paths.plist.is_file() or not paths.launcher.is_file():
            raise ValueError("event worker is not prepared; install with --event-driven first")
    return launchctl_action(action, paths.plist, dry_run=args.dry_run,
                            executable=getattr(args, "event_worker_launchctl", None))


def parser() -> argparse.ArgumentParser:
    """Construct the small command-line interface used by new machines."""
    value = argparse.ArgumentParser(description="Install portable llm-wiki host configuration")
    value.add_argument("--runtime", help="absolute immutable runtime directory")
    value.add_argument("--wiki-root", help="absolute Wiki root; defaults to the iCloud Obsidian vault")
    value.add_argument("--config-source", help="shared knowledge-flow.json template")
    value.add_argument("--writer", action="store_true", help="enable automatic evidence intake")
    value.add_argument("--contributor", action="store_true", help="collect evidence without publishing")
    value.add_argument("--publisher", action="store_true", help="collect and publish as the designated machine")
    value.add_argument("--reader", action="store_true", help="disable automatic evidence intake")
    value.add_argument("--machine-id", help="stable machine identity for an exchange")
    value.add_argument('--materializer-machine-id', help='default page writer after coordinated activation')
    value.add_argument('--shared-writer-bootstrap-machine', help='previous owner that must release page ownership')
    value.add_argument("--allow-repository", action="append", default=[], metavar="PATH",
                       help="explicitly reopen one owned GitHub checkout previously excluded by path")
    value.add_argument("--dry-run", action="store_true", help="show changes without writing")
    value.add_argument("--event-driven", action="store_true", help="prepare the event worker; activation is a separate command")
    value.add_argument("--disable-event-worker", action="store_true", help="disable event processing in the generated config")
    value.add_argument("--event-worker-action", choices=("enable", "bootout", "disable", "status", "rollback"),
                       help="explicitly manage the prepared macOS LaunchAgent")
    value.add_argument("--event-worker-config", help="prepared private config for event-worker management")
    value.add_argument("--event-worker-launchctl", help="test or explicitly select launchctl executable")
    return value


def main(argv: list[str] | None = None) -> None:
    """CLI entry point with no implicit trust, scheduling, or Wiki writes."""
    args = parser().parse_args(argv)
    try:
        if args.event_worker_action:
            result = event_worker(args)
        else:
            if not args.runtime:
                raise ValueError("--runtime is required for installation")
            result = install(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.event_worker_action and result.get("success") is False:
            raise SystemExit(1)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"llm-wiki install failed: {error}") from error


if __name__ == "__main__":
    main()
