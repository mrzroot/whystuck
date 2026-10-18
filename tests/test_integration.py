"""Integration tests: real child processes under a real PTY. Linux only (they read /proc)."""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "whystuck.py")
PY = sys.executable

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs Linux /proc and a PTY")


def run_ws(*args, env=None, input=None, timeout=60):
    e = dict(os.environ)
    e.pop("CI", None)
    if env:
        e.update(env)
    return subprocess.run([PY, WS, *args], capture_output=True, env=e, input=input, timeout=timeout)


def run_json(*args, **kw):
    p = run_ws("--json", *args, **kw)
    assert p.stdout, p.stderr
    return p, json.loads(p.stdout)


def first_verdict(doc):
    assert doc["verdicts"], doc
    return doc["verdicts"][0]


def gone(pid):
    """True when no process with this pid exists any more (a zombie counts as NOT gone)."""
    return not os.path.exists(f"/proc/{pid}")


def make_stub(tmp_path, name, body):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text(f"#!{PY}\nimport os, sys\n{body}\n")
    f.chmod(0o755)
    return str(d)


# ---- exit codes and plumbing ---------------------------------------------------------

def test_output_passthrough_and_exit_zero():
    p = run_ws("--", "echo", "hello")
    assert p.returncode == 0
    assert p.stdout.strip() == b"hello"


@pytest.mark.parametrize("script,code", [("exit 7", 7), ("exit 0", 0), ("kill -9 $$", 137), ("kill -15 $$", 143)])
def test_exit_code_passthrough(script, code):
    assert run_ws("--", "sh", "-c", script).returncode == code


def test_command_not_found_is_127():
    p = run_ws("--", "definitely-not-a-command-xyz")
    assert p.returncode == 127
    assert b"definitely-not-a-command-xyz" in p.stdout  # message arrives through the pty


def test_diagnosis_goes_to_stderr_stdout_stays_clean():
    p = run_ws("--idle", "1", "--kill", "--", PY, "-c", "input('Proceed? [y/N] ')")
    assert p.returncode == 125
    assert b"whystuck" not in p.stdout
    assert b"Waiting for a yes/no answer" in p.stderr
    assert b"Proceed? [y/N]" in p.stdout


def test_timeout_is_124():
    t0 = time.time()
    p = run_ws("--idle", "100", "--timeout", "1.5", "--", "sleep", "30")
    assert p.returncode == 124
    assert time.time() - t0 < 10


def test_piped_stdin_is_forwarded():
    p = run_ws("--", "sh", "-c", "read a; echo got:$a", input=b"y\n")
    assert p.returncode == 0 and b"got:y" in p.stdout


def test_stdin_null_gives_eof_instead_of_stall():
    p = run_ws("--idle", "3", "--stdin", "null", "--", PY, "-c", "import sys; sys.stdin.read(); print('saw-eof')")
    assert p.returncode == 0 and b"saw-eof" in p.stdout


def test_stdin_closed():
    p = run_ws("--idle", "3", "--stdin", "closed", "--", PY, "-c",
               "import os\ntry:\n os.read(0,1)\nexcept OSError as e:\n print('closed', e.errno)")
    assert p.returncode == 0 and b"closed" in p.stdout


def test_without_kill_the_command_keeps_running_and_exit_code_passes_through():
    p, doc = run_json("--idle", "1", "--", PY, "-c", "import time; time.sleep(2.5); print('done'); raise SystemExit(3)")
    assert p.returncode == 3 and doc["status"] == "exited" and doc["child_exit_code"] == 3
    assert first_verdict(doc)["id"] != "" and "done" in doc["output_tail"]


# ---- real detections --------------------------------------------------------------------

def test_confirm_prompt_json_and_cleanup():
    p, doc = run_json("--idle", "1", "--kill", "--", PY, "-c", "input('Proceed? [y/N] ')")
    v = first_verdict(doc)
    assert (p.returncode, doc["status"], doc["exit_code"]) == (125, "stuck", 125)
    assert v["id"] == "confirm" and v["stuck"] is True and v["confidence"] == "high"
    assert any("blocked in read()" in e for e in v["evidence"])
    assert doc["tool"] == "whystuck" and doc["stuck"] is True
    assert gone(doc["pid"])


def test_plain_read_is_stdin_wait():
    _, doc = run_json("--idle", "1", "--kill", "--", "sh", "-c", "read x")
    assert first_verdict(doc)["id"] == "stdin_wait"


def test_cat_is_stdin_wait():
    _, doc = run_json("--idle", "1", "--kill", "--", "cat")
    assert first_verdict(doc)["id"] == "stdin_wait"


