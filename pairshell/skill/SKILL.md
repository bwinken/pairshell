---
name: pairshell
description: Operate a remote Linux shell that the user shares with you through pairshell (one tmux session over telnet or ssh; the user watches the same pane live). Use this skill whenever the user asks you to run, build, test, install, debug, inspect or check anything on a remote host, server, workstation, board, cluster node or "the machine", mentions pairshell, a profile name ("on lab1"), tmux, or asks you to look at what they ran or what is on their screen - even when they never say the word pairshell. Also use it when a task needs files, tools or hardware that only exist on the remote side.
---

# pairshell: working in a shell you share with the user

You and the user drive the **same** remote shell: one tmux pane on the remote
host.  The user is attached to it in their editor's terminal, so every
command you type scrolls past their eyes, and everything they type or
interrupt is visible to you.  Treat it like pair programming at one keyboard:
announce what you are about to run when it matters, never grab the keyboard
while the other person is typing, and read the screen before assuming state.

You never log in yourself (no `ssh`, `scp`, `telnet`, `plink`).  The only
door is the local `pairshell` command; a background `pairshell serve` keeps
the connection and starts automatically when needed.

## The three commands you need

```
pairshell status [--to P]                 is the pane idle? which shell family? serve alive?
pairshell exec [--to P] "cmd" ["cmd2" ...] [--timeout 120] [--max-lines 500]
pairshell screen [--to P] [-n N]          what is on screen (+N lines of scrollback)
pairshell keys [--to P] C-c | q Enter | --literal ":wq" Enter
```

`--to <profile>` targets a specific host; without it the *current* profile is
used (the one the user last attached to).  `pairshell list` shows profiles
and which one is current.  Every command has `--help`.

## Exit codes decide your next move

| rc | Meaning | What to do |
| --- | --- | --- |
| 0 / N | the remote command finished with exit code N | read the output, continue |
| 3 | pane busy: the user is typing, or a program is in the foreground; **nothing was sent** | `pairshell screen`, then wait or ask the user; never `--force` over them |
| 124 | your command is still running after `--timeout` | poll with `pairshell screen` until the prompt is back; **never resend**; `keys C-c` if it should stop |
| 125 | the shell is back at a prompt but never printed the completion marker | usually a tcsh syntax error rejected the whole line, or you started a sub-shell; read the screen tail on stderr, fix the command |
| 2 | pairshell itself failed (serve could not start, no such profile) | report the message verbatim to the user; do not work around it |

rc 3 and rc 124 are the normal rhythm of shared work, not errors.  A build
that returns 124 is simply still building.

## Working loop

1. `pairshell status` once at the start: confirms the pane is idle and tells
   you the shell family (`sh` for bash/zsh, `csh` for tcsh).
2. `pairshell exec "cd ~/proj && git status --short"`.  Shell state persists
   (cwd, environment, background jobs): `cd` once, then work.  Several
   arguments run in order with `### cmd` / `### rc=N` separators; the batch
   stops at the first 3/124/125.
3. Long-running work: give a generous `--timeout` and redirect output to a
   file, then read the file selectively.  `exec` returns at most
   `--max-lines` (500) lines, and huge output in the shared pane is noise for
   the user too.

   ```
   pairshell exec "make -j8 > /tmp/build.log 2>&1" --timeout 900
   pairshell exec "tail -n 40 /tmp/build.log" "grep -n 'error' /tmp/build.log | head"
   ```

4. Interactive or full-screen programs (vim, less, top, `y/n` and password
   prompts): `exec` returns 124 with the program still up.  Read it with
   `pairshell screen`, answer with `pairshell keys` (`q`, `Enter`, `C-c`,
   `--literal ":wq" Enter`).  Prefer non-interactive flags when you can
   (`git --no-pager`, `apt-get -y`, `head` instead of `less`).
5. When the user says "I ran X, check it" or "look at my screen", do not run
   X again: `pairshell screen -n 200` (or more) shows their commands and
   output, because it is the same scrollback.
6. After the user interrupts you or types something, re-read the screen
   before continuing; your mental model of the pane is stale.

## Mind the remote shell

`pairshell status` reports the family.  In **tcsh/csh**:

* `2>/dev/null` does not exist (`Ambiguous output redirect`); use `>& /dev/null`
  or `>& file` to capture both streams.
* `!` expands history even inside single quotes: write `\!`.
* variables: `setenv NAME value`, `set x = 1`; the exit code is `$status`.
* `&&`/`||`/`;` work, but a parse error anywhere throws the whole line away
  (you will see rc 125).

In bash/zsh everything is as usual.  One line per `exec` argument: newlines
are rejected, so use `;` or `&&`, or write a script file first.

## What not to do

* No `--force` unless the user explicitly asked you to type over what is on
  the screen.
* No resending after 124; the first copy is still running.
* No `pairshell ctl`: it runs commands in a hidden channel the user cannot
  see.  It exists only for diagnosing the connection (`pairshell ctl "tmux ls"`).
* No `pairshell stop`/`rm`/`edit` unless the user asks; `stop` only drops the
  agent's connection, the remote tmux session survives.

## Examples

**User:** "在 lab1 上把 tests 跑一遍，看哪裡壞" (run the tests on lab1)

```
pairshell status --to lab1
pairshell exec --to lab1 "cd ~/proj && make test > /tmp/test.log 2>&1" --timeout 600
pairshell exec --to lab1 "grep -nE 'FAIL|Error' /tmp/test.log | head -20" "tail -n 20 /tmp/test.log"
```

**User:** "it's stuck, what is it doing?"

```
pairshell screen -n 50        # read first
pairshell keys C-c            # only if the user wants it stopped
```

**User:** "I just edited the config by hand, continue"

```
pairshell screen -n 100       # see what they changed and where the prompt is
pairshell exec "cat ~/proj/config.ini"
```
