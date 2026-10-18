"""Pure-function tests: classify() on hand-built snapshots (no processes started)."""
import pytest

import whystuck
from helpers import proc, snap

GIT_LOG = ["git", "log"]
FLOCK = {"nr": 73, "name": "flock", "args": [3, 2]}

CASES = [
    # (name, snapshot, expected rule id)
    # --- editor
    ("editor-vim-commit", snap(procs=[proc("git", root=True), proc("vim", ["vim", "/r/.git/COMMIT_EDITMSG"], 101, tty_read=True,
                                       open_paths=["/r/.git/COMMIT_EDITMSG"])], command=["git", "commit"]), "editor"),
    ("editor-nano-only", snap(procs=[proc("nano", root=True)]), "editor"),
    ("editor-code-wait", snap(procs=[proc("code", ["code", "--wait", "msg.txt"], root=True)]), "editor"),
    ("editor-git-hint-text", snap(tail="hint: Waiting for your editor to close the file...\n"), "editor"),
    ("editor-code-without-wait-is-not-editor", snap(procs=[proc("code", ["code", "."], root=True)]), "unknown"),
    # --- pager
    ("pager-less-proc", snap(procs=[proc("git", root=True), proc("less", pid=101, tty_read=True)], command=GIT_LOG), "pager"),
    ("pager-end-text", snap(tail="line1\nline2\n(END)"), "pager"),
    ("pager-more-text", snap(tail="page\n--More--(42%)"), "pager"),
    ("pager-lines-status", snap(tail="foo\nlines 1-23/50 "), "pager"),
    ("pager-delta-proc", snap(procs=[proc("delta", root=True)]), "pager"),
    ("pager-word-end-is-not-pager", snap(tail="This is the END of the story"), "unknown"),
    # --- secret
    ("secret-sudo", snap(tail="[sudo] password for bob: "), "secret"),
    ("secret-ssh-passphrase", snap(tail="Enter passphrase for key '/home/u/.ssh/id_ed25519': "), "secret"),
    ("secret-password-colon", snap(tail="Password:"), "secret"),
    ("secret-git-username", snap(tail="Username for 'https://github.com':"), "secret"),
    ("secret-mfa-code", snap(tail="Enter MFA code"), "secret"),
    ("secret-verification-code", snap(tail="Verification code: "), "secret"),
    ("secret-pinentry-proc", snap(procs=[proc("gpg", root=True), proc("pinentry-curses", pid=101)]), "secret"),
    ("secret-negative-policy-sentence", snap(tail="Password policy updated successfully."), "unknown"),
    # --- confirm
    ("confirm-yN", snap(tail="Proceed? [y/N] "), "confirm"),
    ("confirm-Yn-apt", snap(tail="Do you want to continue? [Y/n] "), "confirm"),
    ("confirm-ssh-host-key", snap(tail="Are you sure you want to continue connecting (yes/no/[fingerprint])? "), "confirm"),
    ("confirm-paren-yn", snap(tail="Overwrite existing file? (y/n)"), "confirm"),
    ("confirm-press-enter", snap(tail="Press Enter to continue"), "confirm"),
    ("confirm-bare-y-not-recognised-documented-limit", snap(tail="Ok to proceed? (y) "), "unknown"),  # documented limit: bare (y) is not recognised
    ("confirm-answered-earlier-is-not-a-prompt", snap(tail="Proceed? [y/N] y\nInstalling...\nDone"), "unknown"),
    ("confirm-no-question-mark", snap(tail="Installing packages, please continue to wait"), "unknown"),
    # --- watch
    ("watch-text-vitest", snap(tail="PASS  Waiting for file changes...\n press h to show help, press q to quit\n"), "watch"),
    ("watch-vitest-proc", snap(procs=[proc("node", ["node", "/x/node_modules/.bin/vitest"], root=True)]), "watch"),
    ("watch-vitest-run-is-not-watch", snap(procs=[proc("node", ["node", "/x/vitest.mjs", "run"], root=True)]), "unknown"),
    ("watch-top", snap(procs=[proc("top", root=True)]), "watch"),
    ("watch-procps-watch", snap(procs=[proc("watch", ["watch", "-n1", "date"], root=True)]), "watch"),
    ("watch-jest-flag", snap(procs=[proc("node", ["node", "jest", "--watch"], root=True)]), "watch"),
    ("watch-alt-screen-only", snap(alt=True), "watch"),
    ("watch-tsc-w", snap(procs=[proc("node", ["node", "tsc", "-w"], root=True)]), "watch"),
    # --- follow
    ("follow-tail-f", snap(procs=[proc("tail", ["tail", "-f", "app.log"], root=True)]), "follow"),
    ("follow-tail-n-is-not-follow", snap(procs=[proc("tail", ["tail", "-n", "20", "app.log"], root=True)]), "unknown"),
    ("follow-journalctl", snap(procs=[proc("journalctl", ["journalctl", "-f"], root=True)]), "follow"),
    ("follow-docker-logs", snap(procs=[proc("docker", ["docker", "logs", "-f", "web"], root=True)]), "follow"),
    # --- lock
    ("lock-apt-text", snap(tail="Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 99"), "lock"),
    ("lock-flock-syscall", snap(procs=[proc("flock", root=True, syscall=FLOCK)]), "lock"),
    # --- network
    ("network-syn-sent", snap(sockets=[{"pid": 100, "state": "SYN_SENT", "local": "10.0.0.2:5555", "remote": "10.255.255.1:443"}]), "network"),
    ("network-established-is-not-network", snap(sockets=[{"pid": 100, "state": "ESTABLISHED", "local": "a:1", "remote": "b:2"}]), "unknown"),
    # --- not stuck
    ("server-listen", snap(sockets=[{"pid": 100, "state": "LISTEN", "local": "0.0.0.0:3000", "remote": "0.0.0.0:0"}]), "server"),
    ("server-prompt-wins", snap(tail="Overwrite? [y/N] ", sockets=[{"pid": 100, "state": "LISTEN", "local": "0.0.0.0:3000", "remote": "x"}]), "confirm"),
    ("sleeping", snap(procs=[proc("sh", root=True), proc("sleep", ["sleep", "30"], pid=101)]), "sleeping"),
    ("busy-high-cpu", snap(cpu=0.95), "busy"),
    ("busy-below-threshold", snap(cpu=0.05), "unknown"),
    # --- generic stdin
    ("stdin-wait-plain-read", snap(procs=[proc("sh", root=True, tty_read=True)], tail="working..."), "stdin_wait"),
    ("stdin-wait-inferred-low", snap(procs=[proc("cat", root=True, tty_read=True, inferred=True)]), "stdin_wait"),
    ("unknown-empty", snap(procs=[]), "unknown"),
]


