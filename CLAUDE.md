# CLAUDE.md

This repository is **pairshell**: a bridge that lets an AI agent and a human
drive the same remote tmux session.  See `README.md` for the design and
`AGENTS.md` for the rules an agent must follow when it operates a remote
shell through pairshell.  When you are *using* pairshell to work on a remote
host, follow `AGENTS.md` to the letter (only `exec`/`keys`, respect rc 3 and
rc 124, mind tcsh syntax).

## Developing pairshell itself

* Python 3.11+, **standard library only** (the client runs on airgapped
  Windows machines without WSL).  No new dependencies.
* Keep everything above `pairshell/transports/` transport-agnostic.
* Never put internal hostnames, IPs, usernames or paths in code, docs, tests
  or commits; examples use `example-host` / `192.0.2.10` / `alice`.
* Never print or log credential values.
* Run `python -m unittest discover -s tests -v` before committing.  Integration
  tests need `tmux` and `bash` (and use `tcsh`/`sshd` when present); they are
  skipped elsewhere.
* The VS Code extension in `vscode/` must stay a thin UI over the CLI's
  `--json` output.
