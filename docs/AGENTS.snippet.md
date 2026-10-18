<!-- Paste this into AGENTS.md / CLAUDE.md / .cursorrules / your agent's rules file. Requires `whystuck` on PATH. -->

## Running shell commands that might wait for input

Never run a command that can wait for a human (pagers, editors, y/N prompts, password prompts, watch mode,
foreground servers) without a watchdog. Wrap risky or unfamiliar commands:

    whystuck --json --kill --idle 20 -- <command>

- Exit code `125` means the command was **stuck** and was stopped. Read `verdicts[0].id`, `.evidence` and `.fixes`
  in the JSON on stdout, then re-run the command using the suggested non-interactive form
  (for example `git --no-pager log`, `GIT_EDITOR=true git commit`, `apt-get install -y`, `vitest run`, `CI=true`).
- Exit code `124` means `--timeout` (default 600 s) was reached.
- Any other exit code is the command's own exit status.
- A verdict with `"stuck": false` (`server`, `sleeping`, `busy`) is **not a hang**: do not kill it. For a server, start it
  detached (`nohup <cmd> > server.log 2>&1 &`) and poll its port instead of waiting for it to exit.
- Never type or invent passwords, passphrases or tokens to get past a `secret` verdict; stop and ask the user.
- If a command is already hung, run `whystuck --explain <pid> --json`.

Defaults that avoid most hangs: `export GIT_PAGER=cat PAGER=cat GIT_EDITOR=true GIT_TERMINAL_PROMPT=0 CI=true`.
