#!/usr/bin/env python3
"""Write each tool's subagent files from the role sources.

Inputs: agents/<role>.md (name, description, access, instructions) and
platforms/<tool>/subagents.toml (that tool's [role.<name>] and [access.<level>] fields).
Outputs: platforms/<tool>/agents/<role>.md for Claude Code and Cursor, .toml for Codex.
"""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORMATS = {"claude": "md", "codex": "toml", "cursor": "md"}
ACCESS = ("read-only", "edit")


def read_role(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.removeprefix("---\n").partition("\n---\n")
    if not text.startswith("---\n") or not sep:
        raise ValueError(f"{path}: missing frontmatter")
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    if (
        fields.get("name") != path.stem
        or fields.get("access") not in ACCESS
        or not fields.get("description")
    ):
        raise ValueError(
            f"{path}: needs name = file name, a description and access in {ACCESS}"
        )
    return fields, body.strip() + "\n"


def scalar(value: object) -> str:
    # JSON strings and booleans are valid YAML and TOML values.
    return json.dumps(value, ensure_ascii=False)


def render(fmt: str, fields: dict[str, str], body: str, settings: dict) -> str:
    values = {"name": fields["name"], "description": fields["description"]}
    values |= settings.get("role", {}).get(fields["name"], {})
    values |= settings.get("access", {}).get(fields["access"], {})
    if fmt == "toml":
        lines = [f"{key} = {scalar(value)}" for key, value in values.items()]
        escaped = body.replace("\\", "\\\\").replace('"""', '""\\"')
        return "\n".join(lines) + f'\ndeveloper_instructions = """\n{escaped}"""\n'
    lines = [f"{key}: {scalar(value)}" for key, value in values.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body


def outputs(root: Path = ROOT) -> dict[Path, str]:
    roles = [read_role(path) for path in sorted((root / "agents").glob("*.md"))]
    result = {}
    for tool, fmt in FORMATS.items():
        platform = root / "platforms" / tool
        settings = tomllib.loads(
            (platform / "subagents.toml").read_text(encoding="utf-8")
        )
        for fields, body in roles:
            result[platform / "agents" / f"{fields['name']}.{fmt}"] = render(
                fmt, fields, body, settings
            )
    return result


def main() -> None:
    for path, text in outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
