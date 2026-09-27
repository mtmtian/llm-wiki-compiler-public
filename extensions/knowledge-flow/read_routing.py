"""Resolve a safe Wiki read scope without weakening evidence intake rules."""

from pathlib import Path

import routing
from operational_context import is_operational_query


def _excluded(cwd, config):
    """Reject a read from an explicitly excluded path."""
    return any(
        routing.inside(cwd, path)
        for path in config.get("excludedPaths", [])
        if isinstance(path, str) and Path(path).is_absolute()
    )


def _preflight(cwd, config):
    """Reuse the write resolver's trusted repository and path checks."""
    if _excluded(cwd, config):
        return None, None, "excluded-path"
    root, identity = routing.git_identity(cwd)
    if root and not routing.eligible_repo(identity, config):
        return None, None, "third-party-or-unverified-repository"
    return root, identity, None


def _named_projects(prompt, config):
    """Return configured projects whose aliases occur in the prompt."""
    folded = str(prompt or "").casefold()
    return {
        project_id
        for project_id, project in config.get("projects", {}).items()
        if any(str(alias).casefold() in folded for alias in project.get("aliases", []))
    }


def _alias_families(prompt, config):
    """Group aliases that refer to the same set of configured domains."""
    folded = str(prompt or "").casefold()
    families = []
    for alias in {
        str(alias).casefold()
        for project in config.get("projects", {}).values()
        for alias in project.get("aliases", [])
        if str(alias).casefold() in folded
    }:
        group = {
            project_id
            for project_id, project in config.get("projects", {}).items()
            if any(str(value).casefold() == alias for value in project.get("aliases", []))
        }
        for family in families:
            if family & group:
                family.update(group)
                break
        else:
            families.append(set(group))
    return families


def _discriminated(project_id, prompt, config):
    """Check whether a shared alias has a platform or domain discriminator."""
    project = config["projects"][project_id]
    folded = str(prompt or "").casefold()
    required = project.get("requiredTerms", [])
    domain = project.get("topicTerms", [])
    return any(str(term).casefold() in folded for term in required) or (
        not required and any(str(term).casefold() in folded for term in domain)
    )


def _alias_route(prompt, config):
    """Resolve an alias, requiring a discriminator only among same-name domains."""
    families = _alias_families(prompt, config)
    if len(families) > 1:
        return False, "ambiguous-business-domains"
    named = families[0] if families else set()
    if not named:
        return None, None
    if len(named) == 1:
        project = next(iter(named))
        reason = "explicit-business-topic" if _discriminated(project, prompt, config) else "explicit-business-alias"
        return project, reason
    candidates = {project for project in named if _discriminated(project, prompt, config)}
    if len(candidates) == 1:
        return next(iter(candidates)), "explicit-business-topic"
    return False, "ambiguous-business-domains"


def _platform_route(scope, prompt, config):
    """Switch a bound domain only when its shared alias has one clear platform."""
    project = config.get("projects", {}).get(scope)
    if not project:
        return None, None
    aliases = {str(alias).casefold() for alias in project.get("aliases", [])}
    siblings = {
        project_id
        for project_id, item in config.get("projects", {}).items()
        if aliases & {str(alias).casefold() for alias in item.get("aliases", [])}
    }
    if len(siblings) <= 1:
        return None, None
    candidates = {item for item in siblings if _discriminated(item, prompt, config)}
    if len(candidates) == 1:
        selected = next(iter(candidates))
        return (selected, "explicit-business-topic") if selected != scope else (None, None)
    if len(candidates) > 1:
        return False, "ambiguous-business-domains"
    return None, None


def _binding_route(binding, current, prompt, config):
    """Reuse a trusted session binding for a read-only long follow-up."""
    if not binding:
        return None
    valid = binding in config.get("projects", {}) or str(binding).startswith("repo-")
    if not valid or (current and current != binding):
        return None
    if _named_projects(prompt, config):
        return None
    return binding


def resolve_read(cwd, prompt, binding, config):
    """Return ``(project, reason)`` for a scoped Wiki read.

    Scope identity is trusted from Git/path/alias/session binding first.  The
    existing ``routing.resolve`` remains the stricter evidence-intake gate.
    """
    prompt = str(prompt or "")
    root, identity, reason = _preflight(cwd, config)
    if reason:
        return None, reason
    mentioned, mention_reason, _ = routing._explicit_routes(prompt, config)
    alias, alias_reason = _alias_route(prompt, config)
    if alias is False:
        return None, alias_reason
    current = routing._cwd_project(cwd, root, identity, config)
    if current is False:
        return None, "ambiguous-business-domains"
    if mention_reason and mention_reason != "explicit-route-mention":
        if mention_reason != "explicit-path-unresolved" or not (mentioned or alias or current):
            return None, mention_reason
    if alias and mentioned and alias != mentioned:
        return None, "ambiguous-route-mentions"
    if alias and routing.GENERAL.search(prompt) and not _discriminated(alias, prompt, config):
        return None, "general-question"
    if alias:
        return alias, alias_reason
    if mentioned:
        return mentioned, mention_reason or "explicit-route-mention"
    switch, switch_reason = _platform_route(current or binding, prompt, config)
    if switch is False:
        return None, switch_reason
    if switch:
        return switch, switch_reason
    if routing.GENERAL.search(prompt):
        return None, "general-question"
    if current:
        return current, "owned-repository" if root else "business-workspace"
    if is_operational_query(prompt):
        return None, "operational-question"
    bound = _binding_route(binding, current, prompt, config)
    if bound:
        return bound, "business-continuation"
    return None, "unbound-workspace"


__all__ = ["resolve_read"]
