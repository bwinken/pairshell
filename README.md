# pairshell

Pair-program in a remote shell with your AI agent: you and Claude Code drive
the **same** tmux session over SSH or Telnet, and you see every command it runs.

```
you (VS Code terminal) ──attach──▶ ┌────────────────────────────────┐
                                   │ tmux session on the remote host │ ◀── one shell: same cwd,
Claude Code ── pairshell exec ──▶  │ (your login shell, tcsh or bash)│     env and jobs for both
                                   └────────────────────────────────┘
```

## 繁體中文簡介

pairshell 讓你和 Claude Code 共用同一個遠端 shell（同一個 tmux session）。Agent 下的每個指令都即時出現在你的
VS Code 終端機，你隨時可以接手、按 Ctrl-C，agent 也能讀螢幕知道你做了什麼。用戶端只要 Windows + Python 3.11，
**不需要 WSL**、沒有任何 pip 相依；遠端只要 Linux + tmux + bash。安裝、功能與限制見下文。

## Install

| Situation | Command |
| --- | --- |
| Online, one line | `pip install git+https://github.com/bwinken/pairshell` |
| Online, no git installed | `pip install https://github.com/bwinken/pairshell/archive/refs/heads/main.zip` |
| **No PyPI** (airgapped, or a proxy that breaks pip's TLS) | download the repository zip, unzip, then `python tools\build_wheel.py` and `pip install dist\pairshell-0.1.0-py3-none-any.whl` (no network, no setuptools) |
| Zero install | unzip anywhere and add its `bin` folder to PATH (`bin\pairshell.cmd` for cmd/PowerShell, `bin/pairshell` for Git Bash/Linux/macOS) |
| Prefer an isolated tool install | `pipx install git+https://github.com/bwinken/pairshell` (pipx also puts the command on PATH for every shell) |

Then `pairshell --version`.  If the command is not found after a pip
install, Python's `Scripts` directory is not on PATH (typical with
`pip install --user`): add it, use pipx, or call `python -m pairshell`
instead; the VS Code extension has a `pairshell.path` setting for that.

`pip install` of a *source* tree downloads setuptools from PyPI first
(`CERTIFICATE_VERIFY_FAILED ... self signed certificate in certificate
chain` behind corporate TLS inspection).  The wheel route above avoids PyPI
entirely.  To fix pip itself: `pip config set global.cert <corporate-root.pem>`,
or `pip --use-feature=truststore ...` (pip 22.2+, uses the Windows
certificate store), or, accepting unverified TLS to those two hosts,
`pip --trusted-host pypi.org --trusted-host files.pythonhosted.org ...`.

Requirements: Windows 10/11 (also Linux/macOS), Python 3.11+; for SSH the
Windows *OpenSSH Client* feature (`ssh.exe`).  Remote: Linux with `tmux` ≥ 2.7,
`bash`, coreutils.  No virtual environment needed: there are no dependencies
to isolate, and an unactivated venv would hide the command from the shell
Claude Code uses.

## Quick start

```bat
pairshell add lab1                :: protocol, host, port, user, password (stored in Windows Credential Manager)
pairshell                         :: menu: Enter attaches this terminal to the shared tmux session
pairshell install-skill           :: in your project: teaches Claude Code the workflow (.claude/skills/pairshell)
```

Claude then works through:

```
pairshell exec "cd ~/proj && make" --timeout 600     output + exit code, typed live into your pane
pairshell screen -n 100                              what is on screen (+scrollback)
pairshell keys C-c                                   interrupt; also q, Enter, --literal ":wq"
pairshell status                                     idle? shell family? serve alive?
```

`pairshell --help` and `pairshell <command> --help` list everything.