def test_password_prompt_via_getpass_tty():
    _, doc = run_json("--idle", "1", "--kill", "--", PY, "-c", "import getpass; getpass.getpass('[sudo] password for bob: ')")
    v = first_verdict(doc)
    assert v["id"] == "secret" and v["confidence"] == "high"


def test_pager_stub_on_path(tmp_path):
    d = make_stub(tmp_path, "less", "sys.stdout.write('line\\n(END)'); sys.stdout.flush(); os.read(os.open('/dev/tty', os.O_RDONLY), 1)")
    _, doc = run_json("--idle", "1", "--kill", "--", "sh", "-c", "echo hi; less", env={"PATH": d + os.pathsep + os.environ["PATH"]})
    v = first_verdict(doc)
    assert v["id"] == "pager" and any("less" in e for e in v["evidence"])


def test_editor_stub_on_path(tmp_path):
    d = make_stub(tmp_path, "vim", "f = open(sys.argv[1]); os.read(os.open('/dev/tty', os.O_RDONLY), 1)")
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text("x")
    _, doc = run_json("--idle", "1", "--kill", "--", "sh", "-c", f"vim {msg}", env={"PATH": d + os.pathsep + os.environ["PATH"]})
    v = first_verdict(doc)
    assert v["id"] == "editor"
    assert any("COMMIT_EDITMSG" in e for e in v["evidence"])


def test_watch_mode_text():
    _, doc = run_json("--idle", "1", "--kill", "--", "sh", "-c",
                      "echo ' PASS  Waiting for file changes...'; echo 'press h to show help, press q to quit'; read x")
    assert first_verdict(doc)["id"] == "watch"


def test_alt_screen_is_tracked_from_raw_output():
    _, doc = run_json("--idle", "1", "--kill", "--include-snapshot", "--", "sh", "-c", "printf '\\033[?1049h'; read x")
    snap = first_verdict(doc)["snapshot"]
    assert snap["alt_screen"] is True


def test_server_is_not_stuck_and_keeps_running():
    t0 = time.time()
    p, doc = run_json("--idle", "1", "--timeout", "4", "--kill", "--", PY, "-m", "http.server", "0", "--bind", "127.0.0.1")
    v = first_verdict(doc)
    assert v["id"] == "server" and v["stuck"] is False
    assert doc["status"] == "timeout" and p.returncode == 124  # --kill did NOT stop it: it is not stuck
    assert doc["stuck"] is False
    assert any("127.0.0.1:" in e for e in v["evidence"])
    assert time.time() - t0 < 15
    assert gone(doc["pid"])


def test_sleep_is_not_stuck():
    _, doc = run_json("--idle", "1", "--timeout", "3", "--kill", "--", "sleep", "30")
    v = first_verdict(doc)
    assert v["id"] == "sleeping" and v["stuck"] is False


def test_busy_loop_is_not_stuck():
    _, doc = run_json("--idle", "1", "--timeout", "4", "--kill", "--", PY, "-c", "while True: pass")
    v = first_verdict(doc)
    assert v["id"] == "busy" and v["stuck"] is False


def test_tail_follow():
    _, doc = run_json("--idle", "1", "--kill", "--", "tail", "-f", "/dev/null")
    assert first_verdict(doc)["id"] == "follow"


def test_lock_names_the_holder(tmp_path):
    lock = tmp_path / "lock-frontend"
    lock.write_text("")
    holder = subprocess.Popen([PY, "-c", f"import fcntl,time; f=open({str(lock)!r},'a'); fcntl.flock(f, fcntl.LOCK_EX); time.sleep(60)"])
    try:
        import fcntl
        fd = os.open(str(lock), os.O_RDWR)
        for _ in range(100):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                time.sleep(0.03)
            except OSError:
                break
        os.close(fd)
        waiter = (f"import fcntl,sys; print('Waiting for cache lock: Could not get lock {lock}', flush=True); "
                  f"f=open({str(lock)!r},'a'); fcntl.flock(f, fcntl.LOCK_EX)")
        _, doc = run_json("--idle", "1", "--kill", "--", PY, "-c", waiter)
        v = first_verdict(doc)
        assert v["id"] == "lock"
        assert any(f"pid {holder.pid}" in e for e in v["evidence"]), v["evidence"]
        assert any("blocked in flock" in e for e in v["evidence"])
    finally:
        holder.kill()
        holder.wait()


def _full_backlog_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(0)
    fillers = []
    for _ in range(4):
        c = socket.socket()
        c.setblocking(False)
        c.connect_ex(srv.getsockname())
        fillers.append(c)
    return srv, fillers


