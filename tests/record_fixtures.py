#!/usr/bin/env python3
"""Re-record real snapshots from live processes (Linux). Output: tests/fixtures/recorded/*.json

    python tests/record_fixtures.py

Each file stores the evidence snapshot whystuck collected plus the rule id we expect.
test_recorded.py then replays them through classify(), so rule changes are checked
against *real* /proc data, not only hand-written dicts.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures", "recorded")
PY = sys.executable

CASES = {
    "confirm-python-input": ("confirm", [PY, "-c", "input('Proceed? [y/N] ')"]),
    "stdin-wait-sh-read": ("stdin_wait", ["sh", "-c", "read x"]),
    "stdin-wait-cat": ("stdin_wait", ["cat"]),
    "secret-getpass": ("secret", [PY, "-c", "import getpass; getpass.getpass('Password: ')"]),
    "watch-text": ("watch", ["sh", "-c", "echo 'Watching for file changes...'; read x"]),
    "follow-tail-f": ("follow", ["tail", "-f", "/dev/null"]),
    "server-http": ("server", [PY, "-m", "http.server", "0", "--bind", "127.0.0.1"]),
    "sleeping": ("sleeping", ["sleep", "30"]),
    "busy-spin": ("busy", [PY, "-c", "while True: pass"]),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (expect, cmd) in CASES.items():
        p = subprocess.run([PY, os.path.join(ROOT, "whystuck.py"), "--json", "--include-snapshot", "--idle", "1",
                            "--timeout", "4", "--kill", "--"] + cmd, capture_output=True)
        doc = json.loads(p.stdout)
        v = doc["verdicts"][0]
        assert v["id"] == expect, (name, v["id"])
        with open(os.path.join(OUT, name + ".json"), "w") as f:
            json.dump({"name": name, "expect": expect, "snapshot": v["snapshot"]}, f, indent=1, sort_keys=True)
        print("recorded", name, "->", expect)


if __name__ == "__main__":
    main()
