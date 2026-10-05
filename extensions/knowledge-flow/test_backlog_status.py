"""Backlog visibility: per-project reviews, queue-full holds and capacity waits."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from backlog_status import REVIEW_BACKLOG_THRESHOLD, backlog_summary
from common import save_json
from maintenance import report
from wake import _reasons

PROJECT = "sample-project"
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)


class BacklogStatusTests(unittest.TestCase):
    """The summary must expose backlog size and age without trusting every file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"

    def add_review(self, name, created, project=PROJECT, **extra):
        save_json(self.state / "review" / f"{name}.json", {
            "jobId": name, "projectId": project, "createdAt": created, **extra})

    def add_wait(self, name, created, project=PROJECT, reason="review-queue-full"):
        save_json(self.state / "capture-pending" / f"{name}.json", {
            "kind": "capacity", "id": name, "createdAt": created, "reason": reason,
            "job": {"id": name, "projectId": project, "createdAt": created}})

    def test_empty_state_reports_empty_groups(self):
        """Given no state directories, When summarized, Then every group is empty."""
        self.assertEqual(backlog_summary(self.state, at=NOW), {
            "reviewsByProject": {}, "unresolvedQueueFullByProject": {},
            "captureWaitsByProject": {}})

    def test_reviews_count_current_heads_with_oldest_age(self):
        """Given a retry chain and a fresh review, Then only heads count and age is hours."""
        self.add_review("old", "2026-10-03T12:00:00+00:00")
        self.add_review("retry", "2026-10-04T12:00:00Z", reviewRetryOf="old")
        self.add_review("fresh", "2026-10-05T10:00:00Z")
        self.add_review("elsewhere", "2026-10-05T11:00:00Z", project="other-project")
        summary = backlog_summary(self.state, at=NOW)["reviewsByProject"]
        self.assertEqual(summary[PROJECT], {"current": 2, "oldestAgeHours": 24.0})
        self.assertEqual(summary["other-project"], {"current": 1, "oldestAgeHours": 1.0})

    def test_unresolved_queue_full_is_grouped_by_project(self):
        """Given audit holds, Then they are counted per project and unnamed ones are ignored."""
        holds = [{"projectId": PROJECT}, {"projectId": PROJECT}, {"projectId": "other-project"},
                 {"projectId": None}]
        summary = backlog_summary(self.state, holds, at=NOW)["unresolvedQueueFullByProject"]
        self.assertEqual(summary, {"other-project": 1, PROJECT: 2})

    def test_capture_waits_report_count_reason_and_oldest_age(self):
        """Given capacity waits, Then count, reason and the oldest wait age are reported."""
        self.add_wait("wait-a", "2026-10-05T06:00:00Z")
        self.add_wait("wait-b", "2026-10-05T11:00:00Z")
        save_json(self.state / "capture-pending/plain-retry.json", {
            "id": "plain-retry", "event": {"a": 1}, "record": {"projectId": PROJECT}})
        summary = backlog_summary(self.state, at=NOW)["captureWaitsByProject"]
        self.assertEqual(summary, {PROJECT: {"count": 2, "reason": "review-queue-full",
                                             "oldestAgeHours": 6.0}})

    def test_ordinary_retry_counts_as_a_wait_only_when_the_queue_is_full(self):
        """Given ordinary retries, Then only the one rescheduled for a full queue is a capacity wait."""
        for name, reason in (("transcript", "transcript-partial-write"), ("full", "queue-full")):
            save_json(self.state / "capture-pending" / f"{name}.json", {
                "id": name, "createdAt": "2026-10-05T09:00:00Z", "reason": reason,
                "event": {}, "record": {"projectId": PROJECT}})
        summary = backlog_summary(self.state, at=NOW)["captureWaitsByProject"]
        self.assertEqual(summary, {PROJECT: {"count": 1, "reason": "queue-full", "oldestAgeHours": 3.0}})

    def test_timestamps_that_overflow_utc_conversion_count_as_unknown_age(self):
        """Given a review dated at the datetime boundary, Then the summary still reports it."""
        self.add_review("edge", "0001-01-01T00:00:00+01:00")
        summary = backlog_summary(self.state, at=NOW)["reviewsByProject"]
        self.assertEqual(summary[PROJECT], {"current": 1, "oldestAgeHours": None})

    def test_corrupt_and_malformed_files_do_not_crash_the_summary(self):
        """Given unreadable records, Then valid ones are still counted and nothing raises."""
        self.add_review("good", "2026-10-05T11:00:00Z")
        (self.state / "review/broken.json").write_text("{not json")
        (self.state / "capture-pending").mkdir(parents=True)
        (self.state / "capture-pending/broken.json").write_text("[1, 2")
        save_json(self.state / "capture-pending/list.json", ["unexpected"])
        self.add_wait("undated", "not-a-date")
        summary = backlog_summary(self.state, at=NOW)
        self.assertEqual(summary["reviewsByProject"][PROJECT]["current"], 1)
        self.assertEqual(summary["captureWaitsByProject"][PROJECT],
                         {"count": 1, "reason": "review-queue-full", "oldestAgeHours": None})

    def test_check_report_persists_backlog_block(self):
        """Given a review and a wait, When --check's report runs, Then maintenance.json has it."""
        self.add_review("one", "2026-10-05T11:00:00Z")
        self.add_wait("wait-a", "2026-10-05T11:00:00Z")
        config = {"version": 1, "stateDir": str(self.state), "projects": {}, "enabled": False}
        result = report(config)
        persisted = json.loads((self.state / "maintenance.json").read_text())
        for value in (result, persisted):
            self.assertEqual(value["backlog"]["reviewsByProject"][PROJECT]["current"], 1)
            self.assertEqual(value["backlog"]["captureWaitsByProject"][PROJECT]["count"], 1)

    def test_wake_reports_backlog_at_threshold_or_with_any_capacity_wait(self):
        """Given a large review count or any capacity wait, Then wake adds review-backlog once."""
        for index in range(REVIEW_BACKLOG_THRESHOLD - 1):
            self.add_review(f"r{index}", "2026-10-05T11:00:00Z")
        below = backlog_summary(self.state, at=NOW)
        self.assertNotIn("review-backlog", _reasons({}, {}, {}, below))
        self.add_review("last", "2026-10-05T11:00:00Z")
        at_threshold = backlog_summary(self.state, at=NOW)
        self.assertEqual(_reasons({}, {}, {}, at_threshold).count("review-backlog"), 1)
        waits_only = {"reviewsByProject": {}, "captureWaitsByProject": {PROJECT: {"count": 1}}}
        self.assertIn("review-backlog", _reasons({}, {}, {}, waits_only))


if __name__ == "__main__":
    unittest.main()
