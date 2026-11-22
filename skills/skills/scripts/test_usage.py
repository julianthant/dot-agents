import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import usage


def entry(kind, content, age_days):
    ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()  # noqa: UP017
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": content}})


class UsageTest(unittest.TestCase):
    def test_count(self):
        skill_call = [{"type": "tool_use", "name": "Skill", "input": {"skill": "lane"}}]
        lines = [
            entry("assistant", skill_call, 1),
            entry("assistant", skill_call, 90),
            entry("user", "<command-name>/lane</command-name>\n<command-args>x", 1),
            entry(
                "user",
                [{"type": "text", "text": "<command-name>/plan</command-name>"}],
                2,
            ),
            entry("assistant", "<command-name>/nope</command-name>", 1),
            entry("user", "see <command-name>name</command-name> syntax", 1),
            entry(
                "user",
                [
                    {
                        "type": "tool_result",
                        "content": "<command-name>/res</command-name>",
                    }
                ],
                1,
            ),
            "not json",
            json.dumps({"type": "user", "message": {"content": "x"}}),
        ]
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "a.jsonl"
            f.write_text("\n".join(lines))
            since = datetime.now(timezone.utc) - timedelta(days=30)  # noqa: UP017
            tool, typed = usage.count([f], since)
        self.assertEqual(dict(tool), {"lane": 1})
        self.assertEqual(dict(typed), {"lane": 1, "plan": 1})


if __name__ == "__main__":
    unittest.main()
