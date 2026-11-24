"""Manage a developer preview on the selected Docker host."""
import argparse
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


def compose(project, env_file, *args, capture=False):
    return subprocess.run(
        ["docker", "compose", "--project-name", project, "--env-file",
         str(env_file), *args],
        cwd=ROOT, check=True, text=True, capture_output=capture,
    ).stdout


def check(project, env_file):
    address = compose(project, env_file, "port", "gateway", "80", capture=True)
    url = "http://" + address.strip()
    for path in ("/", "/api/health"):
        for attempt in range(10):
            try:
                with urlopen(url + path, timeout=3) as response:
                    if path == "/api/health" and json.load(response) != {"status": "ok"}:
                        raise ValueError("Unexpected health response")
                break
            except (OSError, ValueError):
                if attempt == 9:
                    raise
                time.sleep(1)
    print(url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "status", "check", "logs", "down"))
    parser.add_argument("id", help="An environment label, e.g. feature-a")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,29}", args.id):
        parser.error("id must contain 1–30 lowercase Latin letters, digits or hyphens")

    project = "course-preview-" + args.id
    state = ROOT / ".state"
    state.mkdir(mode=0o700, exist_ok=True)
    env_file = state / (args.id + ".env")
    with (state / (args.id + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "up" and not env_file.exists():
            with open(env_file, "x", opener=private_open) as env:
                env.write(f"REDIS_PASSWORD={secrets.token_hex(24)}\n")
        if not env_file.exists():
            if args.action == "down":
                return
            parser.error("environment has not been created")

        if args.action == "up":
            compose(project, env_file, "config", "--quiet")
            compose(project, env_file, "up", "--build", "--wait", "--wait-timeout", "150")
            check(project, env_file)
        elif args.action == "check":
            check(project, env_file)
        elif args.action == "status":
            compose(project, env_file, "ps", "-a")
        elif args.action == "logs":
            compose(project, env_file, "logs", "--tail", "50")
        elif args.action == "down":
            compose(project, env_file, "down", "--volumes", "--remove-orphans")
            env_file.unlink()


def private_open(path, flags):
    return os.open(path, flags, 0o600)


if __name__ == "__main__":
    os.environ.pop("REDIS_PASSWORD", None)
    try:
        main()
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"Environment operation failed: {error}", file=sys.stderr)
        sys.exit(1)
