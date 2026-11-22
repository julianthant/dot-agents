import json
import tomllib
import unittest

import generate


def frontmatter(text: str) -> dict:
    head = text.removeprefix("---\n").partition("\n---\n")[0]
    return {
        key: json.loads(value)
        for key, value in (line.split(": ", 1) for line in head.splitlines())
    }


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.outputs = generate.outputs()
        self.roles = {
            path.stem: generate.read_role(path)
            for path in (generate.ROOT / "agents").glob("*.md")
        }

    def test_committed_outputs_are_current(self):
        for path, text in self.outputs.items():
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                text,
                f"{path}: run agents/generate.py",
            )

    def test_claude_keeps_model_effort_and_tools(self):
        blocked = "Agent, AskUserQuestion, EnterPlanMode, ExitPlanMode, Artifact, PushNotification, SendMessage, ScheduleWakeup"
        edit = {"tools": "*", "disallowedTools": blocked}
        expected = {
            "scout": {
                "model": "haiku",
                "effort": "low",
                "tools": "Bash, Read, Grep, Glob",
            },
            "mechanic": {"model": "sonnet", "effort": "low", **edit},
            "builder": {"model": "sonnet", "effort": "medium", **edit},
        }
        for name, fields in expected.items():
            text = self.outputs[generate.ROOT / f"platforms/claude/agents/{name}.md"]
            meta = frontmatter(text)
            self.assertEqual({key: meta[key] for key in fields}, fields, name)
            self.assertEqual(meta["description"], self.roles[name][0]["description"])
            self.assertTrue(text.endswith(self.roles[name][1]))

    def test_codex_toml_carries_role_and_instructions(self):
        for name, (fields, body) in self.roles.items():
            data = tomllib.loads(
                self.outputs[generate.ROOT / f"platforms/codex/agents/{name}.toml"]
            )
            self.assertEqual(data["name"], name)
            self.assertEqual(data["description"], fields["description"])
            self.assertEqual(data["developer_instructions"], body)
            self.assertIn("model", data)
            self.assertIn("model_reasoning_effort", data)

    def test_cursor_marks_read_only_roles(self):
        for name, (fields, _) in self.roles.items():
            meta = frontmatter(
                self.outputs[generate.ROOT / f"platforms/cursor/agents/{name}.md"]
            )
            self.assertEqual(
                meta.get("readonly", False), fields["access"] == "read-only", name
            )

    def test_codex_escapes_backslashes_and_triple_quotes(self):
        body = 'Match \\d+ and keep """quoted""" text.\n'
        fields = {"name": "x", "description": 'say "hi"', "access": "edit"}
        data = tomllib.loads(generate.render("toml", fields, body, {}))
        self.assertEqual(
            (data["description"], data["developer_instructions"]), ('say "hi"', body)
        )


if __name__ == "__main__":
    unittest.main()
