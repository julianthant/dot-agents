"""Install the dev-auto LaunchAgent from a plain shell after binding the existing Run."""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import account_usage
import tick


def launch_agent(args):
    with (tick.HERE.parent / "com.relay.dev-auto.plist").open("rb") as source:
        plist = plistlib.load(source)
    tools = [shutil.which(tool) for tool in (tick.ORCA, "gh", "git")]
    if not all(tools):
        raise tick.OrcaError("orca, gh and git must be on PATH during installation")
    program = [sys.executable, str(tick.HERE / "tick.py"), "scheduled", "--anchor", args.anchor, "--run", args.run]
    for path in args.repo_paths:
        program.extend(("--repo-path", str(path.resolve(strict=True))))
    log = str(tick.SNAPSHOT.with_name("tick.log"))
    paths = dict.fromkeys([*(str(Path(tool).parent) for tool in tools), "/usr/bin", "/bin"])
    return dict(plist, ProgramArguments=program, StandardOutPath=log, StandardErrorPath=log,
                EnvironmentVariables={"PATH": os.pathsep.join(paths), "ORCA_CLI_COMMAND": tools[0],
                                      "DEV_AUTO_ACCOUNTS": str(account_usage.ACCOUNTS.absolute())})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--repo-path", action="append", type=Path, dest="repo_paths", required=True)
    args = parser.parse_args()
    try:
        tick.supervisor(args)
        plist = launch_agent(args)
        if not account_usage.ACCOUNTS.is_file():
            raise tick.OrcaError("set DEV_AUTO_ACCOUNTS to the fleet's existing accounts.toml")
        domain = f"gui/{os.getuid()}"
        label = plist["Label"]
        service = f"{domain}/{label}"
        launchctl = "/bin/launchctl"
        if subprocess.run([launchctl, "print", service], capture_output=True).returncode == 0:
            parser.exit(1, f"{label} is loaded. Disable and boot it out before reinstalling.\n")
        path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        tick.SNAPSHOT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        with tmp.open("wb") as target:
            plistlib.dump(plist, target)
        tmp.replace(path)
        subprocess.run([launchctl, "enable", service], check=True)
        subprocess.run([launchctl, "bootstrap", domain, str(path)], check=True)
    except (OSError, tick.OrcaError, ValueError, subprocess.SubprocessError) as e:
        parser.exit(1, f"install: {e}\n")
    print(f"{service} installed. Tick log: {plist['StandardOutPath']}")


if __name__ == "__main__":
    main()
