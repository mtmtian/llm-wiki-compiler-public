"""Resolve owned repositories and explicit business domains before retrieval.

An ownership allowlist is independent of semantic relevance. Unknown GitHub
repositories require a cached or live read-only metadata check; failures deny
automatic collection rather than assuming ownership or business relevance.
"""

import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from common import inside, load_json, save_json
from route_mentions import parse_route_mentions

GENERAL = re.compile(r"天气|菜谱|买什么|推荐.*(?:餐厅|耳机|手机)|翻译(?:一下|这句)|translate\b|what time|^(?:解释|介绍)(?:一下)?(?:Python|JavaScript|什么是)", re.I)
# Continuation matching is intentionally finite.  A short follow-up can reuse
# a session binding; an arbitrary long prompt must establish its own identity.
CONTINUATION = re.compile(
    r"^(?:继续|可以|好|好的|行|按这个|照这个|就这样|修一下|再试|确认|同意|再来一个|再来一个吧|为什么|为什么呢|不对|改一下|改一下吧|这个呢|是|是的|是吗|continue|yes|ok)[。.!！?？]?\s*$",
    re.I,
)
WORK = re.compile(r"项目|业务|代码|实现|修复|部署|测试|接口|架构|约束|决策|增长|投放|素材|归因|复盘|报告|预算|转化|留存|回收|ROAS|campaign|bug|fix|implement|repo|release", re.I)


def run(args, cwd=None, timeout=2):
    """Read repository metadata without a shell or interactive credential flow."""
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, check=True).stdout.strip()


def remote_identity(url):
    """Parse a GitHub remote or repository page without accepting lookalike hosts."""
    match = re.fullmatch(r"git@github\.com:([^/\s]+/[^/\s]+)", url)
    if match:
        name = match.group(1)
    else:
        parsed = urlsplit(url)
        if parsed.hostname != "github.com" or parsed.scheme not in ("https", "ssh"):
            return None
        name = parsed.path.strip("/")
        parts = name.split("/", 2)
        if len(parts) == 3:
            if not re.fullmatch(r"(?:(?:pull|issues)/[1-9]\d*(?:/(?:files|commits))?"
                                r"|(?:tree|blob)/[^\s]+|commit/[0-9a-fA-F]{7,40})", parts[2]):
                return None
            name = "/".join(parts[:2])
    name = re.sub(r"\.git$", "", name)
    return name.lower() if re.fullmatch(r"[\w.-]+/[\w.-]+", name) else None


def git_identity(cwd):
    """Return the worktree's remote identity; a repo with no origin stays ineligible."""
    try:
        root = run(["git", "rev-parse", "--show-toplevel"], cwd)
    except (subprocess.SubprocessError, OSError):
        return None, None
    try:
        remote = run(["git", "remote", "get-url", "origin"], root)
        return root, remote_identity(remote)
    except (subprocess.SubprocessError, OSError):
        return root, None


def eligible_repo(identity, config, allow_archived=False):
    """Only known owners and non-forks qualify, except explicit working-fork overrides.

    Archived repositories accept no new work; ``allow_archived`` is only for
    reading knowledge that was published while the repository was active.
    """
    identity = str(identity or "").lower()
    excluded = {str(value).lower() for value in config.get("excludedRepos", [])}
    working_forks = {str(value).lower() for value in config.get("workingForks", [])}
    owners = {str(value).lower() for value in config.get("owners", [])}
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", identity) or identity in excluded:
        return False
    if identity in working_forks:
        return True
    if identity.split("/")[0] not in owners:
        return False
    cache_path = Path(config["stateDir"]) / "repo-metadata.json"
    cache = load_json(cache_path, {})
    if time.time() - cache.get(identity, {}).get("checkedAt", 0) > 86400:
        try:
            metadata = json.loads(run([config.get("gh", "gh"), "api", "repos/" + identity], timeout=3))
            cache[identity] = {"fork": metadata["fork"], "archived": metadata["archived"], "checkedAt": time.time()}
            save_json(cache_path, cache)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError):
            return False
    return cache[identity].get("fork") is False and (allow_archived or not cache[identity].get("archived", True))


def topic_project(prompt, config):
    """Require both an explicit business identity and a domain-specific task signal."""
    text = str(prompt or "")
    folded = text.casefold()
    matches = []
    for key, project in config["projects"].items():
        aliases = project.get("aliases", [])
        terms = project.get("topicTerms", [])
        required = project.get("requiredTerms", [])
        named = any(str(alias).casefold() in folded for alias in aliases)
        domain = any(str(term).casefold() in folded for term in terms)
        platform = not required or any(str(term).casefold() in folded for term in required)
        if named and domain and platform:
            matches.append(key)
    return matches[0] if len(matches) == 1 else False if len(matches) > 1 else None


def is_continuation(prompt):
    """Recognize only a bounded natural short follow-up."""
    value = str(prompt or "").strip()
    return len(value) <= 32 and bool(CONTINUATION.fullmatch(value))