Claude Code asks before every shell command, `pairshell` included.  This
has nothing to do with the remote login (credentials are entered once, at
`pairshell add`); it is Claude Code's own confirmation prompt.  Two levels
for the project's `.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(pairshell status:*)", "Bash(pairshell screen:*)", "Bash(pairshell list:*)"] } }
```

lets Claude look without asking while `exec`/`keys` still prompt you with
the exact command; adding `"Bash(pairshell exec:*)"` and `"Bash(pairshell keys:*)"`
removes those prompts too, so Claude drives freely and your only check is
watching the pane (and Ctrl-C).

## Features

- **One shared shell.** The agent types into the pane you are attached to and
  reads the same scrollback; your `cd`, its `export`, background jobs: shared.
  The tmux session lives on the remote and survives reboots, detaching and
  serve restarts.
- **Safe typing.** `exec` sends nothing unless a shell prompt is idle (rc 3
  otherwise); `--force` only on request.  Commands end with a sentinel so exit
  codes are exact; long commands return 124 and keep running; a rejected line
  (tcsh syntax error, sub-shell) returns 125 instead of hanging.
- **Transports.** Telnet with a built-in client (works on Python 3.13, where
  the stdlib module is gone) and SSH via `ssh.exe` with keys.  One persistent
  login per profile, kept alive and re-established automatically.
- **Profiles and credentials.** `%APPDATA%\pairshell\profiles.json`,
  passwords in the Windows Credential Manager (keychain / secret-tool
  elsewhere), several profiles live at once, `--to <profile>` or the
  `current` one.
- **Attach.** `ssh -t` or the built-in telnet client with VT console mode and
  resize forwarding; Ctrl-] disconnects (`--detach-key C-q` or
  `PAIRSHELL_DETACH_KEY` to change it), tmux keeps running.  Interactive
  menu with live state (stopped / idle / busy).
- **Tooling.** `--json` on `exec`/`screen`/`status`/`list`, a remote
  transcript in `~/.pairshell/<session>.log` (capped per profile with
  `--transcript-mb`, default 50, `0` = off), a VS Code terminal profile
  snippet and a sidebar extension (`vscode/`), and a Claude Code skill
  (`pairshell install-skill`, `--user` for all projects).
- Stdlib only, airgap-friendly, no WSL.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 / N | the remote command's exit code |
| 2 | pairshell error (connection, profile, usage) |
| 3 | pane busy: user typing or a program in the foreground; nothing sent |
| 124 | still running after `--timeout`; poll with `screen`, do not resend |
| 125 | shell back at a prompt but the sentinel never printed |

## Limitations

- **SSH is key-auth only.** `ssh.exe` cannot take a password non-interactively;
  a passphrase-protected key needs the Windows OpenSSH Authentication Agent.
- **Telnet is plaintext.** Credentials and traffic are unencrypted; use it
  only on the trusted networks it was designed for.
- **One pane.** The agent works in the session's active pane (the window you
  are looking at); no multi-pane or multi-window targeting.
- **Heuristic idle detection.** The prompt must end with `% $ # >` or a common
  theme glyph (`❯ ➜ λ » →`); right-hand prompts (zsh `RPROMPT`) need a
  per-profile `--prompt-regex`; a continuation prompt counts as idle;
  programs printing prompt-like text can fool it.  `--force` exists too.
- **Exit-code overlap.** 3/124/125 share the space with remote exit codes;
  `--json` carries a separate `status` field.
- **Output is what tmux rendered.** Progress bars collapse to their final
  state, at most `--max-lines` (500) lines per `exec` (only the tail is
  fetched, the "omitted" count is then approximate); redirect big output to
  a file.  One line per command, no TAB characters, no stdin piping, no file
  transfer.
- **Logs keep secrets.** Commands are written to the local serve log, the
  remote transcript (`~/.pairshell`, mode 700, one rotated generation) and
  the remote shell history; keep passwords out of command lines, or set
  `--transcript-mb 0`.
- **Remote must be Linux** with tmux ≥ 2.7, bash and `base64`; `history-limit`
  applies only to panes created after it is set.
- **Windows-specific code** (VT console input, Credential Manager) is covered
  by code review, not by the automated tests, which run on Linux/macOS.
