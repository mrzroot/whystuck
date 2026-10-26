#!/usr/bin/env python3
"""whystuck - run a command with an idle watchdog; when it stalls, say why.

    whystuck [--idle 20] [--timeout 600] [--kill] [--json] -- COMMAND [ARGS...]

The command runs inside a pseudo-terminal. When it prints nothing for ``--idle``
seconds, whystuck inspects the live process tree (``/proc`` on Linux) together
with the tail of the output, classifies the stall with an ordered rule table
(see ``RULES``), and prints the evidence plus a non-interactive fix.

Library use::

    from whystuck import diagnose
    verdict = diagnose(pid, tail="Proceed? [y/N] ")   # JSON-serialisable dict

Stdlib only. Python 3.9+. Linux first; macOS/BSD best effort; no native Windows.
"""
from __future__ import annotations

import argparse
import dataclasses
import errno
import json
import os
import platform
import re
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:  # POSIX only; importing the module must still work elsewhere
    import fcntl
    import pty
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    fcntl = pty = select = termios = tty = None  # type: ignore

__version__ = "0.1.0"

EXIT_USAGE = 2
EXIT_TIMEOUT = 124   # --timeout reached (same as GNU timeout)
EXIT_STUCK = 125     # stall diagnosed and the command was stopped (--kill)
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127

Snapshot = Dict[str, Any]

# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"            # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0-2AB]"                   # charset
    r"|\x1b[@-Z\\-_]"                     # other 2-byte escapes
)
_ALT_ON = re.compile(rb"\x1b\[\?(?:1049|1047|47)h")
_ALT_OFF = re.compile(rb"\x1b\[\?(?:1049|1047|47)l")


