"""Prepare native MCP registrations without copying Wiki policy or credentials.

The agent installer includes these JSON edits in its existing transaction and
records ownership in the shared registry. Unowned native servers are never
adopted or overwritten, even when they happen to use the same command.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def mcp_path(host: str, profile: Path, home: Path) -> Path:
    """Resolve the native user MCP file for a verified host/profile pair."""
    if host == "claude":
        return home / ".claude.json" if profile == home / ".claude" else profile / ".claude.json"
    if host == "pi":
        return profile / "mcp.json"
    raise ValueError("unsupported MCP host")


def server_spec(launcher: Path) -> dict[str, Any]:
    """Use the existing shared launcher, which selects the current Wiki root."""
    return {"type": "stdio", "command": str(launcher), "args": ["serve"]}


def update_servers(original: dict[str, Any], expected: dict[str, Any],
                   installing: bool, previously_owned: bool) -> tuple[dict[str, Any], bool]:
    """Merge exactly one owned server; preserve user replacements on disable."""
    result = copy.deepcopy(original)
    servers = result.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("native mcpServers must be an object")
    existing = servers.get("llmwiki")
    if installing:
        if "llmwiki" in servers and (not previously_owned or existing != expected):
            raise ValueError("llmwiki MCP name already exists outside this registration")
        result.setdefault("mcpServers", {})["llmwiki"] = expected
        return result, True
    if previously_owned and existing == expected:
        del servers["llmwiki"]
    return result, False


def prepare_mcp_plan(host: str, profile: Path, home: Path, launcher: Path,
                     installing: bool, previously_owned: bool
                     ) -> tuple[tuple[Path, str, int] | None, bool]:
    """Return one transaction entry plus registry ownership, without any writes."""
    target = mcp_path(host, profile, home)
    if installing and not launcher.is_file():
        raise ValueError("shared llmwiki-local launcher is missing")
    if not target.exists() and not installing:
        return None, False
    original = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    if not isinstance(original, dict):
        raise ValueError("native MCP configuration must be an object")
    updated, owned = update_servers(original, server_spec(launcher), installing, previously_owned)
    if updated == original:
        return None, owned
    return (target, json.dumps(updated, ensure_ascii=False, indent=2) + "\n", 0o600), owned
