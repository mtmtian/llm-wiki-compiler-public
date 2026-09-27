"""Extract explicit repository and filesystem references from a user prompt.

This module deliberately only parses text.  It does not execute commands,
expand ``~``/``HOME``, inspect repositories, or read files.  ``routing`` is
responsible for checking that extracted paths exist and are within a
configured project boundary before using them.
"""

from dataclasses import dataclass
import re
from typing import Tuple


@dataclass(frozen=True)
class RouteMentions:
    """Explicit references found in one prompt.

    ``paths`` contains only absolute-path-looking text.  They are untrusted
    strings until ``routing`` resolves them against the local filesystem.
    ``github_urls`` contains complete URL/SSH tokens so the caller can apply
    its strict GitHub identity parser.
    """

    paths: Tuple[str, ...] = ()
    github_urls: Tuple[str, ...] = ()


# Markdown destinations permit spaces when wrapped in ``<...>``.  The plain
# destination form also handles the common ``[report](/tmp/report.md)`` case.
_MARKDOWN_PATH = re.compile(
    r"\]\(\s*(?:<(?P<angle>/[^>\n]+)>|(?P<plain>/[^)\n]+))\s*\)"
)
_BACKTICK_PATH = re.compile(r"`(?P<value>/[^`\n]+)`")
_ANGLE_PATH = re.compile(r"<(?P<value>/[^>\n]+)>")
# Raw paths without spaces are intentionally conservative.  Paths containing
# spaces should use markdown angle brackets or backticks so their boundary is
# explicit and shell snippets cannot be interpreted as a path.
_RAW_PATH = re.compile(r"(?<![:\w/])(?P<value>/[^\s<>'\"`\]\[),;!?]+)")

# Keep the complete token. ``remote_identity`` validates repository-page suffixes
# and rejects look-alike hosts; this parser must not make that decision.
_GITHUB_TOKEN = re.compile(
    r"(?<![\w.-])(?:https?://github\.com/[^\s<>'\"`)]+|ssh://(?:git@)?github\.com/[^\s<>'\"`)]+|git@github\.com:[^\s<>'\"`)]+)",
    re.IGNORECASE,
)


def _clean_path(value: str) -> str:
    """Remove markdown punctuation while preserving spaces in the path."""
    value = value.strip().rstrip(".,;!?。；，！)")
    # A markdown link can include a line suffix used by local viewers.  The
    # suffix is metadata, not part of the filesystem path.
    value = re.sub(r":\d+(?::\d+)?$", "", value)
    return value.rstrip(".,;!?。；，！)")


def parse_route_mentions(prompt: str) -> RouteMentions:
    """Return explicit absolute paths and GitHub URL tokens from ``prompt``.

    Parsing is side-effect free and intentionally does not infer a project
    from a bare repository name or from arbitrary relative paths.
    """
    text = str(prompt or "")
    paths = []
    for match in _MARKDOWN_PATH.finditer(text):
        paths.append(_clean_path(match.group("angle") or match.group("plain")))
    for pattern in (_BACKTICK_PATH, _ANGLE_PATH):
        for match in pattern.finditer(text):
            paths.append(_clean_path(match.group("value")))
    # Do not discover a path embedded in a command-like backtick expression;
    # only a backtick whose complete content starts with ``/`` is explicit.
    raw_text = re.sub(r"`[^`\n]*`", " ", _MARKDOWN_PATH.sub(" ", text))
    for match in _RAW_PATH.finditer(raw_text):
        paths.append(_clean_path(match.group("value")))
    urls = [match.group(0).rstrip(".,;!?。；，！") for match in _GITHUB_TOKEN.finditer(text)]

    # A GitHub URL is not a local path.  The raw-path regex cannot normally
    # match it because ``/`` is preceded by ``:``; this guard also protects
    # against future regex changes.
    paths = [path for path in paths if not re.match(r"^(?:https?|ssh):", path, re.I)]
    # A wrapped path containing spaces also produces a conservative raw-path
    # prefix (for example ``/Users/me/My``).  Keep the explicitly delimited
    # longer value and discard such prefixes.
    paths = [path for path in paths if not any(
        other != path and other.startswith(path + " ") for other in paths
    )]
    return RouteMentions(
        paths=tuple(dict.fromkeys(path for path in paths if path.startswith("/"))),
        github_urls=tuple(dict.fromkeys(urls)),
    )


__all__ = ["RouteMentions", "parse_route_mentions"]