def clean_text(raw: Any) -> str:
    """Terminal output -> plain text: strip escapes, resolve CR overwrites."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = _ANSI_RE.sub("", raw).replace("\r\n", "\n")
    lines = [ln.rsplit("\r", 1)[-1] for ln in raw.split("\n")]
    text = "\n".join(lines)
    return "".join(c for c in text if c == "\n" or c == "\t" or ord(c) >= 32)


def _alt_screen_state(prev: bool, chunk: bytes) -> bool:
    on = [m.end() for m in _ALT_ON.finditer(chunk)]
    off = [m.end() for m in _ALT_OFF.finditer(chunk)]
    if not on and not off:
        return prev
    return max(on, default=-1) > max(off, default=-1)


def _last_lines(snap: Snapshot, n: int = 1) -> List[str]:
    lines = [ln.strip() for ln in (snap.get("tail") or "").split("\n") if ln.strip()]
    return lines[-n:]


def _last_line(snap: Snapshot) -> str:
    ll = _last_lines(snap, 1)
    return ll[0] if ll else ""


def _clip(s: str, n: int = 100) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# Process helpers shared by rules
# --------------------------------------------------------------------------- #

_INTERPRETERS = re.compile(
    r"^(python[\d.]*|pypy[\d.]*|node(js)?|bun|deno|ruby|perl|php|sh|bash|dash|zsh|ksh|fish|env)$"
)
_SCRIPT_ALIASES = {"npm-cli": "npm", "npx-cli": "npx", "yarn": "yarn", "pnpm": "pnpm"}
_CODE_FLAGS = {"-c", "-e", "-p", "--eval", "--print"}


def _script_view(proc: Dict[str, Any]) -> Tuple[str, List[str]]:
    """(tool name, its args) for a process, looking through interpreters."""
    argv = list(proc.get("argv") or [])
    if not argv:
        return (proc.get("comm") or "").lower(), []
    base = os.path.basename(argv[0]).lower()
    if _INTERPRETERS.match(base):
        i = 1
        while i < len(argv):
            a = argv[i]
            if a in _CODE_FLAGS:
                return base, argv[i + 1:]
            if a == "-m" and i + 1 < len(argv):
                return argv[i + 1].lower(), argv[i + 2:]
            if a.startswith("-"):
                i += 1
                continue
            name = os.path.basename(a).lower()
            name = re.sub(r"\.(m?js|cjs|py|sh|rb|pl)$", "", name)
            return _SCRIPT_ALIASES.get(name, name), argv[i + 1:]
    return base, argv[1:]


def _names(proc: Dict[str, Any]) -> set:
    names = {(proc.get("comm") or "").lower()}
    if proc.get("argv"):
        names.add(os.path.basename(proc["argv"][0]).lower())
    names.add(_script_view(proc)[0])
    names.discard("")
    return names


def _procs(snap: Snapshot) -> List[Dict[str, Any]]:
    return snap.get("processes") or []


def _find(snap: Snapshot, names: set) -> Optional[Dict[str, Any]]:
    for p in _procs(snap):
        if _names(p) & names:
            return p
    return None


def _root(snap: Snapshot) -> Optional[Dict[str, Any]]:
    for p in _procs(snap):
        if p.get("is_root"):
            return p
    ps = _procs(snap)
    return ps[0] if ps else None


def _cmd_argv(snap: Snapshot) -> List[str]:
    if snap.get("command"):
        return list(snap["command"])
    r = _root(snap)
    if not r:
        return []
    name, args = _script_view(r)
    return [name] + args if name else []


def _tool(snap: Snapshot) -> str:
    argv = _cmd_argv(snap)
    return os.path.basename(argv[0]).lower() if argv else ""


def _sub(argv: Sequence[str]) -> str:
    """First non-option argument after the tool name (git commit -> 'commit')."""
    skip = False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a in ("-C", "-c"):
            skip = True
            continue
        if not a.startswith("-"):
            return a
    return ""


def _blocking_tty(snap: Snapshot) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for p in _procs(snap):
        br = p.get("blocking_read")
        if br and br.get("tty"):
            return p, br
    return None


def _shell(argv: Sequence[str], env: str = "") -> str:
    base = shlex.join(argv) if argv else "<your command>"
    return (env + " " + base) if env else base


def _insert_after_tool(argv: Sequence[str], flag: str) -> List[str]:
    return [argv[0], flag] + list(argv[1:])


def _tty_evidence(snap: Snapshot) -> Optional[str]:
    b = _blocking_tty(snap)
    if not b:
        return None
    p, br = b
    how = "inferred from wchan" if br.get("inferred") else "blocked in read()"
    return f"pid {p['pid']} ({p.get('comm')}): {how} on fd {br['fd']} -> {br['target']} (terminal input)"


# --------------------------------------------------------------------------- #
# Verdicts and rules
# --------------------------------------------------------------------------- #

def _conf(points: int) -> str:
    return "high" if points >= 3 else "medium" if points == 2 else "low"


@dataclass
class Hit:
    points: int
    summary: str
    evidence: List[str]
    fixes: List[str]


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    stuck: bool          # True: the command will not finish without intervention
    doc: str             # one line, shown by `whystuck --rules`
    check: Callable[[Snapshot], Optional[Hit]]


@dataclass
class Verdict:
    id: str
    title: str
    stuck: bool
    confidence: str
    summary: str
    evidence: List[str]
    fixes: List[str]
    alternatives: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# ---- rule: editor --------------------------------------------------------- #
_EDITORS = {"vim", "vi", "nvim", "view", "vimdiff", "nano", "pico", "emacs", "emacsclient",
            "micro", "joe", "jed", "kak", "hx", "helix", "ee"}
_GUI_EDITORS = {"code", "code-insiders", "codium", "subl", "mate", "gedit", "zed", "atom"}
_EDIT_FILE_RE = re.compile(
    r"(COMMIT_EDITMSG|MERGE_MSG|TAG_EDITMSG|git-rebase-todo|addp-hunk-edit\.diff|EDIT_DESCRIPTION)(\.sw[a-z])?$")
_EDITOR_HINT_RE = re.compile(r"(?i)waiting for your editor to close the file")


def _find_editor(snap: Snapshot) -> Optional[Dict[str, Any]]:
    for p in _procs(snap):
        names = _names(p)
        if names & _EDITORS:
            return p
        if names & _GUI_EDITORS and set(p.get("argv") or []) & {"--wait", "-w", "-W"}:
            return p
    return None


def _check_editor(s: Snapshot) -> Optional[Hit]:
    ed = _find_editor(s)
    hint = bool(_EDITOR_HINT_RE.search("\n".join(_last_lines(s, 3))))
    if not ed and not hint:
        return None
    ev, pts = [], 0
    edfile = None
    if ed:
        pts += 2
        ev.append(f"descendant process is an editor: {ed.get('comm')} (pid {ed['pid']})")
        for path in list(ed.get("open_paths") or []) + list((ed.get("argv") or [])[1:]):
            if _EDIT_FILE_RE.search(path):
                edfile = path
                break
        if edfile:
            pts += 1
            ev.append(f"it is editing a git/editor temp file: {edfile}")
    if hint:
        pts += 2
        ev.append('output says: "Waiting for your editor to close the file..."')
    t = _tty_evidence(s)
    if ed and t:
        pts += 1
        ev.append(t)
    elif ed and s.get("alt_screen"):
        pts += 1
        ev.append("output switched the terminal to the alternate screen (full-screen UI)")
    argv, tool = _cmd_argv(s), _tool(s)
    sub = _sub(argv) if tool == "git" else ""
    fixes: List[str] = []
    if tool == "git" and sub == "commit":
        fixes += ['git commit -m "your message"', "git commit -F message.txt   # message from a file",
                  "GIT_EDITOR=true " + _shell(argv) + "   # accept the template message"]
    elif tool == "git" and sub == "rebase":
        fixes += ["GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true " + _shell(argv)]
    elif tool == "git" and sub == "merge":
        fixes += [_shell(argv + ["--no-edit"])]
    elif tool == "git" and sub == "tag":
        fixes += ['git tag -a NAME -m "message"']
    else:
        fixes += ["EDITOR=true VISUAL=true " + _shell(argv)]
    fixes += ["export GIT_EDITOR=true EDITOR=true   # once, for the whole session"]
    return Hit(pts, f"`{_shell(argv) if argv else 'the command'}` opened an editor and is waiting for it to be closed.",
               ev, fixes)


# ---- rule: pager ---------------------------------------------------------- #
_PAGERS = {"less", "more", "most", "pg", "bat", "batcat", "delta", "moar", "nvimpager", "lv"}
_PAGER_TEXT_RE = re.compile(r"(\(END\)\s*$|--More--|^lines \d+-\d+(/\d+)?( \(END\))?\s*$)")


def _check_pager(s: Snapshot) -> Optional[Hit]:
    p = _find(s, _PAGERS)
    text = next((ln for ln in _last_lines(s, 2) if _PAGER_TEXT_RE.search(ln)), None)
    if not p and not text:
        return None
    ev, pts = [], 0
    if p:
        pts += 2
        ev.append(f"descendant process is a pager: {p.get('comm')} (pid {p['pid']})")
    if text:
        pts += 2
        ev.append(f'last output line looks like a pager prompt: "{_clip(text)}"')
    t = _tty_evidence(s)
    if t:
        pts += 1
        ev.append(t)
    argv, tool = _cmd_argv(s), _tool(s)
    fixes: List[str] = []
    if tool == "git":
        fixes.append(shlex.join(_insert_after_tool(argv, "--no-pager")))
        fixes.append(_shell(argv, "GIT_PAGER=cat PAGER=cat"))
    elif tool in ("systemctl", "journalctl", "loginctl", "timedatectl", "hostnamectl"):
        fixes.append(shlex.join(_insert_after_tool(argv, "--no-pager")))
        fixes.append(_shell(argv, "SYSTEMD_PAGER=cat"))
    elif tool == "man":
        fixes.append(_shell(argv, "MANPAGER=cat PAGER=cat"))
    elif tool == "aws":
        fixes.append(_shell(argv, 'AWS_PAGER=""'))
    else:
        fixes.append(_shell(argv, "PAGER=cat"))
        if argv:
            fixes.append(_shell(argv) + " | cat")
    fixes.append("export PAGER=cat GIT_PAGER=cat   # once, for the whole session")
    return Hit(pts, "Output was handed to a pager that waits for you to press q.", ev, fixes)


# ---- rule: secret prompt -------------------------------------------------- #
_SECRET_RES = [
    re.compile(r"(?i)\bpass(word|phrase|code)\b[^\n]*[:?]\s*$"),
    re.compile(r"(?i)\benter (pin|passphrase|password|otp|code|token|mfa)\b"),
    re.compile(r"(?i)\b(verification|authentication|security|one[- ]time|2fa|mfa|otp) (code|token)\b[^\n]*[:?]\s*$"),
    re.compile(r"(?i)^(username|user name|login)\b[^\n]*:\s*$"),
]
_SECRET_PROCS_PREFIX = ("pinentry", "ssh-askpass", "askpass", "git-askpass")


def _check_secret(s: Snapshot) -> Optional[Hit]:
    line = _last_line(s)
    text = any(r.search(line) for r in _SECRET_RES) if line else False
    pin = next((p for p in _procs(s) if (p.get("comm") or "").lower().startswith(_SECRET_PROCS_PREFIX)), None)
    if not text and not pin:
        return None
    ev, pts = [], 0
    if text:
        pts += 2
        ev.append(f'last output line asks for a credential: "{_clip(line)}"')
    if pin:
        pts += 2
        ev.append(f"descendant process is a credential helper: {pin.get('comm')} (pid {pin['pid']})")
    t = _tty_evidence(s)
    if t:
        pts += 1
        ev.append(t)
    names = set()
    for p in _procs(s):
        names |= _names(p)
    names.add(_tool(s))
    fixes: List[str] = []
    if "sudo" in names or "[sudo]" in line:
        fixes += ["sudo -n " + _shell(_cmd_argv(s)[1:] if _tool(s) == "sudo" else ["COMMAND"]) + "   # fail instead of prompting (needs NOPASSWD)",
                  "configure passwordless sudo for the specific command in /etc/sudoers.d/"]
    if names & {"ssh", "scp", "sftp", "rsync"}:
        fixes += ["ssh -o BatchMode=yes HOST ...   # never prompt; fail fast",
                  "eval \"$(ssh-agent -s)\" && ssh-add ~/.ssh/KEY   # load the key once, before the agent runs"]
    if "git" in names or "username for" in line.lower():
        fixes += ["GIT_TERMINAL_PROMPT=0 " + _shell(_cmd_argv(s)) + "   # fail instead of prompting",
                  "gh auth setup-git   # or configure a credential helper / use an SSH remote / set GH_TOKEN"]
    if names & {"gpg", "gpg2", "gpg-agent"} or pin:
        fixes += ["gpg --batch --pinentry-mode loopback --passphrase-fd 0 ...   # read the passphrase from stdin",
                  "preset the passphrase in gpg-agent before running"]
    if "docker" in names:
        fixes.append("docker login --password-stdin < token-file")
    fixes += ["pass the secret via an env var or file the tool supports; never paste it into an agent transcript"]
    return Hit(pts, "The command is asking for a password, passphrase or other credential.", ev, fixes)


# ---- rule: confirmation prompt -------------------------------------------- #
_CONFIRM_RES = [
    re.compile(r"\[[yY](es)?\s*/\s*[nN](o)?\]"),
    re.compile(r"\[[nN](o)?\s*/\s*[yY](es)?\]"),
    re.compile(r"\((y|yes)\s*/\s*(n|no)\)", re.I),
    re.compile(r"\(yes/no(/\[fingerprint\])?\)\??\s*$", re.I),
    re.compile(r"(?i)\b(are you sure|do you want to|would you like to|proceed|continue|overwrite|replace|remove|delete)\b[^\n]*\?\s*(\[[^\]]*\]\s*)?$"),
    re.compile(r"(?i)\bpress (enter|return|any key)\b[^\n]*$"),
]

_YES_FLAGS: Dict[str, str] = {
    "apt": "-y", "apt-get": "-y", "aptitude": "-y", "dnf": "-y", "yum": "-y", "zypper": "-n",
    "pacman": "--noconfirm", "conda": "-y", "mamba": "-y", "micromamba": "-y",
    "pip": "-y   # for `pip uninstall`", "pip3": "-y   # for `pip uninstall`",
    "npx": "--yes", "rm": "-f", "cp": "-f", "mv": "-f", "gcloud": "--quiet", "az": "--yes",
    "terraform": "-auto-approve", "flatpak": "-y", "snap": "--yes",
}


def _check_confirm(s: Snapshot) -> Optional[Hit]:
    line = _last_line(s)
    if not line or not any(r.search(line) for r in _CONFIRM_RES):
        return None
    ev, pts = [f'last output line is a yes/no style prompt: "{_clip(line)}"'], 2
    t = _tty_evidence(s)
    if t:
        pts += 1
        ev.append(t)
    argv, tool = _cmd_argv(s), _tool(s)
    fixes: List[str] = []
    if "(yes/no" in line.lower() and tool in ("ssh", "scp", "sftp", "rsync", "git"):
        fixes.append("ssh -o StrictHostKeyChecking=accept-new HOST ...   # accepts only *new* host keys")
    if tool in ("apt", "apt-get", "aptitude"):
        fixes.append(_shell(argv[:2] + ["-y"] + argv[2:], "DEBIAN_FRONTEND=noninteractive"))
    elif tool == "npm" and _sub(argv) in ("init", "create"):
        fixes.append(_shell(argv + ["-y"]))
    elif tool == "npm" and _sub(argv) in ("exec", "x"):
        fixes.append(_shell(argv[:2] + ["--yes"] + argv[2:]))
    elif tool == "docker" and "prune" in argv:
        fixes.append(_shell(argv + ["-f"]))
    elif tool == "git" and _sub(argv) == "clean":
        fixes.append(_shell(argv + ["-f"]))
    elif tool in _YES_FLAGS:
        fixes.append(_shell(argv[:1] + [_YES_FLAGS[tool].split()[0]] + argv[1:]) + "   # " + (_YES_FLAGS[tool].split("#", 1)[1].strip() if "#" in _YES_FLAGS[tool] else "non-interactive flag"))
    fixes += ["yes | " + _shell(argv) + "   # answers y to everything: only for commands you trust" if argv
              else "yes | COMMAND   # answers y to everything: only for commands you trust",
              f"check `{tool or 'COMMAND'} --help` for --yes / -y / --force / --non-interactive",
              "feed the answer instead: printf 'y\\n' | whystuck -- " + (_shell(argv) if argv else "COMMAND")]
    return Hit(pts, "The command is waiting for a yes/no (or Enter) answer.", ev, fixes)


# ---- rule: watch mode / TUI ----------------------------------------------- #
_WATCH_TEXT_RE = re.compile(
    r"(?i)watching for (file )?changes|watch usage|press [a-z] to (quit|show|open|run)|"
    r"waiting for (file )?changes|press h to show help|\[nodemon\] (watching|clean exit)")
_WATCH_TOOLS = {"vitest", "jest", "nodemon", "watch", "top", "htop", "btop", "glances", "ts-node-dev",
                "tsc-watch", "webpack-dev-server", "karma"}


def _watch_proc(s: Snapshot) -> Optional[Dict[str, Any]]:
    for p in _procs(s):
        name, args = _script_view(p)
        names = _names(p)
        a = set(args)
        if names & {"top", "htop", "btop", "glances", "watch", "nodemon", "ts-node-dev", "tsc-watch"}:
            return p
        if names & {"vitest"} and not a & {"run", "--run", "--watch=false"}:
            return p
        if names & {"jest", "karma"} and (a & {"--watch", "--watchAll"}):
            return p
        if names & {"tsc", "mocha", "webpack", "eslint"} and a & {"--watch", "-w"}:
            return p
    return None


def _check_watch(s: Snapshot) -> Optional[Hit]:
    text = next((ln for ln in _last_lines(s, 8) if _WATCH_TEXT_RE.search(ln)), None)
    proc = _watch_proc(s)
    alt = bool(s.get("alt_screen"))
    if not text and not proc and not alt:
        return None
    ev, pts = [], 0
    if text:
        pts += 2
        ev.append(f'recent output says it is watching: "{_clip(text)}"')
    if proc:
        pts += 1 if text else 2
        pname, pargs = _script_view(proc)
        ev.append(f"process looks like a watch/TUI tool: {pname} {_clip(' '.join(pargs), 60)}".rstrip())
    if alt:
        pts += 1
        ev.append("output switched the terminal to the alternate screen (full-screen UI)")
    argv, tool = _cmd_argv(s), _tool(s)
    names = set()
    for p in _procs(s):
        names |= _names(p)
    fixes: List[str] = []
    if "vitest" in names or tool == "vitest":
        fixes += ["vitest run   # single run instead of watch mode"]
    if "jest" in names or tool == "jest":
        fixes += [_shell(argv + ["--watchAll=false"]) if argv else "jest --watchAll=false", "CI=true jest"]
    if names & {"top"}:
        fixes += ["top -b -n 1   # batch mode, one iteration"]
    if names & {"htop", "btop", "glances"}:
        fixes += ["ps aux --sort=-%cpu | head   # non-interactive process list"]
    if "watch" in names:
        fixes += ["watch -g CMD   # exit when output changes", "run CMD once instead of under watch"]
    if names & {"nodemon", "ts-node-dev"}:
        fixes += ["run the program with plain `node` instead of a file watcher"]
    if names & {"tsc", "mocha", "webpack"}:
        fixes += ["drop --watch / -w for a one-shot run"]
    if tool in ("npm", "pnpm", "yarn"):
        fixes += [_shell(argv, "CI=true")]
    fixes += ["CI=true " + _shell(argv) + "   # many test runners disable watch mode when CI is set" if argv
              else "CI=true COMMAND   # many test runners disable watch mode when CI is set",
              "look for a one-shot flag in `COMMAND --help` (run / --once / --no-watch)"]
    return Hit(pts, "The tool is in watch / interactive full-screen mode and will never exit by itself.", ev, fixes)


# ---- rule: follow mode ---------------------------------------------------- #
def _follow_proc(s: Snapshot) -> Optional[Dict[str, Any]]:
    for p in _procs(s):
        name, args = _script_view(p)
        a = set(args)
        if name == "tail" and any(re.match(r"^-[a-zA-Z0-9]*[fF][a-zA-Z0-9]*$", x) or x == "--follow" for x in args):
            return p
        if name == "journalctl" and a & {"-f", "--follow"}:
            return p
        if name in ("docker", "podman", "kubectl") and "logs" in a and a & {"-f", "--follow"}:
            return p
        if name == "kubectl" and "get" in a and a & {"-w", "--watch"}:
            return p
    return None


def _check_follow(s: Snapshot) -> Optional[Hit]:
    p = _follow_proc(s)
    if not p:
        return None
    name, args = _script_view(p)
    return Hit(3, f"`{name}` is in follow mode: it waits for new lines forever by design.",
               [f"process: {name} {' '.join(args)} (pid {p['pid']})"],
               ["tail -n 100 FILE   # print the last lines and exit", "journalctl -n 100 --no-pager",
                "docker logs --tail 100 CONTAINER", "timeout 10 " + shlex.join([name] + args) + "   # bounded follow"])


# ---- rule: lock wait ------------------------------------------------------ #
_LOCK_TEXT_RE = re.compile(
    r"(?i)waiting for (the )?([\w ]+ )?lock|could not (get|acquire|obtain) (the )?lock|"
    r"unable to (acquire|obtain) [^\n]*lock|another (git )?process (seems to be|is) running|"
    r"is another process using it|lock file [^\n]* exists|held by process")


def _check_lock(s: Snapshot) -> Optional[Hit]:
    text = next((ln for ln in _last_lines(s, 3) if _LOCK_TEXT_RE.search(ln)), None)
    flock_p = None
    for p in _procs(s):
        sc = p.get("syscall") or {}
        if sc.get("name") in ("flock", "fcntl_setlkw") and p.get("state") in (None, "S", "D"):
            flock_p = p
            break
    if not text and not flock_p:
        return None
    ev, pts = [], 0
    if text:
        pts += 2
        ev.append(f'recent output mentions a lock: "{_clip(text)}"')
    if flock_p:
        pts += 2
        ev.append(f"pid {flock_p['pid']} ({flock_p.get('comm')}) is blocked in flock()")
    holders = s.get("lock_holders") or {}
    fixes: List[str] = []
    stale: List[str] = []
    for path, hs in holders.items():
        if hs:
            pts += 1
            for h in hs[:3]:
                ev.append(f"{path} is held open by pid {h['pid']} ({h.get('comm')})")
                fixes.append(f"wait for pid {h['pid']} ({h.get('comm')}) to finish; stop it only if it is yours and truly stuck: kill {h['pid']}")
        elif os.path.exists(path):
            stale.append(path)
    for path in stale:
        ev.append(f"{path} exists but no other process has it open (possibly a stale lock)")
        fixes.append(f"if no {_tool(s) or 'related'} process is running, remove the stale lock: rm {shlex.quote(path)}")
    tool = _tool(s)
    if tool in ("apt", "apt-get", "aptitude"):
        fixes.append(_shell(_cmd_argv(s)[:2] + ["-o", "DPkg::Lock::Timeout=120"] + _cmd_argv(s)[2:]) + "   # wait up to 2 min for the lock")
    fixes.append("flock -w 30 LOCKFILE COMMAND   # bounded wait for a lock you control")
    return Hit(pts, "The command is waiting for a lock that another process holds.", ev, fixes)


# ---- rule: network stall -------------------------------------------------- #
_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _check_network(s: Snapshot) -> Optional[Hit]:
    syn = [k for k in (s.get("sockets") or []) if k.get("state") == "SYN_SENT"]
    if not syn:
        return None
    ev = []
    for k in syn[:4]:
        who = f"pid {k.get('pid')}" if k.get("pid") else "a process in the tree"
        ev.append(f"{who} has a TCP connect() in progress to {k['remote']} (SYN_SENT: no reply yet)")
    first = syn[0]["remote"]
    host, _, port = first.rpartition(":")
    fixes = [f"nc -vz -w 5 {host} {port}   # does a plain TCP connect work from here?",
             f"curl -v --connect-timeout 5 {'https' if port == '443' else 'http'}://{host}:{port}/",
             ]
    proxies = [n for n in _PROXY_NAMES if n in (s.get("proxy_vars") or [])]
    if proxies:
        fixes.append("proxy variables are set in this environment (" + ", ".join(proxies)
                     + "; values not shown): check that the proxy is reachable, or unset them")
    else:
        fixes.append("no proxy variables are set: if this network needs a proxy or VPN, configure it")
    tool = _tool(s)
    if tool == "git":
        fixes.append(_shell(_cmd_argv(s), "GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=15") + "   # abort slow transfers")
    elif tool in ("pip", "pip3"):
        fixes.append(_shell(_cmd_argv(s) + ["--timeout", "10", "-i", "MIRROR_URL"]))
    elif tool == "npm":
        fixes.append(_shell(_cmd_argv(s) + ["--fetch-timeout=10000", "--registry=MIRROR_URL"]))
    fixes.append("try a mirror/registry mirror, another network, or check DNS/firewall for that address")
    return Hit(3, "A TCP connection is not being answered (host unreachable, filtered or very slow).", ev, fixes)


# ---- rule: long-running server -------------------------------------------- #
_SERVER_TEXT_RE = re.compile(r"(?i)serving http|listening on|listening at|started server|local:\s+http|server (is )?running|ready on")


def _check_server(s: Snapshot) -> Optional[Hit]:
    lis = [k for k in (s.get("sockets") or []) if k.get("state") == "LISTEN"]
    if not lis:
        return None
    ev = [f"listening TCP socket: {k['local']}" + (f" (pid {k['pid']})" if k.get("pid") else "") for k in lis[:3]]
    pts = 2
    text = next((ln for ln in _last_lines(s, 5) if _SERVER_TEXT_RE.search(ln)), None)
    if text:
        pts += 1
        ev.append(f'output says: "{_clip(text)}"')
    cmd = _shell(_cmd_argv(s)) if _cmd_argv(s) else "COMMAND"
    return Hit(pts, "It is running a server. It will keep running until stopped; that is normal, not a hang.", ev,
               [f"nohup {cmd} > server.log 2>&1 &   # start it in the background, then poll the port",
                "use a process manager / a second terminal for servers, and run tests or curl against it from another command",
                "agents: do not wait for a server command to exit; start it detached and check the port"])


# ---- rule: sleeping on purpose -------------------------------------------- #
def _check_sleeping(s: Snapshot) -> Optional[Hit]:
    for p in _procs(s):
        name, args = _script_view(p)
        if name == "sleep" or (p.get("comm") == "sleep"):
            return Hit(3, "A `sleep` is running: the command is waiting on purpose.",
                       [f"process: sleep {' '.join(args)} (pid {p['pid']})"],
                       ["wait for it, or lower the delay in the script that calls it"])
    return None


# ---- rule: busy ----------------------------------------------------------- #
def _check_busy(s: Snapshot) -> Optional[Hit]:
    cpu = s.get("cpu_fraction")
    if cpu is None or cpu < 0.25:
        return None
    return Hit(3 if cpu >= 0.5 else 2, f"Silent but busy: the process tree used about {cpu * 100:.0f}% of a CPU core while sampled.",
               [f"CPU use over the sample window: {cpu * 100:.0f}% of one core"],
               ["wait: long computations and builds often print nothing", "raise the threshold: whystuck --idle 120 -- ...",
                "if it never finishes it may be an infinite loop: `py-spy dump --pid PID` (Python) or attach a debugger"])


# ---- rule: waiting on stdin (generic) ------------------------------------- #
def _check_stdin_wait(s: Snapshot) -> Optional[Hit]:
    b = _blocking_tty(s)
    if not b:
        return None
    p, br = b
    line = _last_line(s)
    c = _shell(_cmd_argv(s)) if _cmd_argv(s) else "COMMAND"
    ev = [_tty_evidence(s) or ""]
    if line:
        ev.append(f'last output line (not a known prompt): "{_clip(line)}"')
    return Hit(1 if br.get("inferred") else 2,
               "It is blocked waiting for terminal input, but the output does not match a known prompt.", ev,
               [f"whystuck --stdin null -- {c}   # stdin is /dev/null: reads fail fast instead of blocking",
                f"{c} < /dev/null", f"printf 'answer\\n' | whystuck -- {c}   # feed the answers", f"CI=true {c}"])


# ---- fallback --------------------------------------------------------------- #
def _check_unknown(s: Snapshot) -> Optional[Hit]:
    ev = []
    for p in _procs(s)[:6]:
        ev.append(f"pid {p['pid']} {p.get('comm')} state={p.get('state')} wchan={p.get('wchan') or '-'}"
                  + (f" syscall={p['syscall']['name'] or p['syscall']['nr']}" if p.get("syscall") else ""))
    line = _last_line(s)
    if line:
        ev.append(f'last output line: "{_clip(line)}"')
    if not ev:
        ev.append("no process information available on this platform/permission level")
    pid = (_root(s) or {}).get("pid", "PID")
    return Hit(0, "No known pattern matched. Here is the raw evidence; this is NOT a diagnosis.", ev,
               [f"ls -l /proc/{pid}/fd   # what is it holding open?", f"strace -f -p {pid}   # what is it doing right now? (needs strace)",
                "re-run with a larger --idle if it may just be slow"])


RULES: Tuple[Rule, ...] = (
    Rule("editor", "Waiting in a text editor", True,
         "an editor (vim/nano/emacs/code --wait/...) is in the process tree, or git says it is waiting for the editor", _check_editor),
    Rule("pager", "Waiting in a pager", True,
         "a pager (less/more/bat/delta/...) is in the tree, or the last line is (END) / --More--", _check_pager),
    Rule("secret", "Waiting for a password / credential", True,
         "last output line asks for a password, passphrase, code or username, or a pinentry/askpass helper runs", _check_secret),
    Rule("confirm", "Waiting for a yes/no answer", True,
         "last output line is a [y/N], (yes/no), 'Proceed?', 'Press Enter' style prompt", _check_confirm),
    Rule("watch", "Stuck in watch / full-screen mode", True,
         "output says it is watching for changes, a known watch/TUI tool runs (vitest, jest, top, watch, ...), or the alternate screen is active", _check_watch),
    Rule("follow", "Follow mode never exits", True,
         "tail -f, journalctl -f, docker/kubectl logs -f, kubectl get -w", _check_follow),
    Rule("lock", "Waiting for a lock", True,
         "output mentions waiting for a lock, or a process is blocked in flock(); names the holder via /proc when visible", _check_lock),
    Rule("network", "Network connect not answered", True,
         "a TCP socket in the tree is in SYN_SENT (Linux /proc/net/tcp, or lsof elsewhere)", _check_network),
    Rule("server", "Running a server", False,
         "a TCP socket in the tree is in LISTEN and no prompt was found", _check_server),
    Rule("stdin_wait", "Waiting for terminal input", True,
         "a process is blocked reading a terminal, but no specific prompt was recognised", _check_stdin_wait),
    Rule("sleeping", "Sleeping on purpose", False,
         "a `sleep` process is in the tree", _check_sleeping),
    Rule("busy", "Busy computing", False,
         "the process tree used >= 25% of a CPU core during the sample window", _check_busy),
    Rule("unknown", "Unknown", True,
         "fallback: nothing matched; prints raw evidence only (confidence low)", _check_unknown),
)


def classify(snapshot: Snapshot, rules: Optional[Sequence[Rule]] = None) -> Verdict:
    """Pure function: snapshot -> Verdict. First matching rule wins; the rest are listed as alternatives."""
    rules = RULES if rules is None else tuple(rules)
    hits: List[Tuple[Rule, Hit]] = []
    for r in rules:
        h = r.check(snapshot)
        if h is not None:
            hits.append((r, h))
    if not hits:  # custom rule list without a fallback
        h = _check_unknown(snapshot)
        hits.append((Rule("unknown", "Unknown", True, "", _check_unknown), h))  # type: ignore[arg-type]
    rule, hit = hits[0]
    alts = [{"id": r.id, "confidence": _conf(h.points)} for r, h in hits[1:] if r.id not in ("unknown", "stdin_wait")]
    return Verdict(rule.id, rule.title, rule.stuck, _conf(hit.points), hit.summary,
                   [e for e in hit.evidence if e], list(dict.fromkeys(hit.fixes)), alts)


# --------------------------------------------------------------------------- #
# Collectors
# --------------------------------------------------------------------------- #

_SYSCALLS = {
    "x86_64": {0: "read", 7: "poll", 23: "select", 270: "pselect6", 271: "ppoll", 232: "epoll_wait", 281: "epoll_pwait",
               202: "futex", 43: "accept", 288: "accept4", 42: "connect", 45: "recvfrom", 73: "flock", 61: "wait4",
               35: "nanosleep", 230: "clock_nanosleep", 34: "pause", 72: "fcntl"},
    "aarch64": {63: "read", 73: "ppoll", 72: "pselect6", 22: "epoll_pwait", 98: "futex", 202: "accept", 242: "accept4",
                203: "connect", 207: "recvfrom", 32: "flock", 260: "wait4", 101: "nanosleep", 115: "clock_nanosleep",
                25: "fcntl"},
}
_READ_NR = {"x86_64": 0, "aarch64": 63}
_TTY_WCHAN = {"n_tty_read", "wait_woken", "tty_read"}
_TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2",
               "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}
_LOCK_PATH_RE = re.compile(r"(\.lock$|\.lck$|lock-frontend$|/lock$|/lockfile$|\.pid$)")
_LOCK_IN_TEXT_RE = re.compile(r"(/[^\s'\"`:]+(?:\.lock|\.lck|lock-frontend|/lock))")


def collector_name() -> str:
    forced = os.environ.get("WHYSTUCK_COLLECTOR")
    if forced:
        return forced
    return "linux-proc" if os.path.isdir("/proc/self") and sys.platform.startswith("linux") else "ps"


def _read_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _readlink(path: str) -> Optional[str]:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _stat_fields(pid: int) -> Optional[Dict[str, Any]]:
    data = _read_file(f"/proc/{pid}/stat")
    if not data:
        return None
    try:
        lp, rp = data.index("("), data.rindex(")")
        rest = data[rp + 2:].split()
        return {"pid": pid, "comm": data[lp + 1:rp], "state": rest[0], "ppid": int(rest[1]),
                "sid": int(rest[3]), "ticks": int(rest[11]) + int(rest[12])}
    except (ValueError, IndexError):
        return None


def _all_stats() -> Dict[int, Dict[str, Any]]:
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if n.isdigit():
            st = _stat_fields(int(n))
            if st:
                out[int(n)] = st
    return out


def _tree_members(stats: Dict[int, Dict[str, Any]], root: int, with_session: bool) -> List[int]:
    children: Dict[int, List[int]] = {}
    for st in stats.values():
        children.setdefault(st["ppid"], []).append(st["pid"])
    seen, order, stack = set(), [], [root]
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in stats:
            continue
        seen.add(pid)
        order.append(pid)
        stack.extend(sorted(children.get(pid, []), reverse=True))
    if with_session and root in stats and stats[root]["sid"] == root:
        for pid, st in sorted(stats.items()):
            if st["sid"] == root and pid not in seen:
                seen.add(pid)
                order.append(pid)
    return order


def _parse_addr(hexaddr: str) -> str:
    ip, port = hexaddr.split(":")
    port_i = int(port, 16)
    try:
        if len(ip) == 8:
            addr = socket.inet_ntop(socket.AF_INET, struct.pack("<I", int(ip, 16)))
            return f"{addr}:{port_i}"
        words = [ip[i:i + 8] for i in range(0, 32, 8)]
        packed = b"".join(struct.pack("<I", int(w, 16)) for w in words)
        return f"[{socket.inet_ntop(socket.AF_INET6, packed)}]:{port_i}"
    except (OSError, ValueError, struct.error):
        return f"{ip}:{port_i}"


def _tcp_table() -> Dict[str, Dict[str, str]]:
    table = {}
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        data = _read_file(fn)
        if not data:
            continue
        for line in data.splitlines()[1:]:
            f = line.split()
            if len(f) < 10:
                continue
            table[f[9]] = {"state": _TCP_STATES.get(f[3], f[3]), "local": _parse_addr(f[1]), "remote": _parse_addr(f[2])}
    return table


def _fd_scan(pid: int, limit: int = 512) -> Tuple[Dict[int, str], List[str]]:
    """fd -> link target, plus real file paths opened."""
    fds: Dict[int, str] = {}
    paths: List[str] = []
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return fds, paths
    for n in names[:limit]:
        t = _readlink(f"/proc/{pid}/fd/{n}")
        if t is None:
            continue
        fds[int(n)] = t
        if t.startswith("/") and not t.startswith(("/dev/", "/proc/", "/sys/")):
            paths.append(t.replace(" (deleted)", ""))
    return fds, paths


def _is_tty_target(t: Optional[str]) -> bool:
    return bool(t) and (t.startswith("/dev/pts/") or t.startswith("/dev/tty") or t == "/dev/console")


def _lock_holders(paths: Sequence[str], exclude: set) -> Dict[str, List[Dict[str, Any]]]:
    res: Dict[str, List[Dict[str, Any]]] = {}
    want = {}
    for p in paths:
        try:
            st = os.stat(p)
            want[p] = (st.st_dev, st.st_ino)
        except OSError:
            continue
        res[p] = []
    if not want:
        return res
    me = os.getpid()
    for n in os.listdir("/proc"):
        if not n.isdigit() or int(n) in exclude or int(n) == me:
            continue
        pid = int(n)
        try:
            fdnames = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fdn in fdnames:
            try:
                st = os.stat(f"/proc/{pid}/fd/{fdn}")
            except OSError:
                continue
            for p, key in want.items():
                if (st.st_dev, st.st_ino) == key:
                    comm = (_read_file(f"/proc/{pid}/comm") or "?").strip()
                    if not any(h["pid"] == pid for h in res[p]):
                        res[p].append({"pid": pid, "comm": comm})
    return res


def _collect_linux(root: int, *, session: bool, sample: float, tail: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[float], Dict[str, Any]]:
    stats = _all_stats()
    members = _tree_members(stats, root, session)
    arch = platform.machine()
    sysnames = _SYSCALLS.get(arch, {})
    read_nr = _READ_NR.get(arch)
    tcp = _tcp_table()
    procs: List[Dict[str, Any]] = []
    sockets: List[Dict[str, Any]] = []
    all_paths: List[str] = []
    syscall_ok = False
    for pid in members:
        st = stats[pid]
        cmdline = _read_file(f"/proc/{pid}/cmdline") or ""
        argv = [a for a in cmdline.split("\0") if a != ""] if cmdline else []
        wchan = (_read_file(f"/proc/{pid}/wchan") or "").strip()
        sc_raw = (_read_file(f"/proc/{pid}/syscall") or "").split()
        syscall = None
        if sc_raw and sc_raw[0] not in ("running",) and sc_raw[0].lstrip("-").isdigit():
            syscall_ok = True
            nr = int(sc_raw[0])
            if nr >= 0:
                try:
                    args = [int(x, 16) for x in sc_raw[1:7]]
                except ValueError:
                    args = []
                syscall = {"nr": nr, "name": sysnames.get(nr), "args": args}
        fds, paths = _fd_scan(pid)
        all_paths += paths
        stdin_t = fds.get(0)
        blocking = None
        if syscall and read_nr is not None and syscall["nr"] == read_nr and syscall["args"]:
            fd = syscall["args"][0]
            tgt = fds.get(fd) or _readlink(f"/proc/{pid}/fd/{fd}")
            if tgt:
                blocking = {"fd": fd, "target": tgt, "tty": _is_tty_target(tgt), "inferred": False}
        elif syscall is None and not sc_raw and wchan in _TTY_WCHAN and _is_tty_target(stdin_t) and st["state"] == "S":
            blocking = {"fd": 0, "target": stdin_t, "tty": True, "inferred": True}
        for target in fds.values():
            if target.startswith("socket:["):
                info = tcp.get(target[8:-1])
                if info:
                    sockets.append({"pid": pid, **info})
        procs.append({"pid": pid, "ppid": st["ppid"], "comm": st["comm"], "argv": argv, "state": st["state"],
                      "wchan": wchan, "syscall": syscall, "stdin": stdin_t, "stdin_tty": _is_tty_target(stdin_t),
                      "blocking_read": blocking, "cpu_ticks": st["ticks"], "open_paths": sorted(set(paths))[:20],
                      "is_root": pid == root})
    cpu = None
    if sample and sample > 0:
        t0 = time.monotonic()
        before = {p["pid"]: p["cpu_ticks"] for p in procs}
        time.sleep(sample)
        elapsed = time.monotonic() - t0
        delta = 0
        for pid, b in before.items():
            st = _stat_fields(pid)
            if st:
                delta += max(0, st["ticks"] - b)
        hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        cpu = delta / hz / elapsed if elapsed > 0 else None
    lock_paths = [p for p in all_paths if _LOCK_PATH_RE.search(p)]
    for m in _LOCK_IN_TEXT_RE.finditer(tail):
        if m.group(1) not in lock_paths:
            lock_paths.append(m.group(1))
    holders = _lock_holders(lock_paths[:8], set(members)) if lock_paths else {}
    caps = {"collector": "linux-proc", "syscall": syscall_ok, "sockets": True}
    return procs, sockets, cpu, {"capabilities": caps, "lock_holders": holders}


def _ps_rows() -> List[Dict[str, Any]]:
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat=,pcpu=,comm=,args="], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        f = line.split(None, 6)
        if len(f) < 6:
            continue
        try:
            rows.append({"pid": int(f[0]), "ppid": int(f[1]), "pgid": int(f[2]), "state": f[3][:1], "pcpu": float(f[4]),
                         "comm": os.path.basename(f[5]), "args": f[6] if len(f) > 6 else f[5]})
        except ValueError:
            continue
    return rows


def _collect_ps(root: int, *, session: bool, sample: float, tail: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[float], Dict[str, Any]]:
    """Best-effort collector for macOS / BSD (and forced on Linux for testing). No syscall data."""
    rows = {r["pid"]: r for r in _ps_rows()}
    children: Dict[int, List[int]] = {}
    for r in rows.values():
        children.setdefault(r["ppid"], []).append(r["pid"])
    members, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in rows and pid not in members:
            members.append(pid)
            stack.extend(children.get(pid, []))
    procs = []
    for pid in members:
        r = rows[pid]
        try:
            argv = shlex.split(r["args"])
        except ValueError:
            argv = r["args"].split()
        procs.append({"pid": pid, "ppid": r["ppid"], "comm": r["comm"], "argv": argv, "state": r["state"], "wchan": "",
                      "syscall": None, "stdin": None, "stdin_tty": False, "blocking_read": None, "cpu_ticks": 0,
                      "open_paths": [], "is_root": pid == root})
    sockets: List[Dict[str, Any]] = []
    sock_ok = False
    if members:
        try:
            out = subprocess.run(["lsof", "-nP", "-a", "-p", ",".join(map(str, members)), "-iTCP"],
                                 capture_output=True, text=True, timeout=8).stdout
            sock_ok = True
            for line in out.splitlines()[1:]:
                m = re.search(r"\s(\d+)\s.*TCP\s+(\S+?)(?:->(\S+))?\s+\((\w+)\)\s*$", line)
                if m:
                    pid, local, remote, state = int(m.group(1)), m.group(2), m.group(3) or "", m.group(4)
                    sockets.append({"pid": pid, "state": state, "local": local, "remote": remote or "*:*"})
        except (OSError, subprocess.SubprocessError):
            pass
    cpu = sum(rows[p]["pcpu"] for p in members) / 100.0 if members else None
    caps = {"collector": "ps", "syscall": False, "sockets": sock_ok}
    return procs, sockets, cpu, {"capabilities": caps, "lock_holders": {}}


def collect_snapshot(pid: int, *, command: Optional[Sequence[str]] = None, tail: Any = "", alt_screen: bool = False,
                     idle_seconds: Optional[float] = None, sample: float = 0.5, session: bool = False) -> Snapshot:
    """Gather everything classify() needs about *pid* and its descendants."""
    tail_text = clean_text(tail)[-4000:]
    fn = _collect_linux if collector_name() == "linux-proc" else _collect_ps
    try:
        procs, sockets, cpu, extra = fn(pid, session=session, sample=sample, tail=tail_text)
    except Exception as exc:  # never let diagnosis crash the run
        procs, sockets, cpu, extra = [], [], None, {"capabilities": {"collector": "error", "syscall": False, "sockets": False},
                                                    "lock_holders": {}, "warnings": [f"collector failed: {exc!r}"]}
    snap: Snapshot = {
        "version": 1, "platform": sys.platform, "root_pid": pid, "idle_seconds": idle_seconds,
        "command": list(command) if command else None, "tail": tail_text, "alt_screen": alt_screen,
        "processes": procs, "sockets": sockets, "cpu_fraction": cpu,
        "proxy_vars": [n for n in _PROXY_NAMES if n in os.environ],
    }
    snap.update(extra)
    return snap


def diagnose(pid: int, *, tail: Any = "", alt_screen: bool = False, command: Optional[Sequence[str]] = None,
             sample: float = 0.5, include_snapshot: bool = False) -> Dict[str, Any]:
    """Diagnose an already-running process (read-only). Returns a JSON-serialisable dict.

    Without the terminal output (``tail``) only process-level evidence is available:
    prompts such as "[y/N]" cannot be recognised from /proc alone.
    """
    snap = collect_snapshot(pid, command=command, tail=tail, alt_screen=alt_screen, sample=sample)
    v = classify(snap).to_dict()
    if include_snapshot:
        v["snapshot"] = snap
    return v


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

class Style:
    def __init__(self, color: bool, unicode_ok: bool):
        self.color, self.u = color, unicode_ok

    def c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    @property
    def bar(self) -> str:
        return "─" if self.u else "-"

    @property
    def bullet(self) -> str:
        return "•" if self.u else "*"

    @property
    def arrow(self) -> str:
        return "▸" if self.u else ">"


def make_style(mode: str, stream: Any) -> Style:
    if mode == "always":
        color = True
    elif mode == "never":
        color = False
    else:
        color = bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ \
            and os.environ.get("TERM") != "dumb"
    enc = (getattr(stream, "encoding", None) or "").lower()
    return Style(color, "utf" in enc)


def render_text(verdict: Dict[str, Any], *, idle_seconds: Optional[float], pid: Optional[int],
                command: Optional[Sequence[str]], style: Style, width: int = 64) -> str:
    st = style
    out: List[str] = []
    title = " whystuck "
    out.append(st.c("2", st.bar * 3 + title + st.bar * (width - len(title) - 3)))
    bits = []
    if idle_seconds is not None:
        bits.append(f"No output for {idle_seconds:.0f}s" if idle_seconds >= 10 else f"No output for {idle_seconds:.1f}s")
    if pid is not None:
        bits.append(f"pid {pid}")
    if command:
        bits.append(_clip(shlex.join(command), 40))
    if bits:
        out.append(" " + " · ".join(bits) if st.u else " " + " | ".join(bits))
    stuck = verdict["stuck"]
    unknown = verdict["id"] == "unknown"
    col = "33" if unknown else ("31" if stuck else "32")
    tag = "UNKNOWN" if unknown else ("STUCK" if stuck else "NOT STUCK")
    out.append(f" {st.arrow} " + st.c("1;" + col, f"{tag}: {verdict['title']}") + f"  [confidence: {verdict['confidence']}]")
    out.append("   " + verdict["summary"])
    if verdict.get("evidence"):
        out.append(st.c("1", " Evidence"))
        out += [f"   {st.bullet} {e}" for e in verdict["evidence"]]
    if verdict.get("fixes"):
        out.append(st.c("1", " Ways to investigate" if unknown else (" Fix (non-interactive)" if stuck else " If you need it to finish / not wait")))
        out += [f"   {st.bullet} " + st.c("36", f) for f in verdict["fixes"]]
    if verdict.get("alternatives"):
        out.append(st.c("2", " Also matched: " + ", ".join(f"{a['id']} ({a['confidence']})" for a in verdict["alternatives"])))
    out.append(st.c("2", st.bar * width))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Runner (PTY + idle watchdog)
# --------------------------------------------------------------------------- #

@dataclass
class RunResult:
    status: str                    # exited | stuck | stopped | timeout
    exit_code: int                 # what whystuck itself exits with
    child_exit_code: Optional[int]
    duration: float
    pid: int
    verdicts: List[Dict[str, Any]]
    output_tail: str


def _set_subreaper() -> bool:
    """Linux: adopt orphaned descendants so they can be reaped (no zombies after --kill)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except Exception:
        return False


