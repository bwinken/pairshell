# pairshell

**Pair-program in a remote shell with your AI agent.** You and Claude Code
drive the *same* tmux session over SSH or Telnet, and you see every command
it runs.

```
you (VS Code terminal) ──attach──▶ ┌──────────────────────────────┐
                                   │  tmux session on the remote   │ ◀── the shared shell:
Claude Code ──pairshell exec──▶    │  (your login shell, tcsh/bash)│     same cwd, env, jobs
                                   └──────────────────────────────┘
```

* The agent's commands are typed into the pane you are looking at; its output
  is what you see.  You can type into the same shell, Ctrl-C its command, or
  finish something yourself.  The agent reads the screen to learn what you did.
* The shared state lives in tmux on the remote host, so it survives Windows
  reboots, `serve` restarts and detaching.
* Windows client, **no WSL required**: pure Python 3.11+ standard library.  SSH
  uses the OpenSSH client that ships with Windows (`ssh.exe`); Telnet uses a
  built-in client.  Linux/macOS clients work too.
* Airgap-friendly: no pip dependencies.

## 繁體中文簡介

pairshell 讓你和 Claude Code 這類 AI agent **共用同一個遠端 shell**：雙方都在同一個
tmux session 裡工作（相同的目錄、環境變數與背景工作）。Agent 下的每一個指令都會即時出現在
你的 VS Code 終端機裡；你隨時可以接手輸入、按 Ctrl-C 中斷，agent 也能讀取螢幕內容得知你做了什麼。

* 用戶端只需要 Windows 10/11 + Python 3.11 以上，**不需要 WSL**，也不需要安裝任何 pip 套件。
  SSH 走 Windows 內建的 `ssh.exe`，Telnet 使用內建的純 Python 用戶端。
* 遠端只需要 Linux + tmux（2.7 以上）+ bash，登入 shell 可以是 tcsh。
* 密碼存在 Windows 認證管理員，不會以明文寫進任何檔案。
* 指令：`pairshell` 開啟互動選單，`pairshell exec "cmd"` 讓 agent 執行指令，
  `pairshell screen` 讀螢幕，`pairshell keys C-c` 送按鍵。詳細說明見下文（英文）。

---

## How it works

```
remote host
 ├─ tmux session <name>  (your login shell)   ◀── you attach from a VS Code terminal
 │     ▲ tmux send-keys / capture-pane
 └─ control shell (bash --norc --noprofile, stty -echo)
       ▲ one persistent Telnet or SSH connection, held by `pairshell serve`
         local JSON-line RPC on 127.0.0.1:<port per profile>
            ▲ pairshell exec / screen / keys / status   ◀── Claude Code
```

`pairshell` is **not** a shell or a multiplexer.  It is a bridge that lets an
agent type into, and read from, a tmux pane a human is also attached to.  The
invisible control channel is used only to run `tmux` commands and diagnostics,
never to do user-visible work.

### What `exec` does

1. **Idle check.** `tmux display -p '#{history_size} #{cursor_y} #{pane_current_command}'`
   plus `capture-pane`.  The pane is idle only if the foreground process is a
   shell (`-tcsh` → `tcsh`) *and* the cursor line ends like a prompt
   (`[%$#>]\s*$`), and the pane is not in copy mode.  Otherwise `exec` refuses
   with exit code **3** and prints the screen tail.  `--force` overrides.
2. **Send.** The command is typed literally (`send-keys -l`, via a base64 round
   trip so no quoting can leak into either shell) followed by a sentinel:
   `; echo __DONE_"$?"_<nonce>__` (`"$status"` for csh/tcsh/fish; a bare space
   instead of `;` when the command ends with `&`), then Enter.
3. **Wait.** The visible pane is polled with exponential backoff (0.3 s → 2 s)
   until `__DONE_(\d+)_<nonce>__` appears.  The echoed command line shows the
   literal `"$?"`, so only real execution prints digits.
