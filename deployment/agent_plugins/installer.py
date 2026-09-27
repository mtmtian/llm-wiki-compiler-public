"""Plan and install Claude/Pi adapters against one shared Wiki configuration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from ..agent_mcp import mcp_path, prepare_mcp_plan, server_spec
from .install_settings import (claude_settings, current_command, pi_settings,
                               read_json, registry_data, registration_record, unique_plan)
from .install_launcher import launcher_source
from .install_storage import (PlannedWrites, RUNTIME_FILES, Snapshot, ensure_bundle_root,
                              remember_read, restore_current, restore_files, snapshot,
                              switch_current, validate_current, verify_bundle, write_file_plan)

INSTALL_ROOT = Path(".local/share/llm-wiki-compiler/agent-plugins")
LAUNCHER_RELATIVE = Path(".local/bin/llmwiki-agent")
PACKAGE_FILES = tuple(sorted(RUNTIME_FILES))


def home_dir(args: Any) -> Path:
    """Resolve the home used for isolated installs and tests."""
    raw = getattr(args, "home", None) or os.environ.get("HOME") or str(Path.home())
    home = Path(raw).expanduser()
    if not home.is_absolute():
        raise ValueError("--home must be absolute")
    return home.resolve()


def config_file(args: Any, home: Path) -> Path:
    """Return the sole shared policy/runtime configuration path."""
    raw = getattr(args, "config", None) or str(home / ".config/llmwiki/knowledge-flow.json")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("--config must be absolute")
    return path.resolve()


def source_bundle() -> tuple[dict[str, bytes], dict[str, str], str, str]:
    """Hash the exact host runtime sources into one immutable bundle."""
    package = Path(__file__).resolve().parent
    content = {name: (package / name).read_bytes() for name in PACKAGE_FILES}
    hashes = {name: hashlib.sha256(value).hexdigest() for name, value in content.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest = json.dumps({"version": 1, "bundleHash": digest, "files": hashes},
                          ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return content, hashes, digest, manifest


def validate_root(root: Path) -> None:
    """Reject an unexpected bundle-root occupant before planning writes."""
    if root.is_symlink():
        raise ValueError("agent bundle root cannot be a symlink")
    if root.exists() and (not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700):
        raise ValueError("agent bundle root has unsafe type or permissions")


def selected_profiles(args: Any, host: str, home: Path) -> list[Path]:
    """Resolve explicit profile(s) or the native host's default profile."""
    values = getattr(args, "profile", None)
    if not values:
        default = os.environ.get("PI_CODING_AGENT_DIR", str(home / ".pi/agent")) if host == "pi" else str(home / ".claude")
        values = [default]
    profiles = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("--profile must be absolute")
        resolved = path.resolve()
        if resolved not in profiles:
            profiles.append(resolved)
    return profiles


def settings_path(args: Any, host: str, profile: Path,
                  previous: dict[str, Any] | None) -> Path:
    """Choose the current native settings path, preserving a Pi override."""
    if host == "claude":
        return profile / "settings.json"
    explicit = getattr(args, "pi_settings", None)
    registered = Path(previous["settings"]).resolve() if previous and previous.get("settings") else None
    requested = Path(explicit).expanduser().resolve() if explicit else None
    if registered and requested and requested != registered:
        raise ValueError("--pi-settings differs from the registered Pi settings path")
    raw = registered or requested or profile / "settings.json"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("--pi-settings must be absolute")
    return path.resolve()


def pointer_value(current: Path, root: Path) -> str | None:
    """Read and validate the current content-addressed bundle link."""
    digest = validate_current(current, root)
    return os.readlink(current) if digest and current.is_symlink() else None


def validate_config(config: Any) -> dict[str, Any]:
    """Require the shared workflow configuration shape used by the bridge."""
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("shared knowledge-flow config is missing or invalid")
    return config