def _set_winsize(fd: int) -> None:
    try:
        import shutil
        cols, rows = shutil.get_terminal_size(fallback=(100, 30))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def _alive(pid: int) -> bool:
    st = _stat_fields(pid) if os.path.isdir("/proc/self") else None
    if st is not None:
        return st["state"] not in ("Z", "X")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _members(pid: int) -> List[int]:
    if collector_name() == "linux-proc":
        return [m for m in _tree_members(_all_stats(), pid, True) if m != pid]
    rows = _ps_rows()
    kids: Dict[int, List[int]] = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])
    out, stack = [], list(kids.get(pid, []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(kids.get(p, []))
    return out


def _try_wait(pid: int) -> Optional[int]:
    try:
        wp, st = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return 0
    return st if wp == pid else None


def _terminate(pid: int, grace: float = 2.0) -> Optional[int]:
    """SIGTERM the whole process group/session, escalate to SIGKILL, reap. Returns root wait status."""
    members = _members(pid)

    def sig(s: int) -> None:
        for target in (-pid, *members):
            try:
                os.kill(target, s)
            except OSError:
                pass

    sig(signal.SIGTERM)
    status = None
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if status is None:
            status = _try_wait(pid)
        if status is not None and not any(_alive(m) for m in members):
            break
        time.sleep(0.05)
    if status is None or any(_alive(m) for m in members):
        sig(signal.SIGKILL)
        end = time.monotonic() + 3
        while time.monotonic() < end and status is None:
            status = _try_wait(pid)
            time.sleep(0.02)
    for m in members:  # reap adopted orphans (needs subreaper); ignore if not ours
        for _ in range(50):
            if _try_wait(m) is not None or not _alive(m):
                break
            time.sleep(0.02)
    return status


def _notes_for(snap: Snapshot) -> List[str]:
    caps = snap.get("capabilities") or {}
    notes = []
    if caps.get("collector") == "ps":
        notes.append("limited platform: no /proc, so no syscall or lock-holder data; using ps/lsof and the output text only")
    elif caps.get("collector") == "linux-proc" and not caps.get("syscall"):
        notes.append("this kernel/permission level hides /proc/PID/syscall: blocked-read evidence is unavailable")
    notes += snap.get("warnings") or []
    return notes


def run_command(cmd: Sequence[str], *, idle: float = 20.0, timeout: float = 0.0, stdin_mode: str = "pty",
                stop_on: Optional[str] = None, stream: Any = None,
                on_verdict: Optional[Callable[[Dict[str, Any], bool], None]] = None,
                env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None, include_snapshot: bool = False,
                sample: float = 0.5, forward_stdin: bool = True, tail_chars: int = 16384) -> RunResult:
    """Run *cmd* in a PTY with an idle watchdog.

    stop_on: None (only report), "stuck" (stop when the verdict says stuck), "any" (stop on any verdict).
    """
    if pty is None:
        raise OSError("this platform has no POSIX pty module")
    cmd = list(cmd)
    child_env = dict(os.environ if env is None else env)
    start = time.monotonic()
    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            if cwd:
                os.chdir(cwd)
            if stdin_mode == "null":
                fd = os.open(os.devnull, os.O_RDONLY)
                os.dup2(fd, 0)
                os.close(fd)
            elif stdin_mode == "closed":
                os.close(0)
            os.execvpe(cmd[0], cmd, child_env)
        except OSError as exc:
            try:
                os.write(2, f"whystuck: {cmd[0]}: {os.strerror(exc.errno or 0)}\n".encode())
            finally:
                os._exit(EXIT_NOT_EXECUTABLE if exc.errno == errno.EACCES else EXIT_NOT_FOUND)
        os._exit(EXIT_NOT_FOUND)

    _set_winsize(master)
    sigcount = [0]
    old_handlers: Dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        sigcount[0] += 1
        try:
            os.killpg(pid, signum if sigcount[0] == 1 else signal.SIGKILL)
        except OSError:
            pass

    def winch(_signum: int, _frame: Any) -> None:
        _set_winsize(master)
        try:
            os.killpg(pid, signal.SIGWINCH)
        except OSError:
            pass

    try:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            old_handlers[s] = signal.signal(s, forward)
        old_handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, winch)
    except ValueError:  # not in the main thread
        pass

    saved_tty = None
    use_stdin = forward_stdin and stdin_mode == "pty"
    try:
        if use_stdin and sys.stdin.isatty():
            saved_tty = termios.tcgetattr(0)
            tty.setcbreak(0)
    except Exception:
        saved_tty = None

    buf = bytearray()
    alt = False
    ends_nl = True
    last_act = time.monotonic()
    diagnosed = False
    stdin_open = use_stdin
    master_open = True
    verdicts: List[Dict[str, Any]] = []
    status = "exited"
    wait_status: Optional[int] = None

    def on_output(data: bytes) -> None:
        nonlocal alt, ends_nl, last_act, diagnosed
        last_act = time.monotonic()
        diagnosed = False
        alt = _alt_screen_state(alt, data)
        ends_nl = data.endswith(b"\n")
        buf.extend(data)
        if len(buf) > tail_chars:
            del buf[: len(buf) - tail_chars]
        if stream is not None:
            try:
                stream.write(data)
                stream.flush()
            except (OSError, ValueError):
                pass

    try:
        while True:
            now = time.monotonic()
            if timeout and now - start >= timeout:
                wait_status = _terminate(pid)
                status = "timeout"
                break
            rl = ([master] if master_open else []) + ([0] if stdin_open else [])
            try:
                ready, _, _ = select.select(rl, [], [], 0.2)
            except (InterruptedError, ValueError):
                ready = []
            for fd in ready:
                if fd == master:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b""
                    if data:
                        on_output(data)
                    else:
                        master_open = False
                else:
                    try:
                        data = os.read(0, 4096)
                    except OSError:
                        data = b""
                    if data:
                        try:
                            os.write(master, data)
                        except OSError:
                            pass
                        last_act = time.monotonic()
                        diagnosed = False
                    else:
                        stdin_open = False  # EOF is deliberately NOT forwarded (see README)
            wait_status = _try_wait(pid)
            if wait_status is not None:
                end = time.monotonic() + 1.0   # drain what is left in the pty
                while master_open and time.monotonic() < end:
                    r, _, _ = select.select([master], [], [], 0.1)
                    if not r:
                        break
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    on_output(data)
                break
            now = time.monotonic()
            if not diagnosed and idle > 0 and now - last_act >= idle:
                snap = collect_snapshot(pid, command=cmd, tail=bytes(buf), alt_screen=alt,
                                        idle_seconds=now - last_act, sample=sample, session=True)
                verdict = classify(snap).to_dict()
                rec = {"at_seconds": round(time.monotonic() - start, 2), "idle_seconds": round(now - last_act, 2), **verdict,
                       "notes": _notes_for(snap)}
                if include_snapshot:
                    rec["snapshot"] = snap
                verdicts.append(rec)
                diagnosed = True
                if on_verdict:
                    on_verdict(rec, ends_nl)
                if stop_on == "any" or (stop_on == "stuck" and verdict["stuck"]):
                    wait_status = _terminate(pid)
                    status = "stuck" if verdict["stuck"] else "stopped"
                    break
    finally:
        if saved_tty is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, saved_tty)
            except Exception:
                pass
        for s, h in old_handlers.items():
            try:
                signal.signal(s, h)
            except (ValueError, TypeError):
                pass
        try:
            os.close(master)
        except OSError:
            pass

    child_code: Optional[int] = None
    if wait_status is not None:
        try:
            child_code = os.waitstatus_to_exitcode(wait_status)
            if child_code < 0:
                child_code = 128 - child_code
        except Exception:
            child_code = None
    if status == "exited":
        code = child_code if child_code is not None else 1
    elif status == "timeout":
        code = EXIT_TIMEOUT
    else:
        code = EXIT_STUCK
    return RunResult(status, code, child_code, round(time.monotonic() - start, 2), pid, verdicts,
                     clean_text(bytes(buf))[-4000:])