4. **Collect.** `capture-pane -J` from the line the prompt was on when the
   command was sent; the output is everything between the echoed command and
   the sentinel (text before the sentinel on the same line is kept, so
   `printf abc` gives `abc`).  Only the last `--max-lines` (500) lines are
   returned, with an "omitted" note.
5. **Timeout.** Exit code **124** with partial output.  The command keeps
   running in tmux: poll with `pairshell screen`, never resend.

Exit code **125** means the shell came back to a prompt without printing the
sentinel: a csh parse error rejected the whole line (`Ambiguous output
redirect`), the command was `exec`/a sub-shell, or the line was edited.

## Requirements

| Side | Needs |
| --- | --- |
| Client | Windows 10/11 (no WSL), Python 3.11+; also Linux/macOS.  For SSH profiles: the Windows *OpenSSH Client* optional feature (`ssh.exe`, on by default on recent Windows). |
| Remote | Linux with `tmux` ≥ 2.7, `bash`, coreutils (`base64`).  Login shell may be tcsh/csh, bash, zsh... |
| Network | Telnet (port 23) or SSH with key authentication. |

## Install

pairshell has no dependencies, so any of these works.  Pick by how the
machine is connected:

**Online, one line (recommended: pipx gives an isolated environment and puts
`pairshell` on PATH for every shell, including the one Claude Code uses):**

```bat
pipx install git+https://github.com/bwinken/pairshell
:: no pipx yet?  py -m pip install --user pipx && py -m pipx ensurepath   (then reopen the terminal)
:: no git on the machine?  pipx install https://github.com/bwinken/pairshell/archive/refs/heads/main.zip
```

**Airgapped, zero install:** download the repository zip on a connected
machine, unzip it anywhere on the workstation, and add its `bin` folder to
PATH.  `bin\pairshell.cmd` (cmd/PowerShell) and `bin/pairshell` (Git Bash,
Linux, macOS) run it straight from the folder; `python -m pairshell` from the
folder works too.  Updating is replacing the folder.

**Plain pip:** `pip install .` in a clone (or `pip install pairshell-main.zip`).
With `--user`, make sure Python's user `Scripts` directory is on PATH.

A virtual environment is not needed for isolation (nothing to conflict with),
and a venv that is not activated hides the `pairshell` command from other
terminals, so the agent cannot find it.  If you prefer venvs, use pipx, which
manages one for you and links the command into PATH.

Check with `pairshell --version` and `pairshell --help`.

## Quick start

```bat
pairshell add lab1        :: interactive: protocol telnet/ssh, host, port, user, password
pairshell                 :: menu: pick lab1, Enter -> you are in the remote tmux session
```

In another terminal (or from Claude Code):

```bat
pairshell exec "pwd" "ls"          :: both commands appear in your terminal
pairshell screen -n 100            :: what is on screen (+100 lines scrollback)
pairshell keys C-c                 :: interrupt whatever runs in the pane
pairshell status                   :: foreground process, idle?, attached clients
```

The menu's Enter (and `pairshell attach`) records the profile as **current**,
the default target of `exec`/`screen`/`keys`/`status` when `--to` is omitted.

## Commands

```
pairshell                       interactive menu (default)
pairshell attach <profile>      start serve if needed, attach this terminal to the tmux session
pairshell serve <profile>       hold the control channel (normally started in background for you)
pairshell stop <profile>        stop serve (does NOT kill the remote tmux session)
pairshell exec [--to P] "cmd" ["cmd2" ...] [--timeout 120] [--force] [--max-lines 500] [--json]
pairshell screen [--to P] [-n N]          visible pane (+N lines scrollback)
pairshell keys [--to P] C-c | q Enter | --literal ":wq" Enter   (then prints the screen)
pairshell status [--to P] [--json]        foreground proc, idle, attached clients, serve alive
pairshell list [--json]                   profiles + running state
pairshell add | edit <P> | rm <P>         profile management (also in the menu)
pairshell current [P] [--clear]           show or set the default target
pairshell ctl [--to P] "bash cmd"         raw control-channel command, diagnostics only
```

