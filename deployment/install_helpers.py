"""Small installer helpers kept separate from policy and event lifecycle code."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


def own_hook(entry: Any, config_path: Path) -> bool:
    """Identify only this installer's hook command for the exact config."""
    if not isinstance(entry, dict) or entry.get("type") != "command" or not isinstance(entry.get("command"), str):
        return False
    try:
        tokens = shlex.split(entry["command"])
    except ValueError:
        return False
    if len(tokens) != 4 or not Path(tokens[0]).name.startswith("python") or tokens[2] != "--config":
        return False
    return (Path(tokens[3]).resolve() == config_path.resolve()
            and Path(tokens[1]).name == "hooks.py" and Path(tokens[1]).parent.name == "knowledge-flow")


def hook_command(runtime: Path, config_path: Path, python: str | None = None) -> str:
    """Build the synchronous command used by both native hooks."""
    return shlex.join([python or sys.executable, str(runtime / "knowledge-flow/hooks.py"), "--config", str(config_path)])


def redact(path: Path, variables: dict[str, str]) -> str:
    """Render dry-run paths without exposing absolute host locations."""
    value = str(path)
    for key in ("RUNTIME", "REPO", "WIKI_ROOT", "HOME"):
        value = value.replace(variables[key], f"${key}")
    return value


def render_launcher(environment: dict[str, str], binary: Path, root: Path | None = None) -> str:
    """Render the private compiler launcher and optional local replica root."""
    lines = ["#!/bin/sh", "set -eu",
             'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"']
    lines.extend(f"export {key}={shlex.quote(value)}" for key, value in environment.items())
    if root is not None:
        lines.append(f"export LLMWIKI_ROOT={shlex.quote(str(root))}")
    lines.append(f"export LLMWIKI_BIN={shlex.quote(str(binary))}")
    return "\n".join(lines) + "\n"


def render_local(environment_path: Path, node: str, require_root: bool = False) -> str:
    """Render a local launcher, optionally failing closed before replica bootstrap."""
    lines = ["#!/bin/sh", "set -eu", f"CONFIG={shlex.quote(str(environment_path))}", ". \"$CONFIG\""]
    if require_root:
        lines.extend([': "${LLMWIKI_ROOT:?LLMWIKI_ROOT is not configured}"',
                      'if [ ! -d "$LLMWIKI_ROOT" ]; then',
                      '  echo "llm-wiki local root is not initialized: $LLMWIKI_ROOT" >&2; exit 1',
                      "fi", 'cd "$LLMWIKI_ROOT"'])
    lines.append(f"exec {shlex.quote(node)} \"$LLMWIKI_BIN\" \"$@\"")
    lines.append("")
    return "\n".join(lines)


def render_maintenance(config_path: Path, python: str) -> str:
    """Render a maintenance launcher following the configured runtime."""
    py, cfg = shlex.quote(python), shlex.quote(str(config_path))
    return "\n".join(("#!/bin/sh", "set -eu", f"CONFIG={cfg}",
        f"WORKER=\"$({py} -c 'import json,sys; print(json.load(open(sys.argv[1]))[\"worker\"])' \"$CONFIG\")\"",
        f"exec {py} \"$(dirname \"$WORKER\")/maintenance.py\" --config \"$CONFIG\" \"$@\"", ""))


def render_alma_session(runtime: Path, config_path: Path, python: str) -> str:
    """Render an Alma bridge launcher pinned to the immutable runtime."""
    return "\n".join(("#!/bin/sh", "set -eu", f"exec {shlex.quote(python)} "
                         f"{shlex.quote(str(runtime / 'alma-session.py'))} "
                         f"--config {shlex.quote(str(config_path))} \"$@\"", ""))