# --------------------------------------------------------------------------- #
# Demo (offline, scripted stalls; inspection is real)
# --------------------------------------------------------------------------- #

_DEMO_STUB = r'''
import os, sys, subprocess, time, fcntl, socket, getpass
name = os.path.basename(sys.argv[0]); a = sys.argv[1:]
def say(s):
    sys.stdout.write(s); sys.stdout.flush()
def wait_tty():
    os.read(os.open("/dev/tty", os.O_RDONLY), 1)
if name == "git" and a[:1] == ["log"]:
    p = subprocess.Popen(["less"], stdin=subprocess.PIPE)
    p.stdin.write(b"commit 9f3c2ab41d7e (HEAD -> main)\nAuthor: mrzroot <mrzroot@users.noreply.github.com>\n\n    Add idle watchdog\n\ncommit 71be0c9a55f2\n\n    Initial commit\n")
    p.stdin.close(); p.wait()
elif name == "less":
    sys.stdout.write(sys.stdin.read()); say("(END)"); wait_tty()
elif name == "git" and a[:1] == ["commit"]:
    path = os.path.join(os.getcwd(), ".git", "COMMIT_EDITMSG")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("\n# Please enter the commit message for your changes.\n")
    say("hint: Waiting for your editor to close the file...\n"); subprocess.call(["vim", path])
elif name == "vim":
    f = open(a[0]); say("\x1b[?1049h\x1b[H\x1b[2J-- INSERT --"); wait_tty()
elif name == "git" and a[:1] == ["clone"]:
    say("Cloning into 'whystuck'...\n")
    s = socket.socket(); s.connect(("127.0.0.1", int(os.environ["WHYSTUCK_DEMO_PORT"])))
elif name == "apt-get" and a[:1] == ["install"]:
    say("Reading package lists... Done\nThe following NEW packages will be installed:\n  ripgrep\n0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\nDo you want to continue? [Y/n] ")
    sys.stdin.readline()
elif name == "apt-get" and a[:1] == ["update"]:
    path = os.environ["WHYSTUCK_DEMO_LOCK"]
    say("Reading package lists... Done\nWaiting for cache lock: Could not get lock " + path + "\n")
    f = open(path, "a"); fcntl.flock(f, fcntl.LOCK_EX)
elif name == "unattended-upgr":
    f = open(os.environ["WHYSTUCK_DEMO_LOCK"], "a"); fcntl.flock(f, fcntl.LOCK_EX); time.sleep(600)
elif name == "sudo":
    getpass.getpass("[sudo] password for demo: ")
elif name == "vitest":
    say("\n DEV  v2.1.0 /project\n\n OK src/add.test.ts (3 tests) 4ms\n\n Test Files  1 passed (1)\n\n PASS  Waiting for file changes...\n       press h to show help, press q to quit\n")
    os.read(0, 1)
else:
    say("demo stub: unknown invocation\n")
'''


