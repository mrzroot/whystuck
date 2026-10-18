# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-18

First release.

### Added
- `whystuck [--idle N] [--timeout N] [--kill] [--json] [--stdin pty|null|closed] -- COMMAND`: runs a command on a PTY with an idle watchdog.
- Ordered rule table with evidence, confidence and non-interactive fixes: `editor`, `pager`, `secret`, `confirm`, `watch`, `follow`, `lock`,
  `network` (TCP `SYN_SENT`), `server`, `stdin_wait`, `sleeping`, `busy`, and an honest `unknown` fallback.
- Linux collector based on `/proc` (stat, cmdline, wchan, syscall, fd links, `/proc/net/tcp{,6}`, lock holders, CPU sample).
- Best-effort `ps`/`lsof` collector for macOS/BSD (not tested on real macOS in this release).
- Exit codes: passthrough, `124` timeout, `125` stuck and stopped, `126`/`127` exec errors.
- `--json` output, `--include-snapshot`, `--explain PID` (read-only), `--tail-file`, `--rules`, `--color`.
- Importable API: `diagnose(pid, tail=...)`, `classify(snapshot)`, `collect_snapshot(pid)`, `run_command(...)`.
- `--demo`: eight offline scenarios (pager, editor, y/n prompt, password prompt, watch mode, long-running server, lock, stalled connect).
- Process-group/session kill with SIGTERM then SIGKILL; Linux subreaper so no zombies are left behind.
- Tests: pure rule tests, mutation-style rule coverage, snapshots recorded from real processes, real-PTY integration tests, golden output.
- English and Persian READMEs, CI for Python 3.9 to 3.13, an `AGENTS.md` snippet.

### Known limits
See the "Limits" section of the README (silence-only detection, last-line prompt matching, SYN_SENT-only network detection, no Windows).