def stage(path: Path, content: str, mode: int) -> Path:
    """Stage one private file in its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".llmwiki-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.chmod(temporary, mode)
    return Path(temporary)


def needs_update(path: Path, content: str, mode: int) -> bool:
    """Compare bytes and private permission bits before backing up a file."""
    if not path.exists():
        return True
    try:
        return path.read_text(encoding="utf-8") != content or (path.stat().st_mode & 0o777) != mode
    except (OSError, UnicodeError):
        return True


def write_plan(plan: list[tuple[Path, str, int]], backup_root: Path, home: Path) -> str | None:
    """Atomically commit a staged plan and back up only changed files."""
    staged: list[tuple[Path, Path]] = []
    try:
        for path, content, mode in plan:
            staged.append((path, stage(path, content, mode)))
        changed = [(path, temporary) for (path, temporary), (_, content, mode) in zip(staged, plan)
                   if needs_update(path, content, mode)]
        if not changed:
            return None
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_root / stamp
        backup.mkdir(parents=True, exist_ok=False, mode=0o700)
        for path, _ in changed:
            if path.exists():
                destination = backup / path.relative_to(home)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(path, destination)
        for path, temporary in changed:
            os.replace(temporary, path)
        return str(backup)
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)


def validate_runtime(args: argparse.Namespace, required_files: tuple[str, ...],
                     validate_manifest: Callable[[Path, tuple[str, ...]], None],
                     find_binary: Callable[[str], str], check_node: Callable[[str], None]) -> tuple[Path, str, str]:
    """Validate an immutable runtime and resolve required host executables."""
    runtime_raw = Path(args.runtime)
    if not runtime_raw.is_absolute():
        raise ValueError("--runtime must be an absolute path")
    runtime = runtime_raw.resolve()
    if not runtime.is_dir() or any(not (runtime / item).is_file() for item in required_files):
        raise ValueError("runtime is missing required built files")
    validate_manifest(runtime, required_files)
    if "intakeEnabled" not in (runtime / "knowledge-flow/hooks.py").read_text(encoding="utf-8"):
        raise ValueError("runtime does not support intakeEnabled; build a current runtime")
    node, gh = find_binary("node"), find_binary("gh")
    check_node(node)
    return runtime, node, gh


def select_wiki(args: argparse.Namespace, machine: dict[str, Any], existing: dict[str, Any] | None,
                home: Path, expand_value: Callable[[Any, dict[str, str]], Any]) -> Path:
    """Select shared Wiki root, preferring v2 sharedWikiRoot on upgrades."""
    default = home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/llm wiki"
    candidate = args.wiki_root or machine.get("wikiRoot")
    old_exchange = existing.get("exchange") if existing else None
    if candidate is None and isinstance(old_exchange, dict) and old_exchange.get("protocolVersion", 1) == 2:
        candidate = existing.get("sharedWikiRoot")
    candidate = candidate or (existing or {}).get("wikiRoot") or str(default)
    if not isinstance(candidate, str):
        raise ValueError("Wiki root must be a string")
    path = Path(expand_value(candidate, {"HOME": str(home)}))
    if not path.is_absolute():
        raise ValueError("--wiki-root must be an absolute path")
    path = path.resolve()
    if not path.is_dir():
        raise ValueError("Wiki root does not exist")
    return path


def load_inputs(args: argparse.Namespace, home: Path, directory: Path,
                read_json: Callable[[Path, Any], Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None,
                                                               dict[str, Any], Path]:
    """Load shared template, machine overrides, and existing private files."""
    source_path = Path(args.config_source).expanduser() if args.config_source else directory / "knowledge-flow.json"
    source = read_json(source_path.resolve(), None)
    if not isinstance(source, dict):
        raise ValueError("knowledge-flow.json template is missing or invalid")
    machine = read_json(home / ".config/llmwiki/machine.json", {}) or {}
    if not isinstance(machine, dict):
        raise ValueError("machine.json must be an object")
    config_path = home / ".config/llmwiki/knowledge-flow.json"
    existing = read_json(config_path, None)
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("existing knowledge-flow.json must be an object")
    hooks = read_json(home / ".codex/hooks.json", {"hooks": {}}) or {"hooks": {}}
    if not isinstance(hooks, dict) or not isinstance(hooks.get("hooks", {}), dict):
        raise ValueError("existing hooks.json must contain an object hooks field")
    return source, machine, existing, hooks, config_path