class _TextOnly:
    """Demo display: show what a human reads (escape sequences stripped)."""

    def __init__(self, out: Any):
        self.out = out

    def write(self, data: bytes) -> None:
        text = clean_text(data)
        self.out.write(text)

    def flush(self) -> None:
        self.out.flush()


DEMO_SCENARIOS = (
    {"id": "pager", "expect": "pager", "cmd": ["git", "log"], "what": "output is handed to a pager (less) that waits for q"},
    {"id": "editor", "expect": "editor", "cmd": ["git", "commit"], "what": "git opens an editor and waits for it to close"},
    {"id": "confirm", "expect": "confirm", "cmd": ["apt-get", "install", "ripgrep"], "what": "an installer asks [Y/n]"},
    {"id": "secret", "expect": "secret", "cmd": ["sudo", "systemctl", "restart", "nginx"], "what": "sudo asks for a password on the terminal"},
    {"id": "watch", "expect": "watch", "cmd": ["vitest"], "what": "a test runner starts in watch mode and never exits"},
    {"id": "server", "expect": "server", "cmd": None, "what": "a real dev server (python -m http.server) that is fine, just long-running"},
    {"id": "lock", "expect": "lock", "cmd": ["apt-get", "update"], "what": "waits for a lock that another process holds", "needs_proc": True},
    {"id": "network", "expect": "network", "cmd": ["git", "clone", "https://example.invalid/whystuck.git"],
     "what": "a TCP connect is never answered (local listener with a full backlog)", "needs_proc": True},
)


