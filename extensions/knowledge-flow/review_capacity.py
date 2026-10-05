"""Read project review capacity and recognize legacy capacity refusals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from current_reviews import current_reviews

# Error text ``pipeline.ts`` records for a batch refused by a full review queue.
QUEUE_FULL_ERROR = "review queue is full"


def pending_reviews(state: Path, project_id: str, replaced_id: str | None = None) -> int:
    """Count active holds, excluding the valid lineage a retry will replace."""
    return len(current_reviews(state, project_id, replaced_id))


def _review_limit(config: dict[str, Any]) -> int | None:
    """Return a valid configured limit; invalid values stay the worker's to reject."""
    limit = config.get("maxPendingPerProject")
    return limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else None


def review_queue_full(config: dict[str, Any], job: dict[str, Any]) -> bool:
    """True when ``pipeline.ts`` would refuse this job with ``QUEUE_FULL_ERROR``."""
    limit = _review_limit(config)
    project = job.get("projectId")
    if limit is None or not project:
        return False
    return pending_reviews(Path(config["stateDir"]), str(project), job.get("reviewRetryOf")) >= limit


def is_capacity_refusal(result: Any) -> bool:
    """Recognize deferred capacity and the older queue-full hold at read boundaries."""
    if not isinstance(result, dict):
        return False
    return (result.get("status") == "deferred" and result.get("error") == QUEUE_FULL_ERROR
            or result.get("status") == "needs_review" and result.get("error") == QUEUE_FULL_ERROR)
