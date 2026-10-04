"""Verify that an append racing a bounded transcript read can recover safely."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.test_agent_plugins_claude import FakeClock, capture_after_flush_test, fixture
from claude_capture import MAX_BYTES


class AppendBeforeRead:
    """Append bytes after the reader seeks but before it fetches transcript data."""

    def __init__(self, stream, append) -> None:
        self.stream = stream
        self.append = append
        self.did_append = False

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def seek(self, *args):
        return self.stream.seek(*args)

    def read(self, size=-1):
        if not self.did_append:
            self.append()
            self.did_append = True
        return self.stream.read(size)


class ClaudeTranscriptReadRaceTests(unittest.TestCase):
    """Ensure a genuine append race retries without weakening transcript checks."""

    def test_growth_between_stat_and_read_retries_from_the_bounded_tail(self) -> None:
        """Given a full-size transcript, when one byte races the read, capture retries."""
        with tempfile.TemporaryDirectory() as temp:
            event, profile = fixture(Path(temp))
            transcript = Path(event["transcript_path"])
            body = transcript.read_bytes()
            marker = b'{"type":"file-history-snapshot","padding":"'
            suffix = b'"}\n'
            fill = MAX_BYTES - len(marker) - len(suffix) - len(body)
            transcript.write_bytes(marker + b"x" * fill + suffix + body)
            self.assertEqual(MAX_BYTES, transcript.stat().st_size)

            original_open = Path.open
            resolved_transcript = transcript.resolve()
            race = {"triggered": False}

            def append_once() -> None:
                with original_open(transcript, "ab") as stream:
                    stream.write(b" ")

            def controlled_open(path, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                stream = original_open(path, *args, **kwargs)
                if path.resolve() == resolved_transcript and mode == "rb" and not race["triggered"]:
                    race["triggered"] = True
                    return AppendBeforeRead(stream, append_once)
                return stream

            clock = FakeClock()
            with patch.object(Path, "open", controlled_open):
                result = capture_after_flush_test(event, profile, clock)

            self.assertTrue(race["triggered"])
            self.assertEqual(["ask", "answer"], [item["text"] for item in result])


if __name__ == "__main__":
    unittest.main()