def _existing_path(raw):
    """Resolve a mentioned path, treating an existing file as its parent."""
    try:
        path = Path(raw)
        if not path.is_absolute() or not path.exists():
            return None
        resolved = path.resolve()
        return resolved.parent if resolved.is_file() else resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _project_paths(config):
    """Yield configured project path mappings without expanding placeholders."""
    for project_id, project in config.get("projects", {}).items():
        for raw in project.get("paths", []):
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                continue
            yield project_id, raw


def _projects_for_path(raw, config):
    """Map an existing explicit path only to configured boundaries."""
    target = _existing_path(raw)
    if target is None:
        return set(), None
    matches = [(len(str(configured)), project_id) for project_id, configured in _project_paths(config)
               if inside(target, configured)]
    if not matches:
        return set(), target
    longest = max(length for length, _ in matches)
    return {project_id for length, project_id in matches if length == longest}, target


def _project_for_identity(identity, config):
    """Return the unique configured project for a verified repo identity."""
    folded = str(identity or "").casefold()
    matches = {project_id for project_id, project in config.get("projects", {}).items()
               if any(str(repo).casefold() == folded for repo in project.get("repos", []))}
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        return False
    return "repo-" + folded.replace("/", "-") if folded else None


def _bare_repo_projects(prompt, config):
    """Use an exact configured repo basename only when it is unambiguous."""
    value = str(prompt or "").strip()
    matches = {}
    for project_id, project in config.get("projects", {}).items():
        for repo in project.get("repos", []):
            identity = str(repo).casefold()
            if "/" not in identity:
                continue
            basename = identity.rsplit("/", 1)[1]
            if re.search(rf"(?<![\w.-]){re.escape(basename)}(?![\w.-])", value, re.IGNORECASE):
                matches.setdefault(basename, set()).add((project_id, identity))
    projects = {project_id for values in matches.values() for project_id, _ in values}
    if len(projects) != 1:
        return None, "ambiguous-route-mentions" if projects else None
    pairs = {pair for values in matches.values() for pair in values}
    project_id = next(iter(projects))
    if any(not eligible_repo(identity, config) for _, identity in pairs):
        return None, "third-party-or-unverified-repository"
    return project_id, "explicit-repository-name"


def _repo_for_path(target, config):
    """Inspect an explicit path's Git identity and apply the ownership gate."""
    root, identity = git_identity(str(target))
    if not root:
        return None, "explicit-path-unresolved"
    if not eligible_repo(identity, config):
        return None, "third-party-or-unverified-repository"
    project = _project_for_identity(identity, config)
    return project, "explicit-repository"


def _identity_routes(urls, config):
    """Resolve GitHub URL mentions after strict identity and ownership checks."""
    candidates = set()
    for url in urls:
        identity = remote_identity(url)
        if not identity or not eligible_repo(identity, config):
            return None, "third-party-or-unverified-repository"
        project = _project_for_identity(identity, config)
        if project is False:
            return None, "ambiguous-route-mentions"
        if project:
            candidates.add(project)
    return candidates, None


def _path_routes(paths, config):
    """Resolve existing absolute paths to configured projects or repo IDs."""
    candidates = set()
    unresolved = False
    for raw in paths:
        projects, target = _projects_for_path(raw, config)
        if len(projects) > 1:
            return None, "ambiguous-route-mentions"
        if target is not None and any(
                isinstance(path, str) and Path(path).is_absolute() and inside(target, path)
                for path in config.get("excludedPaths", [])):
            return None, "excluded-path"
        if projects:
            root, identity = git_identity(str(target))
            if root and not eligible_repo(identity, config):
                return None, "third-party-or-unverified-repository"
            candidates.update(projects)
            continue
        if target is None:
            unresolved = True
            continue
        project, reason = _repo_for_path(target, config)
        if reason == "explicit-path-unresolved":
            unresolved = True
            continue
        if project is None:
            return None, reason
        candidates.add(project)
    if unresolved and not candidates:
        return None, "explicit-path-unresolved"
    return candidates, None


def _explicit_routes(prompt, config):
    """Resolve explicit path/identity mentions into one safe route."""
    mentions = parse_route_mentions(prompt)
    candidates, reason = _identity_routes(mentions.github_urls, config)
    if reason:
        return None, reason, True
    path_candidates, reason = _path_routes(mentions.paths, config)
    if reason and (reason != "explicit-path-unresolved" or not candidates):
        return None, reason, True
    if not reason:
        candidates.update(path_candidates)

    # A bare repository name is accepted only for a unique, configured
    # basename.  This never creates a new repo route from arbitrary prompt
    # words.
    bare_project, bare_reason = _bare_repo_projects(prompt, config)
    if bare_reason:
        if bare_project:
            candidates.add(bare_project)
        elif not candidates:
            return None, bare_reason, True

    if len(candidates) > 1:
        return None, "ambiguous-route-mentions", True
    if candidates:
        return next(iter(candidates)), "explicit-route-mention", True
    return None, None, False


