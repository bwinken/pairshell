# Working with pairshell (instructions for AI agents)

You share **one** remote shell with a human.  It is a tmux pane the human is
attached to; everything you type there appears on their screen, and they can
type into it too.  `pairshell` is your only way in.

## Rules

1. **Do remote work only through `pairshell exec` and `pairshell keys`** so the
   user sees it.  `pairshell ctl` runs commands in the hidden control channel:
   use it for channel diagnostics only (`tmux ls`, `uptime`), never for work.
2. **rc 3 means the pane is busy**: the user is typing, or a program is in the
   foreground.  Nothing was sent.  Do not `--force` over the user.  Look at
   `pairshell screen`, wait, or ask them.
3. **rc 124 means your command is still running** (the `--timeout` expired).
   Run `pairshell wait --timeout N`: it blocks until the command finishes and
   returns its exit code and output exactly as `exec` would have (rc 124
   again means still running: call `wait` again).  Never resend the command.
   `pairshell keys C-c` if it should stop; `wait` then returns rc 125 once
   the prompt is back.
4. **rc 125 means the shell is back at a prompt but the command line was
   rejected** (usually a tcsh syntax error, or you started a sub-shell).  Read
   the screen tail printed on stderr and fix the command.
5. **Interactive or full-screen programs** (vim, less, top, `y/n` prompts,
   password prompts): read the screen with `pairshell screen`, answer with
   `pairshell keys` (`q`, `Enter`, `C-c`, `--literal ":wq" Enter`).  Prefer
   non-interactive flags (`less` → `head`, `git --no-pager`, `-y`).
6. **Long output**: redirect to a file on the remote and read selectively
   (`cmd > /tmp/out.txt 2>&1; wc -l /tmp/out.txt`, then `head`, `grep`,
   `sed -n '100,140p'`).  `exec` returns at most `--max-lines` (500) lines.
7. **Mind the shell.**  `pairshell status` shows the shell family.  In
   tcsh/csh: `2>/dev/null` does not exist (use `>& /dev/null`), `!` expands
   history even inside single quotes (write `\!`), variables are set with
   `setenv`/`set`, and `$?` is `$status`.
8. **If serve is down**, `exec` restarts it automatically and says so on
   stderr; if that fails, tell the user the exact command it printed
   (`pairshell serve <profile>`), do not try to work around it.
9. **When the user says "I ran X, check it"**, read `pairshell screen -n 200`
   (or more) instead of running X again.  Their commands and yours are in the
   same scrollback.
10. **One command line per `exec` argument.**  Several arguments run in order
    and stop at the first busy/timeout/no-sentinel result; check each `### rc=`.
    Quote the command in single quotes so your *local* shell does not expand
    `$VAR`, backticks, `!` or `*` before pairshell sees it; no TAB characters.
11. Shell state persists (cwd, environment, background jobs): `cd` once,
    then work; no need for absolute paths every time.

## Quick reference

```
pairshell status [--to P]              is the pane idle? what runs? which shell?
pairshell exec "cmd" ["cmd2"...] [--timeout 120] [--max-lines 500] [--force]
pairshell wait [--timeout 120]         block until the prompt is back; after rc 124 returns that command's rc and output
pairshell screen [-n N]                visible pane (+N lines of scrollback)
pairshell keys C-c | q Enter | --literal TEXT Enter
pairshell list                         profiles and which one is current
pairshell current P                    switch the default target
```

Target a specific profile with `--to <profile>`; without it the *current*
profile (the one the user last attached to) is used.

## Typical loop

```
pairshell status                      # idle? shell family?
pairshell exec "cd ~/project && git status --short"
pairshell exec "make -j8 > /tmp/build.log 2>&1" --timeout 600
pairshell wait --timeout 600          # only after rc 124: same rc and output exec would have given
pairshell exec "tail -n 40 /tmp/build.log"
```

If the build takes longer than the timeout you get rc 124 and partial
output; one `pairshell wait` then replaces a loop of `screen` polls, and
rc 124 from `wait` just means call it again.  `pairshell status` shows the
pending command.  If the tool you run pairshell from has a shorter timeout
of its own, keep `--timeout` below it: a killed local `pairshell` never
affects the remote command, and the next `wait` still collects it.
