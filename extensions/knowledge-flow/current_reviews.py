"""Read active review records while conservatively folding valid retry chains."""

from collections import defaultdict, deque
from pathlib import Path

from common import load_json


def _project_records(state: Path, project_id: str | None) -> dict[str, dict]:
    """Read identifiable review objects; malformed files remain outside project counts."""
    records = {}
    for path in sorted((state / "review").glob("*.json")):
        try:
            value = load_json(path)
        except (OSError, ValueError, TypeError):
            continue
        if (isinstance(value, dict) and isinstance(value.get("projectId"), str)
                and (project_id is None or value["projectId"] == project_id)):
            records[path.stem] = value
    return records


def _valid_links(records: dict[str, dict]) -> dict[str, str]:
    """Accept only explicit, same-project filename links with matching child identity."""
    links = {}
    for identifier, child in records.items():
        parent_id = child.get("reviewRetryOf")
        if (not isinstance(parent_id, str) or not parent_id
                or child.get("jobId") != identifier or parent_id not in records):
            continue
        parent = records[parent_id]
        if "jobId" in parent and parent.get("jobId") != parent_id:
            continue
        if parent.get("projectId") == child.get("projectId"):
            links[identifier] = parent_id
    return links


def _components(records: dict[str, dict], links: dict[str, str]) -> list[set[str]]:
    """Return connected lineage components, including unlinked standalone records."""
    neighbors = {identifier: set() for identifier in records}
    for child, parent in links.items():
        neighbors[child].add(parent)
        neighbors[parent].add(child)
    components, seen = [], set()
    for start in records:
        if start in seen:
            continue
        component, pending = set(), deque([start])
        while pending:
            current = pending.popleft()
            if current in seen:
                continue
            seen.add(current)
            component.add(current)
            pending.extend(neighbors[current] - seen)
        components.append(component)
    return components


def _active_members(component: set[str], links: dict[str, str]) -> set[str]:
    """Fold only a simple chain; cycles and retry forks remain separately active."""
    edges = {child: parent for child, parent in links.items() if child in component}
    children = defaultdict(int)
    for parent in edges.values():
        children[parent] += 1
    if len(component) > 1 and len(edges) == len(component) - 1 and all(
            children[identifier] <= 1 for identifier in component):
        leaves = [identifier for identifier in component if children[identifier] == 0]
        if len(leaves) == 1:
            return {leaves[0]}
    return component


def current_reviews(state: Path, project_id: str | None = None,
                    replaced_id: str | None = None) -> list[tuple[str, dict]]:
    """Return active same-project reviews, optionally excluding one replaced lineage."""
    records = _project_records(Path(state), project_id)
    links = _valid_links(records)
    active = []
    for component in _components(records, links):
        members = _active_members(component, links)
        if replaced_id in component:
            if len(component) == 1 or len(members) == 1:
                continue
            members.discard(replaced_id)
        active.extend((identifier, records[identifier]) for identifier in members)
    return sorted(active, key=lambda item: item[0])
