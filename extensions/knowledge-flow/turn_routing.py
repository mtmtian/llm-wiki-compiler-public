"""Recover a project discovered during one completed conversation turn.

Prompt routing is the early retrieval gate. This second, intake-only pass uses
the same ownership and ambiguity rules on already validated visible messages.
It neither scans older tasks nor asks a model to classify unrelated chats, and
an assistant's routing hint never becomes proof that its claimed work happened.
"""

from capture import capture_evidence_result
from common import safe_text
from route_mentions import parse_route_mentions
from routing import resolve, resolve_repo_identity

MAX_ROUTE_CHARS = 120_000
MAX_ROUTE_MENTIONS = 24
RECOVERABLE_REASONS = {None, "unbound-workspace", "explicit-path-unresolved"}


def recover_turn_route(event, record, config):
    """Return a late route and its capture, preserving every explicit denial."""
    if record.get("projectId") or record.get("reason") not in RECOVERABLE_REASONS:
        return record, None
    source = {**event}
    if not source.get("transcript_path") and record.get("transcriptPath"):
        source["transcript_path"] = record["transcriptPath"]
    captured = capture_evidence_result(source, record, config)
    current = [item for item in captured["evidence"]
               if item.get("current") is not False and not item.get("historical")]
    users = [item["text"] for item in current if item["kind"] == "user"]
    text = "\n".join(item["text"] for item in current)
    if not users or not text or len(text) > MAX_ROUTE_CHARS:
        return record, captured
    mentions = parse_route_mentions(text)
    if len(mentions.paths) + len(mentions.github_urls) > MAX_ROUTE_MENTIONS:
        return record, captured
    project, reason = resolve(event["cwd"], text, None, config)
    if not project:
        return record, captured
    return {**record, "projectId": project, "reason": "turn-evidence",
            "routeReason": reason, "prompt": safe_text("\n".join(users), 12000),
            "repoIdentity": resolve_repo_identity(event["cwd"], text, project, config)}, captured
