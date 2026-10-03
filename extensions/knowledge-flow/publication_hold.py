"""Turn a durable result that can never be published into an ordinary review hold.

A model result is saved before publication and reused verbatim on every later
finalization attempt.  When publication rejects the result's own content (the
contract, not the exchange or the network), every retry fails the same way, so
backing off would only hide the batch forever.  Instead the batch completes as a
session hold: its review file states the violation, the batch audit keeps the
rejected result, and the existing ``--retry-review`` drafts it again under the
current contract with the original evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common import digest, save_json

CONTRACT_HOLD_ERROR = "durable result violates the publication contract"


def hold_unpublishable(config: dict[str, Any], job: dict[str, Any], error: Exception) -> dict[str, Any]:
    """Write the review hold for ``job`` and return the result that replaces the rejected one.

    The review mirrors what ``pipeline.ts`` writes for a held session, so review
    capacity, ``--resolve`` and ``--retry-review`` treat it like any other hold.
    """
    state = Path(config["stateDir"])
    reason = f"{CONTRACT_HOLD_ERROR}: {error}; reprocess with --retry-review"
    review_path = state / "review" / (job["id"] + ".json")
    review = {"jobId": job["id"], "projectId": job["projectId"], "createdAt": job.get("createdAt"),
              "claims": [], "evidence": [], "decisions": [{"decision": "needs_review", "reason": reason}]}
    if job.get("sessionContext"):
        review["sessionReview"] = {"sessionContext": job["sessionContext"], "inputEvidence": job.get("evidence", []),
                                   "modelStages": str(state / "consolidation" / digest(job["id"]))}
    save_json(review_path, review)
    return {"status": "needs_review", "publishedPageIds": [], "reviewCount": 1,
            "error": reason, "reviewFile": str(review_path)}