def prepare_file_plan(args: Any) -> PlannedWrites:
    """Return a list-compatible complete plan plus planning-time read snapshots.

    Parent installers may append further owned file entries; keep the returned
    ``PlannedWrites`` object so its read/current guards survive until commit.
    """
    host, action = getattr(args, "host", None), getattr(args, "action", None)
    if host not in ("claude", "pi") or action not in ("install", "disable"):
        raise ValueError("install/disable requires --host claude|pi")
    home = home_dir(args)
    config_path = config_file(args, home)
    if (host == "pi" and action == "install"
            and config_path != home / ".config/llmwiki/knowledge-flow.json"):
        raise ValueError("Pi reads the shared default config; custom --config is not supported")
    read_set: dict[Path, Snapshot] = {}
    remember_read(read_set, config_path)
    config = validate_config(read_json(config_path))
    original_config = json.loads(json.dumps(config))
    registry, records = registry_data(config)
    profiles = selected_profiles(args, host, home)
    integration_root, launcher = home / INSTALL_ROOT, home / LAUNCHER_RELATIVE
    current, extension = integration_root / "current", integration_root / "current/pi-extension.ts"
    validate_root(integration_root)
    current_expected = pointer_value(current, integration_root) if action == "install" else None
    pointer = (current, current_expected) if action == "install" else None
    plan = PlannedWrites(read_set=read_set, current=pointer)
    if action == "install":
        append_bundle_plan(plan, integration_root, launcher, read_set)
    update_registrations(plan, args, host, action, profiles, config_path, home, extension,
                         integration_root, launcher, registry, records, read_set)
    if registry.get("registrations") or set(registry) - {"version", "registrations"}:
        config["agentPlugins"] = registry
    else:
        config.pop("agentPlugins", None)
    if config != original_config:
        plan.append((config_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", 0o600))
    unique = unique_plan(plan)
    return include_write_snapshots(unique, read_set, pointer)


def append_bundle_plan(plan: PlannedWrites, root: Path, launcher: Path,
                       read_set: dict[Path, Snapshot]) -> None:
    """Append validated immutable bundle and stable-launcher contents."""
    content, hashes, digest, manifest = source_bundle()
    target = root / digest
    if target.exists() or target.is_symlink():
        verify_bundle(target, hashes)
    plan.extend((target / name, data.decode("utf-8"), 0o600) for name, data in content.items())
    plan.append((target / "manifest.json", manifest, 0o600))
    remember_read(read_set, launcher)
    if read_set[launcher][0] and read_set[launcher][1] != launcher_source(root).encode():
        raise ValueError("stable launcher was modified")
    plan.append((launcher, launcher_source(root), 0o700))


def update_registrations(plan: PlannedWrites, args: Any, host: str, action: str,
                         profiles: list[Path], config_path: Path, home: Path,
                         extension: Path, integration_root: Path, launcher: Path,
                         registry: dict[str, Any], records: list[Any],
                         read_set: dict[Path, Snapshot]) -> None:
    """Merge exact MCP, native settings and registry edits into one plan."""
    for profile in profiles:
        previous = registration_record(records, host, config_path, profile)
        settings = settings_path(args, host, profile, previous)
        owned = bool(previous and previous.get("mcpOwned"))
        mcp_owned = add_mcp_plan(plan, read_set, host, profile, home, action, owned)
        native = native_settings(args, host, settings, profile, config_path, launcher,
                                 extension, integration_root, action, bool(previous), read_set)
        if native is not None:
            plan.append((settings, native, 0o600))
        if action == "install":
            upsert_registration(records, host, config_path, profile, settings, mcp_owned)
        else:
            remove_registration(records, host, config_path, profile)
    registry["registrations"] = records


def add_mcp_plan(plan: PlannedWrites, read_set: dict[Path, Snapshot], host: str,
                 profile: Path, home: Path, action: str, owned: bool) -> bool:
    """Plan MCP edits and record their exact read snapshot for lost-update safety."""
    target = mcp_path(host, profile, home)
    remember_read(read_set, target)
    result, mcp_owned = prepare_mcp_plan(host, profile, home, home / ".local/bin/llmwiki-local",
                                         action == "install", owned)
    if result is not None:
        plan.append(result)
    return mcp_owned


def native_settings(args: Any, host: str, settings: Path, profile: Path, config: Path,
                    launcher: Path, extension: Path, integration_root: Path,
                    action: str, previously_owned: bool,
                    read_set: dict[Path, Snapshot]) -> str | None:
    """Delegate JSON changes for the selected native host only."""
    installing = action == "install"
    if host == "claude":
        return claude_settings(settings, profile, config, launcher,
                               home_dir(args) / ".local/share/llm-wiki-compiler/integrations/claude",
                               installing, read_set)
    return pi_settings(settings, extension, installing, previously_owned, read_set)


def remove_registration(records: list[Any], host: str, config: Path, profile: Path) -> None:
    """Remove one exact owner record without disturbing other registrations."""
    records[:] = [item for item in records if not (item.get("host") == host
                  and item.get("config") == str(config) and item.get("profile") == str(profile))]


def upsert_registration(records: list[Any], host: str, config: Path, profile: Path,
                        settings: Path, mcp_owned: bool) -> None:
    """Update one registration in place to keep installs byte-idempotent."""
    replacement = {"host": host, "config": str(config), "profile": str(profile),
                   "settings": str(settings), "mcpOwned": mcp_owned}
    for index, item in enumerate(records):
        if (item.get("host"), item.get("config"), item.get("profile")) == (
                host, str(config), str(profile)):
            records[index] = replacement
            return
    records.append(replacement)


def include_write_snapshots(plan: PlannedWrites, read_set: dict[Path, Snapshot],
                            pointer: tuple[Path, str | None] | None) -> PlannedWrites:
    """Attach expected prior contents for every destination before returning."""
    final = PlannedWrites(read_set=read_set, current=pointer)
    for path, content, mode in plan:
        target = Path(os.path.abspath(path))
        prior = read_set.get(target)
        if prior is None:
            prior = snapshot(target)
            if prior[0] and prior != (True, content.encode(), mode):
                raise ValueError(f"immutable installer target was modified: {target}")
            read_set[target] = prior
        final.append((target, content, mode))
    return final


def verify_planning_reads(plan: PlannedWrites) -> None:
    """Abort if an input changed between plan construction and commit."""
    for path, expected in plan.read_set.items():
        if snapshot(path) != expected:
            raise RuntimeError(f"configuration changed after planning: {path}")
    if plan.current_expected is not None:
        current, expected = plan.current_expected
        if expected is None:
            if current.exists() or current.is_symlink():
                raise RuntimeError("agent current changed after planning")
        elif not current.is_symlink() or os.readlink(current) != expected:
            raise RuntimeError("agent current changed after planning")


def change_report(plan: PlannedWrites) -> list[dict[str, str]]:
    """Summarize exact file changes without creating or modifying paths."""
    rows = []
    for path, content, mode in plan:
        old = snapshot(path)
        if old != (True, content.encode(), mode):
            rows.append({"path": str(path), "action": "update" if old[0] else "create"})
    return rows


def install(args: Any, file_plan: PlannedWrites | None = None) -> dict[str, Any]:
    """Install/disable selected adapters with private backups and rollback."""
    plan = file_plan if file_plan is not None else prepare_file_plan(args)
    verify_planning_reads(plan)
    changes = change_report(plan)
    previous = plan.current_expected[1] if plan.current_expected else None
    home = home_dir(args)
    root = home / INSTALL_ROOT
    current, action = root / "current", args.action
    if getattr(args, "dry_run", False):
        append_pointer_change(changes, action, root, previous)
        return {"dryRun": True, "changes": changes, "backup": None}
    if action == "install":
        ensure_bundle_root(root)
    backup, initial, expected_after = write_file_plan(plan, home)
    switched, digest = False, ""
    try:
        if action == "install":
            _, _, digest, _ = source_bundle()
            if previous != digest:
                switch_current(current, root, digest, previous)
                switched = True
    except Exception:
        rollback_install(current, root, previous, switched, digest, expected_after, initial)
        raise
    append_pointer_change(changes, action, root, previous)
    return {"dryRun": False, "changes": changes, "backup": str(backup) if backup else None}


def rollback_install(current: Path, root: Path, previous: str | None, switched: bool, digest: str,
                     expected_after: dict[Path, Snapshot], initial: dict[Path, Snapshot]) -> None:
    """Restore pointer and owned files only while they still match our writes."""
    if switched:
        restore_current(current, root, previous, digest)
    restore_files(initial, list(initial), expected_after)


def append_pointer_change(changes: list[dict[str, str]], action: str,
                          root: Path, previous: str | None) -> None:
    """Add a planned atomic bundle switch to the user-facing change report."""
    if action == "install":
        _, _, digest, _ = source_bundle()
        if previous != digest:
            changes.append({"path": str(root / "current"), "action": "switch"})


def claude_registered(settings: Any, record: dict[str, Any], home: Path) -> bool:
    """Check exact stable commands for both configured Claude events."""
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        return False
    profile, config = Path(record["profile"]), Path(record["config"])
    command = current_command(home / LAUNCHER_RELATIVE, config, profile)
    for event in ("UserPromptSubmit", "Stop"):
        groups = settings["hooks"].get(event, [])
        matches = [entry for group in groups if isinstance(group, dict)
                   and isinstance(group.get("hooks"), list) for entry in group["hooks"]
                   if isinstance(entry, dict) and entry.get("command") == command]
        if len(matches) != 1:
            return False
    return True


def adapter_configured(item: dict[str, Any], home: Path) -> bool:
    """Read a registration's native settings and check the exact adapter entry."""
    settings_path_value = Path(item.get("settings", ""))
    settings = read_json(settings_path_value, {}) if settings_path_value.is_absolute() else {}
    if item.get("host") == "claude":
        return claude_registered(settings, item, home)
    target = str(home / INSTALL_ROOT / "current/pi-extension.ts")
    return isinstance(settings, dict) and settings.get("extensions", []).count(target) == 1


def mcp_configured(item: dict[str, Any], home: Path) -> bool:
    """Check the shared MCP launcher entry currently registered by the host."""
    path = mcp_path(item["host"], Path(item["profile"]), home)
    current = read_json(path, {})
    servers = current.get("mcpServers", {}) if isinstance(current, dict) else {}
    return isinstance(servers, dict) and servers.get("llmwiki") == server_spec(home / ".local/bin/llmwiki-local")


def status(args: Any) -> dict[str, Any]:
    """Report configured host entries separately from unverified delivery."""
    home = home_dir(args)
    config = validate_config(read_json(config_file(args, home)))
    _, records = registry_data(config)
    host_filter, rows = getattr(args, "host", None), []
    for item in records:
        if item.get("host") not in ("claude", "pi") or (host_filter and item["host"] != host_filter):
            continue
        configured = adapter_configured(item, home)
        rows.append({"host": item["host"], "config": item.get("config"), "profile": item.get("profile"),
                     "enabled": configured, "configured": configured, "mcpConfigured": mcp_configured(item, home),
                     "runtimeDelivery": "unverified"})
    return {"registrations": rows, "runtimeDelivery": "unverified"}
