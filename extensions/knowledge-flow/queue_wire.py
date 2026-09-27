"""Canonical JSON wire boundary for ordinary knowledge-flow subprocess calls."""

from __future__ import annotations

import json
from typing import Any


HARD_PROCESS_EVENT_BYTES = 600_000


class EventTooLarge(ValueError):
    """Raised before a request can reach the bounded Node process."""

    def __init__(self, operation: str, actual: int, maximum: int):
        super().__init__(f"{operation} event is {actual} bytes; maximum is {maximum}")
        self.operation = operation
        self.actual = actual
        self.maximum = maximum


def request_bytes(config: dict[str, Any], value: dict[str, Any]) -> bytes:
    """Serialize exactly the object passed to ``subprocess.run(input=...)``."""
    return json.dumps({**value, "config": config}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def process_limit(config: dict[str, Any]) -> int:
    """Clamp local configuration so it cannot exceed the Node hard cap."""
    configured = int(config.get("maxProcessEventBytes", HARD_PROCESS_EVENT_BYTES))
    if configured <= 0:
        raise ValueError("maxProcessEventBytes must be positive")
    return min(HARD_PROCESS_EVENT_BYTES, configured)


def checked_request(config: dict[str, Any], operation: str, value: dict[str, Any]) -> bytes:
    """Return wire bytes or reject a request before spawning the subprocess."""
    encoded = request_bytes(config, value)
    maximum = process_limit(config)
    if len(encoded) > maximum:
        raise EventTooLarge(operation, len(encoded), maximum)
    return encoded


def job_bytes(job: dict[str, Any]) -> int:
    """Measure one queued job with the same escaping policy as the wire."""
    return len(json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