def run_demo(only: Optional[Sequence[str]] = None, idle: float = 1.5, color: str = "auto", out: Any = None) -> int:
    out = out or sys.stdout
    style = make_style(color, out)
    ids = [s["id"] for s in DEMO_SCENARIOS]
    chosen = [s for s in DEMO_SCENARIOS if not only or s["id"] in only]
    unknown = [o for o in (only or []) if o not in ids]
    if unknown:
        print(f"whystuck: unknown demo scenario(s): {', '.join(unknown)}. Available: {', '.join(ids)}", file=sys.stderr)
        return EXIT_USAGE
    if pty is None:
        print("whystuck: --demo needs a POSIX pseudo-terminal (Linux/macOS). On Windows use WSL.", file=sys.stderr)
        return EXIT_USAGE
    linux = collector_name() == "linux-proc"
    out.write(style.c("1", f"whystuck {__version__} demo") + f" · Python {platform.python_version()} · "
              f"{platform.system()} {platform.machine()} · idle threshold {idle:g}s\n")
    out.write("Each scenario runs a small stand-in program (a script named like the real tool) under whystuck.\n"
              "The stand-ins only imitate the stall. The inspection that follows (process tree, sockets, locks) is real.\n"
              "Terminal escape sequences are stripped from the display below.\n")
    passed = ran = 0
    with tempfile.TemporaryDirectory(prefix="whystuck-demo-") as tmp:
        shebang = "#!" + sys.executable
        if re.search(r"\s", sys.executable) or len(shebang) > 100:
            shebang = "#!/usr/bin/env python3"
        bindir = os.path.join(tmp, "bin")
        work = os.path.join(tmp, "work")
        os.makedirs(bindir)
        os.makedirs(work)
        for name in ("git", "less", "vim", "apt-get", "sudo", "vitest", "unattended-upgr"):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(shebang + "\n" + _DEMO_STUB)
            os.chmod(p, 0o755)
        lockfile = os.path.join(tmp, "lock-frontend")
        open(lockfile, "w").close()
        for i, sc in enumerate(chosen, 1):
            cmd = sc["cmd"] or [sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"]
            shown = shlex.join(["python", "-m", "http.server", "0", "--bind", "127.0.0.1"]) if not sc["cmd"] else shlex.join(cmd)
            out.write("\n" + style.c("1;36", f"[{i}/{len(chosen)}] {sc['id']}") + f" — `{shown}`: {sc['what']}\n")
            if sc.get("needs_proc") and not linux:
                out.write(style.c("33", "  skipped: this scenario needs Linux /proc to observe the stall\n"))
                continue
            env = dict(os.environ)
            env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
            env["WHYSTUCK_DEMO_LOCK"] = lockfile
            cleanup: List[Callable[[], None]] = []
            holder = None
            if sc["id"] == "lock":
                holder = subprocess.Popen([os.path.join(bindir, "unattended-upgr")], env=env, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                cleanup.append(lambda h=holder: (h.kill(), h.wait()))
                if fcntl is not None:
                    fd = os.open(lockfile, os.O_RDWR)
                    cleanup.append(lambda fd=fd: os.close(fd))
                    for _ in range(100):  # wait until the holder really has the lock
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            fcntl.flock(fd, fcntl.LOCK_UN)
                            time.sleep(0.03)
                        except OSError:
                            break
            if sc["id"] == "network":
                srv = socket.socket()
                srv.bind(("127.0.0.1", 0))
                srv.listen(0)
                fillers = []
                for _ in range(4):
                    c = socket.socket()
                    c.setblocking(False)
                    c.connect_ex(srv.getsockname())
                    fillers.append(c)
                env["WHYSTUCK_DEMO_PORT"] = str(srv.getsockname()[1])
                cleanup.append(lambda: [x.close() for x in fillers + [srv]])
            ctx = Style(style.color, style.u)

            def show(rec: Dict[str, Any], ends_nl: bool, ctx: Style = ctx) -> None:
                if not ends_nl:
                    out.write("\n")
                out.write(render_text(rec, idle_seconds=rec["idle_seconds"], pid=None, command=None, style=ctx))

            res = None
            try:
                res = run_command(cmd, idle=idle, timeout=idle + 12, stop_on="any" if sc["id"] == "server" else "stuck",
                                  stream=_TextOnly(out), on_verdict=show, env=env, cwd=work, forward_stdin=False)
            finally:
                for fn in cleanup:
                    try:
                        fn()
                    except Exception:
                        pass
            ran += 1
            got = res.verdicts[0]["id"] if res and res.verdicts else "(no stall detected)"
            ok = got == sc["expect"]
            passed += ok
            mark = style.c("32", "OK") if ok else style.c("31", "MISMATCH")
            out.write(f"  expected `{sc['expect']}`, whystuck said `{got}` {mark}  (command stopped by whystuck: {res.status}, exit {res.exit_code})\n")
        out.write(f"\nSummary: {passed}/{ran} scenarios diagnosed as expected.\n")
    out.flush()
    return 0 if passed == ran else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

EPILOG = """\
exit codes:
  N      the command's own exit status (128+SIGNAL if it was killed by a signal)
  124    --timeout reached; the command was stopped
  125    a stall was diagnosed as 'stuck' and the command was stopped (--kill); with --explain: verdict is 'stuck'
  126/127 command not executable / not found
  2      usage error or unsupported platform

Without --kill, whystuck only *reports*: the command keeps running.
Examples:
  whystuck -- git log
  whystuck --idle 10 --kill --json -- npm test
  whystuck --explain 12345
  whystuck --demo
"""


def _print_rules(out: Any) -> None:
    out.write("whystuck rules, in evaluation order (first match wins; others are listed as 'also matched'):\n\n")
    for r in RULES:
        out.write(f"  {r.id:<11} {'stuck    ' if r.stuck else 'not stuck'}  {r.doc}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="whystuck", usage="whystuck [options] [--] COMMAND [ARGS...]",
                                description="Run a command with an idle watchdog; when it stalls, say why and how to fix it.",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--idle", type=float, default=None, metavar="SECONDS",
                   help="seconds without output before diagnosing (default 20; demo default 1.5)")
    p.add_argument("--timeout", type=float, default=600.0, metavar="SECONDS",
                   help="stop the command after this long overall; 0 disables (default 600)")
    p.add_argument("--kill", action="store_true", help="stop the command (whole process group) when the verdict is 'stuck'")
    p.add_argument("--json", action="store_true", help="print one JSON result on stdout; child output is captured, not streamed")
    p.add_argument("--stdin", choices=("pty", "null", "closed"), default="pty",
                   help="pty: child gets the terminal (default); null: stdin is /dev/null; closed: stdin is closed")
    p.add_argument("--explain", type=int, metavar="PID", help="diagnose an already running process (read-only) and exit")
    p.add_argument("--tail-file", metavar="FILE", help="with --explain: file holding the process's recent output text")
    p.add_argument("--demo", action="store_true", help="run bundled offline scenarios that stall on purpose")
    p.add_argument("--only", metavar="LIST", help="with --demo: comma-separated scenario ids")
    p.add_argument("--rules", action="store_true", help="list the detection rules and exit")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--include-snapshot", action="store_true", help="include the raw evidence snapshot in --json output")
    p.add_argument("--version", action="version", version=f"whystuck {__version__}")
    p.add_argument("command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return p


def _json_default(o: Any) -> Any:
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: List[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not cmd:
        cmd = [c for c in args.command if c != "--"]
    elif args.command:
        parser.error("unexpected arguments before '--': " + " ".join(args.command))
    err = sys.stderr

    if args.rules:
        _print_rules(sys.stdout)
        return 0
    if args.demo:
        return run_demo(only=[x for x in (args.only or "").split(",") if x] or None,
                        idle=args.idle if args.idle else 1.5, color=args.color)

    if args.explain is not None:
        pid = args.explain
        if not os.path.exists(f"/proc/{pid}") and collector_name() == "linux-proc" or pid <= 0:
            print(f"whystuck: no such process: {pid}", file=err)
            return 1
        tail = ""
        if args.tail_file:
            try:
                tail = open(args.tail_file, "rb").read().decode("utf-8", "replace")[-16384:]
            except OSError as exc:
                print(f"whystuck: cannot read --tail-file: {exc}", file=err)
                return EXIT_USAGE
        v = diagnose(pid, tail=tail, include_snapshot=args.include_snapshot)
        if args.json:
            print(json.dumps({"tool": "whystuck", "version": __version__, "pid": pid, "verdict": v}, indent=2,
                             ensure_ascii=False, default=_json_default))
        else:
            sys.stdout.write(render_text(v, idle_seconds=None, pid=pid, command=None, style=make_style(args.color, sys.stdout)))
            if not tail:
                sys.stdout.write("note: without the process's output text (--tail-file) prompts like [y/N] cannot be recognised.\n")
        return EXIT_STUCK if v["stuck"] else 0

    if not cmd:
        parser.print_usage(err)
        print("whystuck: no command given (try `whystuck --demo`)", file=err)
        return EXIT_USAGE
    if pty is None or os.name != "posix":
        print("whystuck: running commands needs a POSIX pseudo-terminal, which native Windows does not have.\n"
              "Use WSL, or import whystuck.classify()/diagnose() from Python. `--demo` and the runner are POSIX-only in v0.1.",
              file=err)
        return EXIT_USAGE
    idle = args.idle if args.idle is not None else 20.0
    if idle <= 0:
        parser.error("--idle must be > 0")

    _set_subreaper()
    style_err = make_style(args.color, err)

    def show(rec: Dict[str, Any], ends_nl: bool) -> None:
        if args.json:
            return
        if not ends_nl:
            err.write("\n")
        err.write(render_text(rec, idle_seconds=rec["idle_seconds"], pid=res_pid[0], command=cmd, style=style_err))
        for n in rec.get("notes") or []:
            err.write(f"note: {n}\n")
        err.flush()

    res_pid = [None]
    try:
        res = run_command(cmd, idle=idle, timeout=args.timeout, stdin_mode=args.stdin, stop_on="stuck" if args.kill else None,
                          stream=None if args.json else sys.stdout.buffer, on_verdict=show,
                          include_snapshot=args.include_snapshot)
    except OSError as exc:
        print(f"whystuck: cannot start: {exc}", file=err)
        return EXIT_USAGE
    if args.json:
        doc = {"tool": "whystuck", "version": __version__, "command": cmd, "pid": res.pid, "status": res.status,
               "exit_code": res.exit_code, "child_exit_code": res.child_exit_code, "duration_seconds": res.duration,
               "idle_threshold_seconds": idle, "stuck": any(v["stuck"] for v in res.verdicts), "verdicts": res.verdicts,
               "output_tail": res.output_tail, "platform": sys.platform}
        print(json.dumps(doc, indent=2, ensure_ascii=False, default=_json_default))
    elif res.status == "timeout":
        err.write(f"whystuck: --timeout {args.timeout:g}s reached; command stopped (exit {EXIT_TIMEOUT})\n")
    elif res.status == "stuck":
        err.write(f"whystuck: command stopped because it is stuck (exit {EXIT_STUCK})\n")
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
