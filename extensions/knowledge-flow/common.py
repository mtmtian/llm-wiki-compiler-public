"""Private local state and bounded text handling for the knowledge workflow.

Portable policy is versioned separately in the private deployment directory.
Runtime configuration and event records are kept outside the synchronized wiki.
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def load_json(path, default=None):
    """Load local JSON without turning corruption into permissive defaults."""
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path, value):
    """Replace a state record atomically with private file permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(text):
    """Stable identity for exact events and evidence."""
    return hashlib.sha256(text.encode()).hexdigest()


def inside(path, directory):
    """Resolve symlinks before checking a configured filesystem boundary."""
    try:
        Path(path).resolve().relative_to(Path(directory).resolve())
        return True
    except (ValueError, OSError):
        return False


OPERATIONAL_SHARED_README_NAME = "README.cross-machine.md"


def local_operational_readme():
    """Resolve the local README against the current HOME at read time."""
    return Path(os.environ.get("HOME", str(Path.home()))) / ".config/llmwiki/README.local.md"


def operational_readme_paths(config):
    """Return only the two explicitly approved operational README paths."""
    wiki_root = config.get("sharedWikiRoot", config.get("wikiRoot"))
    paths = [local_operational_readme()]
    if isinstance(wiki_root, str) and Path(wiki_root).is_absolute():
        paths.append(Path(wiki_root) / OPERATIONAL_SHARED_README_NAME)
    return paths


def read_operational_context(config, limit=12000):
    """Read optional operational notes without making them business evidence."""
    sections = []
    remaining = limit
    for path in operational_readme_paths(config):
        try:
            if remaining <= 0 or not path.is_file() or path.stat().st_size > 100000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        text = safe_text(text, remaining)
        if text:
            sections.append(f"[{path.name}]\n{text}")
            remaining -= len(text)
    return "\n\n".join(sections)


def is_excluded_artifact_path(path):
    """Reject historical, old-machine, backup, and every README artifact path."""
    parts = {part.casefold() for part in Path(path).parts}
    name = Path(path).name.casefold()
    if name.endswith(".history.md") or name.startswith("readme") and name.endswith(".md"):
        return True
    return any(part == "old-machine" or part.startswith("old-machine-")
               or part == "backup" or part.startswith("backup-")
               or part == "backups" or part == "备份" for part in parts)


def safe_text(text, limit=16000):
    """Bound evidence and redact common credential forms before model use."""
    text = str(text or "")[:limit]
    patterns = [
        r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        r"(?i)(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[=:]\s*[^\s,;]+",
        r"(?i)bearer\s+[a-z0-9._-]{16,}",
        r"https?://[^/\s:@]+:[^/\s@]+@",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text)
    return text


def page_ids(config, project_id, topic_scope=None):
    """Use the current accepted projection; stale local mappings cannot resurrect retired pages."""
    if (topic_scope or config.get("topicScope")) == "semantic":
        directory = Path(config["wikiRoot"]) / "wiki/concepts"
        return sorted("concepts/" + path.stem for path in directory.glob("*.md")
                      if inside(path, directory) and _frontmatter_header(path) is not _FRONTMATTER_INVALID)
    project = config["projects"].get(project_id, {})
    registry = load_json(Path(config["stateDir"]) / "pages.json", {})
    explicit = project.get("pages", [])
    mapped = set(explicit + registry.get(project_id, []))
    accepted = {value for value in mapped if _mapped_page_allowed(config, project_id, value, value in explicit)}
    return sorted(accepted | set(synced_pages(config, project_id)))


def _mapped_page_allowed(config, project_id, value, explicit):
    """Keep real manual pages compatible, while metadata and confinement constrain registry hints."""
    if not isinstance(value, str) or not re.fullmatch(r"concepts/[^/\\]+", value):
        return False
    if value.split("/")[1] in (".", "..") or not config.get("wikiRoot"):
        return False
    directory = Path(config["wikiRoot"]) / "wiki/concepts"
    page = Path(config["wikiRoot"]) / "wiki" / (value + ".md")
    if not inside(page, directory) or not page.is_file():
        return False
    owners = _frontmatter_projects(page)
    return project_id in owners if isinstance(owners, set) else (
        owners is _FRONTMATTER_MISSING and (explicit or page.name.startswith(_legacy_prefix(project_id))))


