"""Let new work wait while a project's review queue is full.

``pipeline.ts`` refuses a batch once a project holds ``maxPendingPerProject``
reviews and records it as a terminal hold.  Doing that after the worker has
spent budget and frozen the batch turns a capacity limit into silent loss, so
the worker asks first and keeps the turn queued instead.

Waiting is bounded: once half of the shared intake queue is in use, the worker
stops waiting and lets the existing guard record the hold (which an operator can
reprocess with ``--retry-review``), so one project's backlog never fills the
queue that every project's Stop hook depends on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common import load_json

# Error text ``pipeline.ts`` records for a batch refused by a full review queue.
QUEUE_FULL_ERROR = "review queue is full"
DEFAULT_MAX_QUEUED_JOBS = 30
# Share of the shared intake queue that waiting work may occupy.
WAITING_QUEUE_SHARE = 0.5


def pending_reviews(state: Path, project_id: str, replaced_id: str | None = None) -> int:
    """Count holds exactly like ``pipeline.ts`` ``pendingCount``.

    A retry does not count the review it would replace.
    """
    count = 0
    for path in (state / "review").glob("*.json"):
        if replaced_id and path.name == replaced_id + ".json":
            continue
        try:
            record = load_json(path, {})
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("projectId") == project_id:
            count += 1
    return count


def _review_limit(config: dict[str, Any]) -> int | None:
    """Return a valid configured limit; invalid values stay the worker's to reject."""
    limit = config.get("maxPendingPerProject")
    return limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else None


def _queue_has_room(state: Path, config: dict[str, Any]) -> bool:
    """Waiting work may use at most a fixed share of the shared intake queue."""
    capacity = int(config.get("maxQueuedJobs", DEFAULT_MAX_QUEUED_JOBS))
    return len(list((state / "queue").glob("*.json"))) < capacity * WAITING_QUEUE_SHARE


def review_queue_full(config: dict[str, Any], job: dict[str, Any]) -> bool:
    """True when ``pipeline.ts`` would refuse this job with ``QUEUE_FULL_ERROR``."""
    limit = _review_limit(config)
    project = job.get("projectId")
    if limit is None or not project:
        return False
    return pending_reviews(Path(config["stateDir"]), str(project), job.get("reviewRetryOf")) >= limit


def should_wait_for_review(config: dict[str, Any], job: dict[str, Any]) -> bool:
    """True when this job's project review queue is full and the intake queue has room.

    Example: with ``maxPendingPerProject`` 10, ten ``review/*.json`` records for
    project ``p`` and three queued files, a job for ``p`` waits; with fifteen
    queued files out of thirty, it does not.
    """
    return review_queue_full(config, job) and _queue_has_room(Path(config["stateDir"]), config)