* `exec` takes one command line per argument (newlines are rejected).  With
  several arguments it prints `### cmd` / `### rc=N` separators, keeps going
  after ordinary failures, and stops at the first busy (3), timeout (124) or
  no-sentinel (125) result.  The exit code is that stop code, else the last
  non-zero `rc`, else 0.
* `keys` accepts tmux key names matching `^[A-Za-z0-9_^\-]+$` (`C-c`, `Enter`,
  `Up`, `q`, `F5`, `M-x`); anything else goes through `--literal TEXT`.
* If serve is not running, `exec`/`screen`/`keys`/`ctl` start it in the
  background (hidden window on Windows) and say so on stderr.  `status` and
  `list` never start anything.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 / N | the remote command's exit code |
| 2 | pairshell error (connection, unknown profile, usage) |
| 3 | pane busy: user typing or a program in the foreground; nothing was sent |
| 124 | still running after `--timeout`; poll with `screen`, do not resend |
| 125 | the shell is back at a prompt but the sentinel never printed |

## Profiles, credentials, state

Stored in `%APPDATA%\pairshell` (Windows), `$XDG_CONFIG_HOME/pairshell` or
`~/.config/pairshell` (POSIX), or `$PAIRSHELL_HOME` if set:

```
profiles.json      name, protocol (telnet|ssh), host, port, user, key_path (ssh),
                   session (tmux name, default = profile name), rpc_port (auto, unique), last_used
current            profile last connected from the menu / attach
run/<P>.json       pid, rpc_port, token, started_at of a live serve (stale or reused pids are detected)
run/<P>.log        serve log (rotated); run/<P>.stderr.log holds crash output of a background serve
```

Passwords are **never stored in plaintext**.  On Windows they live in the
Credential Manager (target `pairshell:<profile>`, written through `ctypes`
`CredWriteW`/`CredReadW`/`CredDeleteW`, stdlib only).  macOS uses the
keychain (`security`), Linux uses `secret-tool` when present.  Without a
store, `pairshell serve <P>` prompts in the terminal, or reads
`PAIRSHELL_PASSWORD` from its environment.  pairshell never prints a
credential value.

Several profiles can be live at once, each with its own serve process and RPC
port; `--to <profile>` picks one, `pairshell current <profile>` changes the
default, and the menu shows the state of all of them (stopped /
connected-idle / busy).

## Attaching

* **ssh:** runs `ssh -t [-p port] [-i key] user@host "tmux new -A -s <session>"`
  (with `env TERM=xterm-256color` when your terminal exports no `TERM`).
* **telnet:** built-in client.  Logs in with the stored credentials, answers
  the terminal-type and window-size negotiations, runs
  `exec env TERM=xterm-256color tmux new -A -s <session>` (shell-agnostic, so
  it does not matter whether your login shell is tcsh or bash), then relays
  raw bytes.  On Windows it enables `ENABLE_VIRTUAL_TERMINAL_INPUT` /
  `ENABLE_VIRTUAL_TERMINAL_PROCESSING`, reads raw console input (not scan
  codes) and forwards terminal resizes (NAWS).  **Ctrl-]** disconnects; the
  tmux session keeps running.
* Manual fallback for telnet: `plink -telnet example-host` (PuTTY) then
  `tmux new -A -s <session>`.  Do not use Windows' `telnet.exe`: it breaks
  tmux rendering.
* If you ever land on a plain login-shell prompt instead of tmux (a very slow
  login shell can discard the typed `tmux` command), just type
  `tmux new -A -s <session>` yourself; nothing else is different.

Detaching with tmux's own `prefix d` also ends the attach (the login shell
was replaced by tmux via `exec`).

## VS Code integration

**Phase 1 (settings only).** Register pairshell as a terminal profile and
open terminals in the editor area, so the shared shell sits next to your files:

```json
"terminal.integrated.profiles.windows": {
  "pairshell": { "path": "pairshell" }
},
"terminal.integrated.defaultLocation": "editor"
```

Then *Terminal: Create New Terminal (With Profile)* → `pairshell` opens the
menu; Enter attaches.  If `pairshell` is not on `PATH`, use
`{"path": "python", "args": ["-m", "pairshell"]}`.

**Phase 2 (extension).** `vscode/` contains a small TypeScript extension:
a sidebar tree of profiles with live state, click-to-attach in an editor
terminal, a status bar item showing the agent's current target and
busy/idle (click to switch), and add/edit/remove through input boxes.  It
is a thin UI that shells out to `pairshell list --json`, `status --json`,
`attach`, `stop`, `current`; all logic stays in Python.  See
[vscode/README.md](vscode/README.md) for building the `.vsix` for offline
installation.

## How the agent drives the session

Claude Code never logs in to the remote host itself and never attaches to
tmux.  It runs on your workstation and only executes the local `pairshell`
command from its shell tool, exactly like any other CLI:

```
 you: "build it on lab1"
  |
  v
Claude Code (workstation) ── runs ──▶ pairshell exec "cd ~/proj && make" --timeout 600
                                          |  JSON-line RPC, 127.0.0.1:<port>
                                          v
                                      pairshell serve lab1  (background, holds the one telnet/ssh login)
                                          |  hidden control shell: tmux send-keys / capture-pane
                                          v
                                      tmux session "lab1" on the remote  ◀── your VS Code terminal is attached here
```

1. `serve` checks the pane is idle (a shell at a prompt, nobody typing).  If
   you are in the middle of something, `exec` returns rc 3 and Claude waits.
2. It types `cd ~/proj && make ; echo __DONE_"$?"_<nonce>__` into the pane,
   so the command scrolls by in your terminal as if Claude sat next to you.
3. It watches the pane until the sentinel appears, cuts the output out of
   the scrollback and prints it to Claude with the real exit code.  Long
   builds return 124 and Claude keeps polling `pairshell screen`.
4. Anything you do in the same pane (Ctrl-C, fixing a file, running a test)
   is visible to Claude through `pairshell screen -n 200`, and Claude's
   `cd`/`export` stay in effect for you, because it is one shell.

Claude picks pairshell up from the instructions in your project: reference
`AGENTS.md` from the project's `CLAUDE.md` (or paste its rules) and name the
profile, for example "remote work goes through `pairshell exec --to lab1`".
Which profile is the default (`current`) is whatever you last attached to.

## Agent instructions

The easiest way to teach Claude Code is the bundled **skill**:

```bat
pairshell install-skill              :: -> %USERPROFILE%\.claude\skills\pairshell\SKILL.md (all projects)
pairshell install-skill --project    :: -> .\.claude\skills\pairshell\SKILL.md (this project only)
```

Restart Claude Code afterwards.  From then on, asking it to "run the tests
on lab1" or "check what I ran on the server" makes it pick up the skill and
work through `pairshell exec`/`screen`/`keys` with the right etiquette (rc 3
and rc 124 handling, tcsh syntax, no `--force` over you).  The same rules
are in [AGENTS.md](AGENTS.md) for other agents; `CLAUDE.md` points at it.

## Remote notes and gotchas

* **Session bootstrap** happens on every client call and is idempotent:
  `tmux has-session -t =S: || tmux start-server \; set-option -g history-limit 50000 \; new-session -d -s S -x 200 -y 50 \; send-keys -t =S: 'unset autologout' Enter`,
  then, when `#{pane_pipe}` is not yet set, `tmux pipe-pane -t =S: 'cat >> ~/.pairshell/S.log'`
  keeps a transcript of the pane on the remote (`pipe-pane -o` is *not* used:
  it toggles).  tmux panes are login shells, and tcsh's `autologout` would
  otherwise kill the shared shell after an idle hour.