def _legacy_prefix(project_id):
    """Recognize the original generated page namespace without making it an ownership override."""
    readable = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9-]", "-", project_id)).strip("-")[:32] or "project"
    return readable + "-" + digest(project_id)[:8] + "-"


_FRONTMATTER_MISSING = object()
_FRONTMATTER_INVALID = object()


def _frontmatter_header(path):
    """Read only a real page's bounded YAML header for scope discovery."""
    try:
        if path.is_symlink():
            return _FRONTMATTER_INVALID
        with path.open("r", encoding="utf-8") as stream:
            text = stream.read(16000)
    except (OSError, UnicodeError):
        return _FRONTMATTER_INVALID
    if not text.startswith("---") or text[:4] not in ("---\n", "---\r"):
        return _FRONTMATTER_MISSING
    closing = re.search(r"^---[ \t]*(?:\r?\n|$)", text[4:], re.MULTILINE)
    if closing is None:
        return _FRONTMATTER_INVALID
    return text[4:4 + closing.start()]


def _header_scalar(header, key):
    """Read the compiler's simple string scalar form without a YAML dependency."""
    match = re.search(r"^" + key + r"[ \t]*:[ \t]*(.+?)[ \t]*$", header, re.MULTILINE)
    if match is None:
        return _FRONTMATTER_MISSING
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value if value else _FRONTMATTER_INVALID


def _frontmatter_projects(path):
    """Support legacy owners and the compiler's semantic source-project lists."""
    header = _frontmatter_header(path)
    if not isinstance(header, str):
        return header
    owner = _header_scalar(header, "projectId")
    if _header_scalar(header, "topicScope") != "semantic":
        return {owner} if isinstance(owner, str) else owner
    match = re.search(r"^sourceProjectIds:[ \t]*(.*)(?:\r?\n|$)", header, re.MULTILINE)
    if match is None:
        return _FRONTMATTER_INVALID
    try:
        inline = match.group(1).strip()
        values = json.loads(inline) if inline else _block_projects(header[match.end():])
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value for value in values):
            return _FRONTMATTER_INVALID
        return set(values) | ({owner} if isinstance(owner, str) else set())
    except ValueError:
        return _FRONTMATTER_INVALID


def _block_projects(text):
    """Read YAML string-list entries as emitted by the compiler's serializer."""
    values = []
    for line in text.splitlines():
        match = re.fullmatch(r"[ \t]*-[ \t]+(.+?)\s*", line)
        if match is None:
            break
        value = match.group(1)
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        values.append(value)
    return values


def synced_pages(config, project_id):
    """Recognize accepted pages by frontmatter ownership, with legacy prefix fallback."""
    if not config.get("wikiRoot"):
        return []
    prefix = _legacy_prefix(project_id)
    directory = Path(config["wikiRoot"]) / "wiki" / "concepts"
    pages = []
    for path in directory.glob("*.md"):
        if not inside(path, directory):
            continue
        owners = _frontmatter_projects(path)
        if (isinstance(owners, set) and project_id in owners
                or owners is _FRONTMATTER_MISSING and path.name.startswith(prefix)):
            pages.append("concepts/" + path.stem)
    return pages


def config_from(path):
    """Require a supported private configuration and absolute runtime paths."""
    config = load_json(path)
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Unsupported knowledge-flow configuration")
    for key in ("wikiRoot", "stateDir", "worker", "node"):
        if not Path(config[key]).is_absolute():
            raise ValueError("Runtime paths must be absolute")
    from semantic_scope import apply_scope
    return apply_scope(config)
