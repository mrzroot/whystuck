"""Builders for hand-written snapshots (the pure-function tests)."""


def proc(comm, argv=None, pid=100, state="S", tty_read=False, inferred=False, open_paths=None, syscall=None,
         root=False, target="/dev/pts/3"):
    return {
        "pid": pid, "ppid": 1, "comm": comm, "argv": argv if argv is not None else [comm], "state": state,
        "wchan": "", "syscall": syscall, "stdin": target, "stdin_tty": True, "cpu_ticks": 0,
        "blocking_read": ({"fd": 0, "target": target, "tty": True, "inferred": inferred} if tty_read else None),
        "open_paths": open_paths or [], "is_root": root,
    }


def snap(tail="", procs=None, command=None, alt=False, sockets=None, cpu=None, holders=None, proxy=None):
    return {
        "version": 1, "platform": "linux", "root_pid": 100, "idle_seconds": 20.0, "command": command, "tail": tail,
        "alt_screen": alt, "processes": procs if procs is not None else [proc("python3", root=True)],
        "sockets": sockets or [], "cpu_fraction": cpu, "proxy_vars": proxy or [],
        "capabilities": {"collector": "linux-proc", "syscall": True, "sockets": True},
        "lock_holders": holders or {},
    }
