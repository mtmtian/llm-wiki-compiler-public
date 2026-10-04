"""Read complete visible text from one native Claude Code prompt branch.

The host prompt_id must match a transcript promptId. Parent links, session,
workspace and final assistant text are validated before supplying evidence to
the shared queue. Tool results, thinking, sidechains and previous turns never
become knowledge. Bounded tail reads fail closed if a full turn is unavailable.
"""

import json
import re
from pathlib import Path

MAX_BYTES = 8_000_000
MAX_CHARS = 120_000
NUMBER_PREFIX = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
NUMBER_SUFFIXES = {".", "e", "E", "e+", "e-", "E+", "E-"}


class IncompleteTranscript(ValueError):
    """The host may still be flushing this turn; retry briefly without fallback."""


def visible_text(row):
    """Return text blocks only, excluding meta prompts and tool observations."""
    if row.get("isMeta") or row.get("type") not in ("user", "assistant"):
        return ""
    message = row.get("message", {})
    if not isinstance(message, dict) or message.get("role") != row["type"]:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    if row["type"] == "user" and any(isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
        return ""
    return "\n".join(part["text"] for part in content if isinstance(part, dict)
                     and part.get("type") == "text" and isinstance(part.get("text"), str)).strip()


def read_rows(event, claude_dir):
    """Confine the transcript to the selected Claude profile before opening it."""
    path = Path(event["transcript_path"])
    if not path.is_absolute():
        raise ValueError("transcript-not-absolute")
    path = path.resolve(strict=True)
    root = (claude_dir / "projects").resolve()
    if not path.is_relative_to(root) or path.suffix != ".jsonl":
        raise ValueError("transcript-outside-profile")
    with path.open("rb") as stream:
        size = path.stat().st_size
        stream.seek(max(0, size - MAX_BYTES))
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise IncompleteTranscript("transcript-changed-during-read")
    if size > MAX_BYTES:
        raw = raw.partition(b"\n")[2]
    lines = raw.split(b"\n")
    terminated = raw.endswith(b"\n")
    if terminated:
        lines.pop()
    rows = [decode_row(line, index == len(lines) - 1 and not terminated)
            for index, line in enumerate(lines) if line.strip()]
    if not rows:
        raise IncompleteTranscript("transcript-empty")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("transcript-invalid")
    return rows


def decode_row(line, incomplete_tail):
    """Decode a JSONL row, retrying only an unfinished unterminated tail."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as error:
        unfinished_utf8 = (incomplete_tail and error.reason == "unexpected end of data"
                            and error.end == len(line))
        if unfinished_utf8:
            raise IncompleteTranscript("transcript-partial-write") from error
        raise ValueError("transcript-invalid-utf8") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        if incomplete_tail and incomplete_json_prefix(text, error):
            raise IncompleteTranscript("transcript-partial-write") from error
        raise ValueError("transcript-invalid-json") from error


def incomplete_json_prefix(text, error):
    """Recognize JSON parser failures that could finish with more tail bytes."""
    if error.msg == "Unterminated string starting at":
        return True
    if incomplete_unicode_escape(text, error) or incomplete_number_prefix(text, error):
        return True
    suffix = text[error.pos:]
    partial_literals = {"t", "tr", "tru", "f", "fa", "fal", "fals", "n", "nu", "nul", "-"}
    return not suffix.strip() or (error.msg == "Expecting value" and suffix in partial_literals)


def incomplete_unicode_escape(text, error):
    """Accept only a trailing prefix of four hexadecimal digits after ``\\u``."""
    if error.msg != "Invalid \\uXXXX escape" or error.pos == 0 or error.pos >= len(text):
        return False
    if text[error.pos - 1:error.pos + 1] != "\\u":
        return False
    digits = text[error.pos + 1:]
    return len(digits) < 4 and all(char in "0123456789abcdefABCDEF" for char in digits)


def incomplete_number_prefix(text, error):
    """Recognize an otherwise valid number ending at its fraction or exponent marker."""
    if error.msg != "Expecting ',' delimiter":
        return False
    suffix = text[error.pos:]
    if suffix not in NUMBER_SUFFIXES:
        return False
    prefix = text[:error.pos]
    match = NUMBER_PREFIX.search(prefix)
    if not match or (match.start() and prefix[match.start() - 1] not in " \t\r\n:[,"):
        return False
    return suffix != "." or "." not in match.group()


def current_branch(rows, event, project_roots):
    """Walk the final assistant's ancestry back to this exact native prompt."""
    expected = event.get("last_assistant_message", "").strip()
    endings = [r for r in rows if r.get("type") == "assistant" and visible_text(r) == expected]
    if not expected or not endings:
        raise IncompleteTranscript("completion-not-found")
    ending = endings[-1]
    message = ending.get("message", {})
    if not isinstance(message, dict) or message.get("stop_reason") != "end_turn":
        raise IncompleteTranscript("completion-not-final")
    indexed = {r["uuid"]: r for r in rows if isinstance(r.get("uuid"), str)}
    current, seen, branch = endings[-1], set(), []
    while True:
        validate_row(current, event, project_roots)
        identifier = current.get("uuid")
        if not identifier or identifier in seen:
            raise ValueError("invalid-transcript-ancestry")
        seen.add(identifier)
        branch.append(current)
        if current.get("type") == "user" and current.get("promptId") and visible_text(current):
            content = current.get("message", {}).get("content", [])
            if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "image"
                                                  for part in content):
                raise ValueError("image-evidence-incomplete")
            if current["promptId"] != event["prompt_id"]:
                raise ValueError("different-prompt")
            return list(reversed(branch))
        current = indexed.get(current.get("parentUuid"))
        if current is None:
            raise IncompleteTranscript("incomplete-transcript-ancestry")


def validate_row(row, event, project_roots):
    """Accept directory changes only within one Git worktree or configured project directory."""
    if row.get("sessionId") != event["session_id"] or row.get("isSidechain"):
        raise ValueError("foreign-session")
    if workspace_root(row.get("cwd"), project_roots) != workspace_root(event["cwd"], project_roots):
        raise ValueError("foreign-workspace")


def workspace_root(value, project_roots):
    """Return the directory that bounds one workspace; other folders stay exact.

    A Git worktree keeps its existing boundary. Outside Git, the nearest
    resolved configured project directory in ``project_roots`` is the boundary,
    so nested repositories, other projects and symlink escapes stay separate.
    """
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("foreign-workspace")
    directory = Path(value).resolve()
    chain = (directory, *directory.parents)
    repository = next((parent for parent in chain if (parent / ".git").exists()), None)
    return repository or next((parent for parent in chain if parent in project_roots), directory)


def evidence(event, claude_dir, project_roots):
    """Supply hashed visible messages only after complete branch validation."""
    from common import digest, safe_text

    branch = current_branch(read_rows(event, claude_dir), event, project_roots)
    visible = [(row, visible_text(row)) for row in branch if visible_text(row)]
    if not visible or visible[0][0].get("type") != "user":
        raise ValueError("missing-visible-prompt")
    if sum(len(text) for _, text in visible) > MAX_CHARS:
        raise ValueError("turn-over-evidence-budget")
    return [{"id": "claude-" + digest(row["uuid"])[:24], "kind": row["type"],
             "text": safe_text(text, MAX_CHARS), "complete": True,
             "locator": "claude://" + event["session_id"] + "/message/" + row["uuid"],
             "observedAt": row.get("timestamp", "")} for row, text in visible]
