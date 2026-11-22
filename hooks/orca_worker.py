"""Identify a dispatched worker from Orca's local terminal handle."""

import json
import os
import subprocess


def is_worker():
    terminal = os.environ.get("ORCA_TERMINAL_HANDLE")
    if not terminal:
        return False
    cli = os.environ.get("ORCA_CLI_COMMAND") or (
        "orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else "orca"
    )
    args = [
        cli,
        "orchestration",
        "worker-list",
        "--environment",
        "local",
        "--limit",
        "100",
        "--json",
    ]
    page = []
    try:
        while True:
            output = subprocess.check_output(
                args + page, stderr=subprocess.DEVNULL, timeout=2
            )
            result = json.loads(output)["result"]
            if any(
                worker.get("terminal") == terminal
                and worker.get("outcome") == "in_progress"
                for worker in result["workers"]
            ):
                return True
            cursor = result.get("nextCursor")
            if not cursor:
                return False
            page = ["--cursor", str(cursor)]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return False
