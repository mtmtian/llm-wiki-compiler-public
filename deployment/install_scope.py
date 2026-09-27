"""Perform explicit, narrow migrations of stale repository exclusions.

Older installers excluded their own checkout by path and repository identity.
Those entries are retained as local configuration until the operator names the
exact checkout to reopen.  This module never infers a broad directory or
removes an exclusion merely because it happens to contain a Git repository.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


GITHUB_IDENTITY = re.compile(r"^[\w.-]+/[\w.-]+$")


def _git_output(arguments: list[str], cwd: Path) -> str:
    """Read one Git value without invoking a shell or interactive prompts."""
    try:
        result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True,
                                check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"repository is not a readable Git checkout: {cwd}") from error
    return result.stdout.strip()


def _github_identity(remote: str) -> str | None:
    """Normalize HTTPS and SCP-style GitHub remotes to ``owner/repo``."""
    match = re.fullmatch(r"git@github\.com:([^/\s]+/[^/\s]+)", remote)
    name = match.group(1) if match else ""
    if not name:
        parsed = urlsplit(remote)
        if parsed.hostname != "github.com" or parsed.scheme not in ("https", "ssh"):
            return None
        name = parsed.path.strip("/")
    name = re.sub(r"\.git$", "", name).lower()
    return name if GITHUB_IDENTITY.fullmatch(name) else None


def repository_info(raw_path: str) -> tuple[Path, str]:
    """Resolve an exact Git worktree root and its GitHub origin identity."""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() or not candidate.is_dir():
        raise ValueError(f"--allow-repository must name an existing absolute directory: {raw_path}")
    root = Path(_git_output(["git", "-C", str(candidate), "rev-parse", "--show-toplevel"], candidate))
    root = root.expanduser().resolve()
    if root != candidate.resolve():
        raise ValueError(f"--allow-repository must name the Git checkout root: {candidate}")
    remote = _git_output(["git", "-C", str(root), "remote", "get-url", "origin"], root)
    identity = _github_identity(remote)
    if identity is None:
        raise ValueError(f"repository origin is not a GitHub repository: {root}")
    return root, identity


def _is_inside(path: Path, parent: Path) -> bool:
    """Return true for a path equal to or below a protected directory."""
    return path == parent or parent in path.parents


def _protected_roots(config: dict[str, Any]) -> list[Path]:
    """Find runtime, state, replica, and shared exchange roots that stay closed."""
    values = [config.get("stateDir"), config.get("wikiRoot"), config.get("sharedWikiRoot")]
    exchange = config.get("exchange")
    if isinstance(exchange, dict):
        values.append(exchange.get("root"))
    worker = config.get("worker")
    if isinstance(worker, str):
        values.append(str(Path(worker).parent.parent))
    roots = []
    for value in values:
        if not isinstance(value, str) or not Path(value).is_absolute():
            continue
        roots.append(Path(value).expanduser().resolve())
    return roots


def _identity_allowed(identity: str, config: dict[str, Any]) -> bool:
    """Require the reopened repository to pass the configured ownership policy."""
    owners = {str(item).casefold() for item in config.get("owners", [])}
    working_forks = {str(item).casefold() for item in config.get("workingForks", [])}
    return identity in working_forks or identity.split("/", 1)[0] in owners


def _validate_repositories(config: dict[str, Any], raw_paths: list[str]) -> list[tuple[Path, str]]:
    """Resolve requested roots and enforce ownership plus protected-root gates."""
    repositories = [repository_info(value) for value in raw_paths]
    protected = _protected_roots(config)
    for root, identity in repositories:
        if any(_is_inside(root, parent) or _is_inside(parent, root) for parent in protected):
            raise ValueError(f"--allow-repository cannot reopen runtime/state path: {root}")
        if not _identity_allowed(identity, config):
            raise ValueError(f"repository is outside configured owners/workingForks: {identity}")
    return repositories


def _remove_path_exclusions(config: dict[str, Any], allowed_roots: set[Path]) -> list[str]:
    """Remove only excluded paths that resolve exactly to an allowed root."""
    excluded_paths = config.get("excludedPaths", [])
    if not isinstance(excluded_paths, list):
        raise ValueError("knowledge-flow exclusions must be lists")
    removed = []
    kept = []
    for raw in excluded_paths:
        try:
            same = Path(raw).expanduser().is_absolute() and Path(raw).expanduser().resolve() in allowed_roots
        except (OSError, RuntimeError, TypeError):
            same = False
        (removed if same else kept).append(str(raw) if same else raw)
    config["excludedPaths"] = kept
    return removed


def _remove_repo_exclusions(config: dict[str, Any], allowed_ids: set[str]) -> list[str]:
    """Remove excluded repository identities only when explicitly authorized."""
    excluded_repos = config.get("excludedRepos", [])
    if not isinstance(excluded_repos, list):
        raise ValueError("knowledge-flow exclusions must be lists")
    removed = [str(raw) for raw in excluded_repos if str(raw).casefold() in allowed_ids]
    config["excludedRepos"] = [raw for raw in excluded_repos if str(raw).casefold() not in allowed_ids]
    return removed


def migrate_repository_exclusions(config: dict[str, Any], raw_paths: list[str] | None) -> dict[str, list[str]]:
    """Remove only exact, explicitly authorized repository exclusions.

    Protected runtime/state roots are rejected even when somebody initialized a
    Git repository inside them.  Parent exclusions and unrelated user paths are
    left untouched.
    """
    requested = [str(value) for value in (raw_paths or []) if str(value).strip()]
    if not requested:
        return {"paths": [], "repos": []}
    repositories = _validate_repositories(config, requested)
    allowed_roots = {root for root, _ in repositories}
    allowed_ids = {identity.casefold() for _, identity in repositories}
    return {"paths": _remove_path_exclusions(config, allowed_roots),
            "repos": _remove_repo_exclusions(config, allowed_ids)}


__all__ = ["migrate_repository_exclusions", "repository_info"]
