"""Track explicitly reviewed baseline-page retirement separately from content ownership.

An absent generated page ordinarily restores its baseline. A reviewed merge (the
legacy migration or a topic merge) is different: its tombstone survives retries,
and removing it restores the original page while still protecting any
human-created replacement.
"""
from typing import Any


def retired_baseline(value: Any, baseline: dict[str, bytes]) -> set[str]:
    """Validate a persisted tombstone inventory without accepting arbitrary paths."""
    if value is None:
        return set()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("invalid retired baseline inventory")
    if len(set(value)) != len(value):
        raise ValueError("duplicate retired baseline page")
    for item in value:
        if item not in baseline or not item.startswith("wiki/concepts/") or not item.endswith(".md"):
            raise ValueError("retired baseline path is outside reviewed concepts")
    return set(value)


def desired_retirement(config: dict[str, Any], baseline: dict[str, bytes], projection: dict[str, bytes]) -> set[str]:
    """Only baseline pages removed by the validated migration or a reviewed merge receive tombstones."""
    migration = config.get("topicMigration") or {}
    pages = [*migration.get("pages", []), *(config.get("topicMerges") or [])]
    if not pages and not migration.get("retiredPages"):
        return set()
    destinations = {"wiki/" + page["pageId"] + ".md" for page in pages}
    old = {"wiki/" + prior["pageId"] + ".md" for page in pages for prior in page["previousPages"]}
    old.update("wiki/" + page["pageId"] + ".md" for page in migration.get("retiredPages", []))
    retired = retired_baseline(sorted((old - destinations) & set(baseline)), baseline)
    if any(relative in projection for relative in retired):
        raise ValueError("retired baseline page is still present in the verified generation")
    return retired