- **Not a security boundary.** Anything running as your Windows user can drive
  the session through the loopback RPC (token in your profile directory).
- The VS Code extension is installed from a bundled package (`pairshell
  install-vscode`), not from the marketplace; it was exercised under a stub
  of the VS Code API, not inside a running VS Code.

### Files

| What | Where |
| --- | --- |
| profiles, `current` | `%APPDATA%\pairshell` (Windows), `~/.config/pairshell` (POSIX), or `$PAIRSHELL_HOME` |
| run state, serve logs | `%LOCALAPPDATA%\pairshell\run`, `~/.local/state/pairshell/run`, or `$PAIRSHELL_HOME/run` |
| passwords | Windows Credential Manager (`pairshell:<profile>`), macOS keychain, `secret-tool` |
| remote transcript | `~/.pairshell/<session>.log` on the remote host |

## How it works

`serve` (one background process per profile) holds a Telnet/SSH login whose
shell is a hidden `bash --norc --noprofile` used only to run `tmux` commands
against the shared session.  `exec` checks `#{pane_current_command}` and the
cursor line, types `cmd ; echo __DONE_"$?"_<nonce>__` (`$status` for csh) with
`send-keys -l` via base64, polls `capture-pane` until the sentinel appears,
then captures from the line the prompt was on and returns what lies between
the echoed command and the sentinel.  The session is bootstrapped
idempotently on every call (`history-limit 50000`, `unset autologout`,
`pipe-pane` transcript).  The CLI talks to serve over JSON lines on
`127.0.0.1:<port>` with a per-process token.

## Remote notes

- **tcsh:** `2>/dev/null` is invalid (use `>& /dev/null`), `!` expands even in
  single quotes (`\!`), exit status is `$status`; the sentinel adapts.
- The agent's rules are in [AGENTS.md](AGENTS.md); the bundled skill carries
  the same content for Claude Code.
- Telnet fallback without pairshell: `plink -telnet example-host` then
  `tmux new -A -s <session>` (not Windows `telnet.exe`, it breaks tmux).
- If a slow login shell drops the typed `tmux` command on attach, type
  `tmux new -A -s <session>` yourself.

## VS Code

```bat
pairshell install-vscode
```

does both layers in one go, without node or the marketplace: it builds the
extension package from the copy bundled in pairshell and installs it through
VS Code's `code` command (or leaves a `.vsix` next to you for *Extensions:
Install from VSIX...* when `code` is not on PATH), and it merges these
settings into your user `settings.json`, keeping your comments and other
keys (a `.pairshell.bak` backup is written first):

```json
"terminal.integrated.profiles.windows": { "pairshell": { "path": "pairshell" } },
"terminal.integrated.defaultLocation": "editor"
```

A new terminal with that profile opens the menu in the editor area.  The
extension adds a profile tree, click-to-attach, and a status bar item for
the agent's current target; see [vscode/](vscode/README.md).  Flags:
`--no-extension`, `--no-settings`, `--no-default-location`, `--vsix-only`,
`--insiders`.  Reload the window afterwards.

## Troubleshooting

- After upgrading pairshell run `pairshell stop --all`; running serves keep
  the old code until restarted (the next command starts them again).
- `pairshell serve <P>` in a terminal shows the login conversation live;
  background logs are `run/<P>.log` and `run/<P>.stderr.log` (see Files).
- `pairshell status` explains why a pane counts as busy; `pairshell ctl "tmux ls"`
  runs a raw command in the control shell (diagnostics only).
- Login failures are detected only with `login incorrect|authentication
  failure|access denied|login failed`, because MOTDs contain words like "error".

## Development

`python -m unittest discover -s tests -v` (unit tests everywhere; integration
tests against real tmux, a fake telnetd, a throwaway sshd and a pty-driven
attach run on Linux/macOS).  The `local` protocol uses a local bash as the
"remote" for development.

## License

MIT.  `pairshell/transports/_telnetlib.py` is a trimmed copy of CPython's
`telnetlib` and keeps its PSF license notice.