@pytest.mark.parametrize("name,snapshot,expected", CASES, ids=[c[0] for c in CASES])
def test_expected_rule(name, snapshot, expected):
    v = whystuck.classify(snapshot)
    assert v.id == expected, (name, v.id, v.evidence)
    assert v.confidence in ("low", "medium", "high")
    assert v.evidence, "every verdict must carry evidence"
    assert v.fixes, "every verdict must carry at least one fix/next step"


def test_at_least_forty_cases():
    assert len(CASES) >= 40


@pytest.mark.parametrize("rule", [r.id for r in whystuck.RULES if r.id != "unknown"])
def test_every_rule_is_needed(rule):
    """Mutation-style: removing any rule must change the outcome of at least one case."""
    without = [r for r in whystuck.RULES if r.id != rule]
    changed = [name for name, s, exp in CASES if exp == rule and whystuck.classify(s, without).id != rule]
    assert changed, f"no test case depends on rule {rule!r}"


def test_every_rule_has_a_positive_case():
    covered = {exp for _, _, exp in CASES}
    assert {r.id for r in whystuck.RULES} <= covered | {"unknown"}


def test_confidence_levels():
    both = snap(tail="Proceed? [y/N] ", procs=[proc("python3", root=True, tty_read=True)])
    text_only = snap(tail="Proceed? [y/N] ")
    assert whystuck.classify(both).confidence == "high"
    assert whystuck.classify(text_only).confidence == "medium"
    assert whystuck.classify(snap(procs=[proc("cat", root=True, tty_read=True, inferred=True)])).confidence == "low"
    assert whystuck.classify(snap(procs=[])).confidence == "low"


def test_unknown_never_pretends():
    v = whystuck.classify(snap(tail="compiling...", procs=[proc("rustc", root=True)]))
    assert v.id == "unknown" and v.confidence == "low"
    assert "NOT a diagnosis" in v.summary
    assert any("rustc" in e for e in v.evidence)


def test_stuck_flag_matches_rule_table():
    table = {r.id: r.stuck for r in whystuck.RULES}
    for name, s, exp in CASES:
        assert whystuck.classify(s).stuck == table[exp]


def test_alternatives_are_reported():
    s = snap(tail="Proceed? [y/N] ", alt=True)
    v = whystuck.classify(s)
    assert v.id == "confirm"
    assert {"id": "watch", "confidence": "low"} in v.alternatives


# ---- tool-specific fixes ------------------------------------------------------

