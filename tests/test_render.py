"""Golden-output tests for the text renderer and the JSON schema."""
import json
import os

import pytest

import whystuck
from helpers import proc, snap

GOLDEN = os.path.join(os.path.dirname(__file__), "golden")

SCENARIOS = {
    "confirm": snap(tail="Reading package lists... Done\nDo you want to continue? [Y/n] ",
                    procs=[proc("apt-get", root=True, pid=4242, tty_read=True, target="/dev/pts/3")],
                    command=["apt-get", "install", "ripgrep"]),
    "pager": snap(tail="commit 9f3c2ab\n(END)", procs=[proc("git", root=True, pid=4242),
                                                       proc("less", pid=4243, tty_read=True, target="/dev/tty")],
                  command=["git", "log"]),
    "server": snap(tail="Serving HTTP on 0.0.0.0 port 8000 ...",
                   sockets=[{"pid": 4242, "state": "LISTEN", "local": "0.0.0.0:8000", "remote": "0.0.0.0:0"}],
                   command=["python3", "-m", "http.server"]),
}


def render(name):
    v = whystuck.classify(SCENARIOS[name]).to_dict()
    st = whystuck.Style(color=False, unicode_ok=False)
    return whystuck.render_text(v, idle_seconds=20, pid=4242, command=SCENARIOS[name]["command"], style=st)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_text_golden(name):
    path = os.path.join(GOLDEN, f"{name}.txt")
    got = render(name)
    if os.environ.get("WHYSTUCK_UPDATE_GOLDEN"):
        with open(path, "w") as f:
            f.write(got)
    with open(path) as f:
        assert got == f.read()


def test_color_off_has_no_escapes_and_color_on_has():
    v = whystuck.classify(SCENARIOS["confirm"]).to_dict()
    plain = whystuck.render_text(v, idle_seconds=20, pid=1, command=None, style=whystuck.Style(False, True))
    colored = whystuck.render_text(v, idle_seconds=20, pid=1, command=None, style=whystuck.Style(True, True))
    assert "\x1b[" not in plain and "\x1b[" in colored


def test_verdict_json_schema():
    d = whystuck.classify(SCENARIOS["confirm"]).to_dict()
    assert set(d) == {"id", "title", "stuck", "confidence", "summary", "evidence", "fixes", "alternatives"}
    assert isinstance(d["stuck"], bool) and isinstance(d["evidence"], list) and isinstance(d["fixes"], list)
    json.dumps(d)


def test_rule_table_is_well_formed():
    ids = [r.id for r in whystuck.RULES]
    assert len(ids) == len(set(ids))
    assert ids[-1] == "unknown"
    for r in whystuck.RULES:
        assert r.doc and r.title
