"""Run the acceptance scenarios against two disposable previews."""
import json
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CLI = Path(__file__).with_name("envctl.py")


def run(action, preview):
    return subprocess.check_output(
        [sys.executable, str(CLI), action, preview], text=True
    ).strip()


def marker(url, label=None):
    data = None if label is None else json.dumps({"label": label}).encode()
    request = Request(
        url + "/api/marker", data, {"Content-Type": "application/json"},
        method="GET" if data is None else "PUT",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)
    print("PASS", description, flush=True)


def verify(first, second):
    a = run("up", first).splitlines()[-1]
    b = run("up", second).splitlines()[-1]
    print(f"Preview {first}: {a}\nPreview {second}: {b}", flush=True)
    expect(a != b, "separate HTTP ports")
    expect(marker(a) == {"preview": "course-preview-" + first, "label": ""},
           "first preview identity and empty state")
    expect(marker(b) == {"preview": "course-preview-" + second, "label": ""},
           "second preview identity and empty state")
    expect(marker(a, "only in A")["label"] == "only in A",
           "marker written through gateway and API")
    expect(marker(b)["label"] == "", "write in A leaves B unchanged")
    try:
        marker(a, " ")
    except HTTPError as error:
        expect(error.code == 400, "blank label rejected with HTTP 400")
    else:
        raise RuntimeError("blank label was accepted")

    a = run("up", first).splitlines()[-1]
    expect(marker(a)["label"] == "only in A", "repeated up retains data")
    run("down", first)
    try:
        marker(a)
    except URLError as error:
        expect(not isinstance(error, HTTPError), "deleted preview is unreachable")
    else:
        raise RuntimeError("deleted preview still responds")
    expect(marker(b)["label"] == "", "second preview survives removal of first")
    a = run("up", first).splitlines()[-1]
    expect(marker(a)["label"] == "", "recreated preview starts with empty data")


if __name__ == "__main__":
    suffix = secrets.token_hex(4)
    first, second = f"check-a-{suffix}", f"check-b-{suffix}"
    try:
        verify(first, second)
    finally:
        try:
            run("down", first)
        finally:
            run("down", second)