* `history-limit` applies only to panes created after it is set.  For sessions
  you create yourself put `set-option -g history-limit 50000` in
  `~/.tmux.conf`.
* The agent types into the session's **current window / active pane**, i.e.
  the one you are looking at.  If you open another window in the same session,
  the agent follows you (and `status` shows the window index).
* **tcsh:** `2>/dev/null` fails (`Ambiguous output redirect`; use
  `>& /dev/null`), and `!` history-expands even inside single quotes
  (escape as `\!`).  The sentinel uses `$status` for csh-family shells
  automatically.
* The control shell sets `LANG=en_US.UTF-8` (falling back to `C.utf8`) and
  `TERM=dumb`; the tmux pane's own environment is untouched.
* The RPC endpoint listens on 127.0.0.1 only and requires a per-process token
  stored in `run/<P>.json`.

### SSH: keys, not passwords

`ssh.exe` has no non-interactive way to accept a password (there is no
`sshpass` on Windows, and `BatchMode=yes` deliberately disables the prompt),
so password authentication over SSH is not supported in v1.  Use a key:

```bat
ssh-keygen -t ed25519
type %USERPROFILE%\.ssh\id_ed25519.pub | ssh alice@example-host "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
pairshell add lab2 --protocol ssh --host example-host --user alice --key %USERPROFILE%\.ssh\id_ed25519
```

Extra ssh arguments (jump hosts, ciphers) can be stored per profile with
`--ssh-option=-oProxyJump=bastion` (repeatable).  A passphrase-protected key
works when it is loaded into the Windows *OpenSSH Authentication Agent*
service (`ssh-add`); `serve` runs `ssh.exe` with `BatchMode=yes`, so it
cannot prompt for the passphrase itself.

## Troubleshooting

* `pairshell serve <P>` in a terminal shows the login conversation and errors
  live; the background log is `run/<P>.log` under the config directory.
* `pairshell status` tells you whether serve is up, the transport connected,
  what runs in the foreground and why the pane counts as busy.
* `pairshell ctl "tmux ls"` runs a raw command in the control shell.
* No prompt detected although the shell is idle?  The idle check needs the
  cursor line to end with `%`, `$`, `#`, `>` or one of the common theme
  glyphs (`❯`, `➜`, `λ`, `»`, `→`).  Adjust your prompt or use `--force`.
* Telnet login hangs: the remote must show `login:` and `Password:` prompts.
  Auth failure is detected with `login incorrect|authentication failure|access denied|login failed`
  only, because MOTDs routinely contain words like "error".

## Development

```
python -m unittest discover -s tests -v      # or: pytest
```

The unit tests cover sentinel parsing, capture-window math, idle detection,
key validation, the profile store, RPC and the control protocol (with a
scripted remote).  Integration tests run when `tmux` and `bash` are
available (Linux/macOS): a real tmux session through the `local` protocol,
a fake telnetd (real pty, tcsh login shell if installed) for the Telnet
transport and for the built-in attach client driven through a pty
(auto-login, resize negotiation, typing, Ctrl-] detach), and a throwaway
`sshd` for the SSH transport.  The Windows Credential Manager round trip
runs only on Windows; the Windows console code paths of `attach` and the
menu are not covered by automated tests.

The `local` protocol (`pairshell add dev --protocol local`) uses a local
bash as the "remote"; it exists for development and tests on Linux/macOS.

Layout:

```
pairshell/  cli.py menu.py profiles.py credentials.py rpc.py serve.py tmuxops.py attach.py dialogs.py
            transports/{base.py, telnet.py, ssh.py, local.py, _telnetlib.py}
vscode/     VS Code extension (phase 2)
tests/      unit + integration tests, fake telnetd
```

## License

MIT.  `pairshell/transports/_telnetlib.py` is a trimmed copy of CPython's
`telnetlib` (removed from the standard library in Python 3.13) and keeps its
PSF license notice.