def _cwd_project(cwd, root, identity, config):
    """Return the default project implied by the current cwd."""
    if root:
        return _project_for_identity(identity, config)
    paths = [(len(path), key) for key, item in config.get("projects", {}).items()
             for path in item.get("paths", [])
             if isinstance(path, str) and Path(path).is_absolute() and inside(cwd, path)]
    return max(paths)[1] if paths else None


def _routes_agree(*projects):
    """Check explicit route candidates without treating cwd as a conflict."""
    values = {value for value in projects if value and value is not False}
    return len(values) <= 1


def resolve_repo_identity(cwd, prompt, project, config):
    """Find a verified repo identity supporting a resolved project route.

    Only fixed git metadata calls and the strict GitHub parser are used; no
    prompt text is ever passed to a shell.  A single identity is returned, and
    conflicting or unverified identities return ``None``.
    """
    identities = set()
    root, identity = git_identity(cwd)
    if root and identity and eligible_repo(identity, config):
        if _project_for_identity(identity, config) == project:
            identities.add(identity.lower())
    mentions = parse_route_mentions(prompt)
    for url in mentions.github_urls:
        identity = remote_identity(url)
        if identity and eligible_repo(identity, config) and _project_for_identity(identity, config) == project:
            identities.add(identity)
    for raw in mentions.paths:
        _, target = _projects_for_path(raw, config)
        if target is None:
            continue
        _, identity = git_identity(str(target))
        if identity and eligible_repo(identity, config) and _project_for_identity(identity, config) == project:
            identities.add(identity.lower())
    return next(iter(identities)) if len(identities) == 1 else None


def _bound_route(binding, prompt, config, current):
    """Reuse a configured project or stable repo binding for short follow-ups."""
    if not binding or (binding not in config.get("projects", {}) and not str(binding).startswith("repo-")):
        return None
    if str(binding).startswith("repo-"):
        return binding if is_continuation(prompt) else None
    project = config["projects"][binding]
    folded = prompt.casefold()
    other_named = any(str(alias).casefold() in folded for key, item in config["projects"].items()
                      if key != binding for alias in item.get("aliases", []))
    domain = any(str(term).casefold() in folded for term in project.get("topicTerms", []))
    if not other_named and (is_continuation(prompt) or domain) and (not current or current == binding):
        return refined_domain(binding, prompt, config)
    return None


def _preflight(cwd, prompt, config):
    """Apply cwd exclusion, repository eligibility, and general filtering."""
    if any(inside(cwd, path) for path in config.get("excludedPaths", [])
           if isinstance(path, str) and Path(path).is_absolute()):
        return None, None, "excluded-path"
    root, identity = git_identity(cwd)
    if root and not eligible_repo(identity, config):
        return None, None, "third-party-or-unverified-repository"
    if GENERAL.search(prompt):
        return None, None, "general-question"
    return root, identity, None


def resolve(cwd, prompt, binding, config):
    """Resolve one turn, never treating a broad daily workspace as a business domain."""
    prompt = str(prompt or "")
    root, identity, reason = _preflight(cwd, prompt, config)
    if reason:
        return None, reason

    # Explicit path/identity references are sufficient to route a non-project
    # cwd.  They remain bounded to configured paths or an eligible repository.
    mentioned, mention_reason, _ = _explicit_routes(prompt, config)
    explicit = topic_project(prompt, config)
    if explicit is False:
        return None, "ambiguous-business-domains"
    current = _cwd_project(cwd, root, identity, config)
    if current is False:
        return None, "ambiguous-business-domains"
    # API routes and future filenames can look like absolute paths. They cannot
    # create a scope, but should not erase one already independently verified.
    if mention_reason and mention_reason != "explicit-route-mention":
        if mention_reason != "explicit-path-unresolved" or not (current or explicit):
            return None, mention_reason
    # cwd is a default hint; only independently explicit routes must agree.
    if not _routes_agree(explicit, mentioned):
        return None, "ambiguous-route-mentions"
    if mentioned:
        return mentioned, mention_reason or "explicit-route-mention"
    if explicit:
        return explicit, "explicit-business-topic"
    if current and (root or WORK.search(prompt) or is_continuation(prompt)):
        refined = refined_domain(current, prompt, config)
        if refined is None and current not in config.get("projects", {}):
            refined = current
        return refined, "owned-repository" if root else "business-workspace"
    bound = _bound_route(binding, prompt, config, current)
    if bound:
        return bound, "business-continuation"
    return None, "unbound-workspace"


def refined_domain(project_id, prompt, config):
    """A named platform switch can change domains inside a bound business session."""
    if not project_id or project_id not in config.get("projects", {}):
        return None
    aliases = config["projects"][project_id].get("aliases", [])
    if not aliases:
        return project_id
    explicit = topic_project(aliases[0] + " " + prompt, config)
    if explicit is False:
        return None
    return explicit or project_id
