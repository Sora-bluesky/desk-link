import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watch


class EventContractTests(unittest.TestCase):
    def test_seat_event_keeps_required_keys_and_omits_unused_routing(self):
        event = watch.make_event(
            "cursor",
            "%USERPROFILE%/.cursor/session.jsonl",
            {"type": "assistant", "text": "hello"},
        )

        self.assertEqual(
            set(event),
            {"id", "ts", "seat", "dir", "kind", "text", "src"},
        )
        self.assertEqual(event["seat"], "cursor")
        self.assertEqual(event["dir"], "in")
        self.assertEqual(event["kind"], "utterance")
        self.assertLessEqual(len(event["text"]), watch.TEXT_MAX)

    def test_optional_routing_uses_only_declared_values(self):
        event = watch.make_event(
            "claude",
            "%USERPROFILE%/.claude/session.jsonl",
            {"type": "assistant", "text": "routed"},
            to="grok-build",
            model="grok-4.6",
            effort="xhigh",
        )

        self.assertEqual(event["to"], "grok-build")
        self.assertEqual(event["model"], "grok-4.6")
        self.assertEqual(event["effort"], "xhigh")
        self.assertEqual(
            watch.ROUTING_VALUES["to"],
            ("cursor", "claude", "codex", "grok-build"),
        )

        for field, value in (
            ("to", "unknown"),
            ("model", "unknown"),
            ("effort", "unknown"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, f"invalid {field}"):
                    watch.routing_fields(**{field: value})

    def test_seat_kind_remains_utterance_or_meta(self):
        utterance = watch.make_event(
            "codex",
            "%USERPROFILE%/.codex/utterance.jsonl",
            {"type": "user", "text": "question"},
        )
        meta = watch.make_event(
            "grok_build",
            "%USERPROFILE%/.grok/meta.jsonl",
            {"type": "tool_use", "name": "Read"},
        )

        self.assertEqual(utterance["kind"], "utterance")
        self.assertEqual(meta["kind"], "meta")

    def test_inbox_append_writes_complete_jsonl_records(self):
        events = [
            {"id": "one", "text": "first"},
            {"id": "two", "text": "second"},
        ]
        with tempfile.TemporaryDirectory() as root:
            inbox = os.path.join(root, "inbox.jsonl")
            watch.append_inbox(inbox, events)
            with open(inbox, "r", encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]

        self.assertEqual(records, events)

    def test_main_catches_storage_error_without_traceback_or_private_path(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            private_path = os.path.join(root, "bus")
            Path(private_path).write_text("not a directory", encoding="utf-8")
            with mock.patch.object(watch, "SCRIPT_DIR", root), mock.patch("sys.stderr", stderr):
                code = watch.main(["--once"])

        output = stderr.getvalue()
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(output),
            {"status": "error", "error": "watch storage operation failed"},
        )
        self.assertNotIn(private_path, output)
        self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()
