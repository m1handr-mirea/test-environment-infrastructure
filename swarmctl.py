"""Run a two-node Swarm lab and reconcile trusted GitHub pull requests."""
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
LAB = ["docker", "compose", "-f", "swarm/lab.yaml"]
PORTS = range(18080, 18088)
PR_PREFIX = "course-pr-"


def run(*args, capture=False, data=None, env=None):
    return subprocess.run(
        args, cwd=ROOT, check=True, input=data, env=env,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def read(*args):
    return run(*args, capture=True).decode().strip()


def node(role, *args):
    return [*LAB, "exec", "-T", role, "docker", *args]


def manager(*args):
    return node("manager", *args)


def wait_for(callback, description, timeout=100):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = callback()
            if result:
                return result
        except (subprocess.CalledProcessError, OSError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {description}")


def bootstrap():
    run(*LAB, "up", "-d")
    for role in ("manager", "worker"):
        wait_for(
            lambda: read(*node(role, "info", "--format", "{{.Swarm.LocalNodeState}}")),
            f"{role} Docker daemon",
        )
    address = read(*LAB, "exec", "-T", "manager", "hostname", "-i").split()[0]
    manager_state = read(*manager("info", "--format", "{{.Swarm.LocalNodeState}}"))
    if manager_state == "inactive":
        run(*manager("swarm", "init", "--advertise-addr", address), capture=True)
    worker_state = read(*node("worker", "info", "--format", "{{.Swarm.LocalNodeState}}"))
    if worker_state == "inactive":
        token = read(*manager("swarm", "join-token", "-q", "worker"))
        run(*node("worker", "swarm", "join", "--token", token, address + ":2377"),
            capture=True)
    wait_for(
        lambda: read(*manager("node", "ls", "--filter", "role=worker",
                              "--format", "{{.Status}}")) == "Ready",
        "worker membership",
    )
    run(*manager("node", "ls", "--format", "table {{.Hostname}}\t{{.Status}}\t{{.ManagerStatus}}"))


def json_lines(*args):
    return [json.loads(line) for line in read(*args).splitlines()]


def services():
    return json_lines(*manager("service", "ls", "--format", "{{json .}}"))


def gateway(stack, current):
    return next((service for service in current if service["Name"] == stack + "_gateway"), None)


def published(service):
    match = re.search(r":(\d+)->80/tcp", service["Ports"])
    return int(match[1]) if match else None


def local_url(port):
    endpoint = read(*LAB, "port", "manager", str(port))
    return "http://" + endpoint


def http_json(url, path):
    with urlopen(url + path, timeout=3) as response:
        return json.load(response)


def ready(stack, revision, url):
    current = services()
    expected = {"gateway": "1/1", "api": "2/2", "store": "1/1"}
    replicas = {s["Name"]: s["Replicas"] for s in current}
    if any(replicas.get(f"{stack}_{name}") != replicas_expected
           for name, replicas_expected in expected.items()):
        return False
    for name in ("gateway", "api"):
        images = read(*manager(
            "service", "ps", "--filter", "desired-state=running",
            "--format", "{{.Image}}", f"{stack}_{name}",
        )).splitlines()
        if len(images) != int(expected[name][0]) or any(
            image.split("@")[0] != image_name(name, revision) for image in images
        ):
            return False
    if http_json(url, "/api/version") != {"preview": stack, "revision": revision}:
        return False
    if http_json(url, "/api/health") != {"status": "ok"}:
        return False
    with urlopen(url, timeout=3) as response:
        return response.status == 200


def image_name(service, revision):
    return f"registry:5000/course-{service}:{revision}"


def build(revision):
    for service, tree, dockerfile in (
        ("gateway", revision, "gateway/Dockerfile"),
        ("api", f"{revision}:app", "Dockerfile"),
    ):
        archive = run("git", "archive", tree, capture=True)
        image = image_name(service, revision)
        run(*manager("build", "--network=host", "-t", image, "-f", dockerfile, "-"),
            data=archive)
        run(*manager("push", image))


def up(stack, revision):
    current = services()
    existing = gateway(stack, current)
    port = published(existing) if existing else None
    if port is None:
        used = {published(service) for service in current}
        port = next((candidate for candidate in PORTS if candidate not in used), None)
        if port is None:
            raise RuntimeError("All eight preview ports are in use")
    url = local_url(port)
    image = image_name("gateway", revision)
    if existing and existing["Image"].split("@")[0] == image and ready_safely(stack, revision, url):
        return url
    build(revision)
    name = stack + "_redis_password"
    names = read(*manager("secret", "ls", "--format", "{{.Name}}")).splitlines()
    if name not in names:
        run(*manager("secret", "create", name, "-"), data=secrets.token_hex(24).encode())
    run(
        *LAB, "exec", "-T", "-e", f"STACK={stack}", "-e", f"REVISION={revision}",
        "-e", f"PREVIEW_PORT={port}", "manager", "docker", "stack", "deploy",
        "--detach=true", "-c", "-", stack,
        data=(ROOT / "swarm/stack.yaml").read_bytes(),
    )
    wait_for(lambda: ready(stack, revision, url), f"{stack} at {revision[:12]}", timeout=150)
    return url


def ready_safely(stack, revision, url):
    try:
        return ready(stack, revision, url)
    except (OSError, ValueError):
        return False


def down(stack):
    names = read(*manager("stack", "ls", "--format", "{{.Name}}")).splitlines()
    if stack in names:
        run(*manager("stack", "rm", stack))
    volume = stack + "_preview_data"
    secret = stack + "_redis_password"
    for kind, resource in (("volume", volume), ("secret", secret)):
        names = read(*manager(kind, "ls", "--format", "{{.Name}}")).splitlines()
        if resource in names:
            wait_for(lambda: read(*manager(kind, "rm", resource)), resource + " removal")


def stack_id(label):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,24}", label):
        raise ValueError("Invalid environment name")
    return "course-" + label


def synchronize(repo, previous):
    pages = json.loads(read("gh", "api", "--paginate", "--slurp",
                            f"repos/{repo}/pulls?state=open&base=main&per_page=100"))
    desired = {}
    for pull in (pull for page in pages for pull in page):
        head = pull["head"]
        if ((head.get("repo") or {}).get("full_name") == repo
                and pull["user"]["login"] == repo.split("/", 1)[0]):
            desired[pull["number"]] = head["sha"]

    existing = read(*manager("stack", "ls", "--format", "{{.Name}}")).splitlines()
    for stack in existing:
        match = re.fullmatch(PR_PREFIX + r"(\d+)", stack)
        if match and int(match[1]) not in desired:
            down(stack)
            previous.pop(int(match[1]), None)
            print(f"PR #{match[1]} removed", flush=True)

    for number, revision in desired.items():
        stack = f"{PR_PREFIX}{number}"
        if previous.get(number) == revision:
            continue
        # Fetch the exact PR ref from the allowed repository. A moving ref is
        # retried on the next poll instead of deploying an unverified commit.
        run("git", "fetch", "--no-tags", f"https://github.com/{repo}.git",
            f"+refs/pull/{number}/head:refs/course-pr/{number}")
        if read("git", "rev-parse", f"refs/course-pr/{number}") != revision:
            continue
        url = up(stack, revision)
        previous[number] = revision
        print(f"PR #{number} ready {revision} {url}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "up", "down", "watch", "status"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    state = ROOT / ".state"
    state.mkdir(mode=0o700, exist_ok=True)
    with (state / "swarm.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "bootstrap":
            bootstrap()
        elif args.action == "status":
            run(*manager("node", "ls"))
            run(*manager("service", "ls"))
        elif args.action == "up":
            revision = read("git", "rev-parse", "--verify", args.revision + "^{commit}")
            print(up(stack_id(args.name or ""), revision))
        elif args.action == "down":
            down(stack_id(args.name or ""))
        else:
            repo = args.name or ""
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                parser.error("watch requires an owner/repository")
            previous = {}
            while True:
                try:
                    synchronize(repo, previous)
                except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                    if args.once:
                        raise
                    print(f"Sync will retry: {error}", file=sys.stderr, flush=True)
                if args.once:
                    return
                time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