def test_git_log_pager_fix():
    v = whystuck.classify(snap(procs=[proc("git", root=True), proc("less", pid=101)], command=GIT_LOG))
    assert "git --no-pager log" in v.fixes
    assert "GIT_PAGER=cat PAGER=cat git log" in v.fixes


def test_git_commit_editor_fix():
    v = whystuck.classify(snap(procs=[proc("git", root=True), proc("vim", pid=101)], command=["git", "commit"]))
    assert 'git commit -m "your message"' in v.fixes
    assert "GIT_EDITOR=true git commit   # accept the template message" in v.fixes


def test_git_rebase_editor_fix():
    v = whystuck.classify(snap(procs=[proc("git", root=True), proc("nano", pid=101)], command=["git", "rebase", "-i", "HEAD~3"]))
    assert any(f.startswith("GIT_SEQUENCE_EDITOR=true") for f in v.fixes)


def test_apt_confirm_fix():
    v = whystuck.classify(snap(tail="Do you want to continue? [Y/n] ", command=["apt-get", "install", "jq"]))
    assert "DEBIAN_FRONTEND=noninteractive apt-get install -y jq" in v.fixes


def test_ssh_hostkey_fix():
    v = whystuck.classify(snap(tail="Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
                               command=["ssh", "host", "ls"]))
    assert any("StrictHostKeyChecking=accept-new" in f for f in v.fixes)


def test_sudo_fix():
    v = whystuck.classify(snap(tail="[sudo] password for bob: ", command=["sudo", "systemctl", "restart", "x"]))
    assert any(f.startswith("sudo -n systemctl restart x") for f in v.fixes)


def test_git_credentials_fix():
    v = whystuck.classify(snap(tail="Username for 'https://github.com':", command=["git", "push"]))
    assert any(f.startswith("GIT_TERMINAL_PROMPT=0 git push") for f in v.fixes)


def test_vitest_fix_through_node_script():
    v = whystuck.classify(snap(procs=[proc("node", ["node", "/p/node_modules/vitest/vitest.mjs"], root=True)],
                               command=["npx", "vitest"]))
    assert "vitest run   # single run instead of watch mode" in v.fixes


def test_network_fix_mentions_endpoint_and_proxy_names_not_values():
    v = whystuck.classify(snap(sockets=[{"pid": 100, "state": "SYN_SENT", "local": "a:1", "remote": "1.2.3.4:443"}],
                               proxy=["HTTPS_PROXY"], command=["pip", "install", "x"]))
    text = "\n".join(v.fixes + v.evidence)
    assert "1.2.3.4:443" in text and "HTTPS_PROXY" in text
    assert any("--timeout 10" in f for f in v.fixes)


def test_lock_holder_is_named():
    v = whystuck.classify(snap(tail="Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend",
                               holders={"/var/lib/dpkg/lock-frontend": [{"pid": 4242, "comm": "unattended-upgr"}]},
                               command=["apt-get", "update"]))
    assert any("4242" in e and "unattended-upgr" in e for e in v.evidence)
    assert any("DPkg::Lock::Timeout" in f for f in v.fixes)


def test_stale_lock_is_flagged(tmp_path):
    lock = tmp_path / "index.lock"
    lock.write_text("")
    v = whystuck.classify(snap(tail=f"Waiting for lock {lock}", holders={str(lock): []}, command=["git", "status"]))
    assert any("stale" in e for e in v.evidence)
    assert any(f.startswith("if no git process is running") for f in v.fixes)


# ---- text utilities ---------------------------------------------------------------

def test_clean_text_strips_ansi_and_carriage_returns():
    raw = b"\x1b[31mred\x1b[0m\r\nprogress 10%\rprogress 100%\r\n\x1b]0;title\x07done\x1b[?1049h"
    assert whystuck.clean_text(raw) == "red\nprogress 100%\ndone"


def test_alt_screen_tracking():
    st = False
    st = whystuck._alt_screen_state(st, b"hello\x1b[?1049h")
    assert st is True
    st = whystuck._alt_screen_state(st, b"more output")
    assert st is True
    st = whystuck._alt_screen_state(st, b"\x1b[?1049l bye")
    assert st is False


def test_script_view_looks_through_interpreters():
    p = proc("node", ["node", "/usr/lib/node_modules/npm/bin/npm-cli.js", "test"])
    assert whystuck._script_view(p) == ("npm", ["test"])
    assert whystuck._script_view(proc("python3", ["python3", "-m", "http.server", "8000"])) == ("http.server", ["8000"])
    assert whystuck._script_view(proc("python3", ["python3", "-c", "print(1)"]))[0] == "python3"


def test_import_api_is_json_serialisable():
    import json
    v = whystuck.classify(snap(tail="Proceed? [y/N] "))
    json.dumps(v.to_dict())