def test_network_connect_stall_syn_sent():
    srv, fillers = _full_backlog_listener()
    try:
        port = srv.getsockname()[1]
        code = f"import socket; s=socket.socket(); s.connect(('127.0.0.1', {port}))"
        _, doc = run_json("--idle", "1", "--kill", "--", PY, "-c", code)
        v = first_verdict(doc)
        if v["id"] != "network":
            pytest.skip("kernel did not leave the connect in SYN_SENT (full-backlog trick unsupported here)")
        assert any(f"127.0.0.1:{port}" in e and "SYN_SENT" in e for e in v["evidence"])
        assert any(f"nc -vz -w 5 127.0.0.1 {port}" in f for f in v["fixes"])
    finally:
        for s in fillers + [srv]:
            s.close()


# ---- cleanup guarantees -------------------------------------------------------------------

def test_kill_takes_down_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    _, doc = run_json("--idle", "1", "--kill", "--", "sh", "-c", f"sleep 300 & echo $! > {pidfile}; read x")
    assert doc["status"] == "stuck"
    gc = int(pidfile.read_text())
    deadline = time.time() + 3
    while not gone(gc) and time.time() < deadline:
        time.sleep(0.05)
    assert gone(gc), "grandchild still present (alive or zombie)"
    assert gone(doc["pid"])


def test_no_stray_processes_after_timeout(tmp_path):
    pidfile = tmp_path / "gc.pid"
    p, doc = run_json("--idle", "100", "--timeout", "1.5", "--", "sh", "-c", f"sleep 300 & echo $! > {pidfile}; wait")
    assert p.returncode == 124
    gc = int(pidfile.read_text())
    deadline = time.time() + 3
    while not gone(gc) and time.time() < deadline:
        time.sleep(0.05)
    assert gone(gc)


# ---- explain / demo / rules / platform -----------------------------------------------------

def test_explain_running_process_read_only(tmp_path):
    import pty
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("cat", ["cat"])
    try:
        time.sleep(0.5)
        p = run_ws("--explain", str(pid), "--json")
        doc = json.loads(p.stdout)
        v = doc["verdict"]
        assert doc["pid"] == pid and v["id"] == "stdin_wait" and p.returncode == 125
        # read-only: the process is still alive and untouched
        assert os.path.exists(f"/proc/{pid}")
        # with the output text supplied, a prompt can be recognised
        tf = str(tmp_path / "tail.txt")
        with open(tf, "w") as f:
            f.write("Proceed? [y/N] ")
        doc2 = json.loads(run_ws("--explain", str(pid), "--json", "--tail-file", tf).stdout)
        assert doc2["verdict"]["id"] == "confirm"
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


def test_explain_unknown_pid():
    assert run_ws("--explain", "999999").returncode == 1


def test_diagnose_api_on_live_process():
    import pty
    import whystuck
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("sh", ["sh", "-c", "read x"])
    try:
        time.sleep(0.5)
        v = whystuck.diagnose(pid, tail="Continue? [Y/n] ", sample=0.1)
        json.dumps(v)
        assert v["id"] == "confirm"
    finally:
        os.kill(pid, 9)
        os.waitpid(pid, 0)


def test_demo_runs_all_scenarios_as_expected():
    p = run_ws("--demo", "--color", "never", "--idle", "1.2", timeout=120)
    out = p.stdout.decode()
    assert p.returncode == 0, out
    assert "Summary: 8/8 scenarios diagnosed as expected." in out
    assert "MISMATCH" not in out


def test_rules_listing():
    p = run_ws("--rules")
    assert p.returncode == 0
    for rid in ("editor", "pager", "secret", "confirm", "watch", "lock", "network", "server", "unknown"):
        assert rid.encode() in p.stdout


def test_usage_without_command():
    p = run_ws()
    assert p.returncode == 2


def test_unsupported_platform_message(monkeypatch, capsys):
    sys.path.insert(0, ROOT)
    import whystuck
    monkeypatch.setattr(whystuck, "pty", None)
    assert whystuck.main(["--", "echo", "x"]) == 2
    assert "Windows" in capsys.readouterr().err


def test_ps_collector_fallback_sees_the_tree(monkeypatch):
    """The macOS/BSD code path, exercised on Linux via WHYSTUCK_COLLECTOR=ps."""
    import shutil
    if not shutil.which("ps"):
        pytest.skip("no ps")
    import whystuck
    monkeypatch.setenv("WHYSTUCK_COLLECTOR", "ps")
    child = subprocess.Popen(["sh", "-c", "sleep 20; true"])
    try:
        time.sleep(0.5)
        snap = whystuck.collect_snapshot(child.pid, sample=0)
        names = {p["comm"] for p in snap["processes"]}
        assert "sleep" in names
        assert snap["capabilities"]["collector"] == "ps" and snap["capabilities"]["syscall"] is False
        assert whystuck.classify(snap).id == "sleeping"
    finally:
        child.kill()
        child.wait()
