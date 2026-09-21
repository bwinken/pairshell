# pairshell for VS Code

A thin UI over the `pairshell` CLI:

* **Profiles view** (activity bar): every profile with its live state
  (stopped / connecting / idle / busy), refreshed every few seconds from
  `pairshell list --json` while the window has focus.  A busy pane shows the
  command pairshell typed and how long it has been running
  (`busy · make -j8 · 12m30s`); the tooltip adds the busy reason, the pending
  command (`pairshell wait` collects it) and how many terminals are attached.
  Click a profile to open an editor-area terminal running
  `pairshell attach <profile>`; a second click focuses that terminal.
* **Status bar item**: the agent's current target (`pairshell current`) and
  whether the shared pane is idle or busy, with the running command; click to
  switch the target.  When the CLI cannot be run, the item and the welcome
  view say so and link to the `pairshell.path` setting.
* **Add / Edit / Remove** through input boxes (the telnet password is passed
  to `pairshell add --password-stdin` on stdin and stored by pairshell in the
  Windows Credential Manager, never by the extension).
* Context menu: *Show Screen* (`pairshell screen -n 50` in an output
  channel), *Stop serve*, *Open serve Log*.

All logic stays in Python; the extension only shells out to
`pairshell list --json`, `status --json`, `attach`, `stop`, `current`,
`add`, `edit`, `rm`.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `pairshell.path` | `pairshell` | Command to run pairshell, e.g. `python -m pairshell` if it is not on PATH. |
| `pairshell.refreshIntervalSeconds` | `3` | Poll interval for the tree and status bar. |
| `pairshell.terminalLocation` | `editor` | `editor` or `panel` for attach terminals. |

## Install

`pairshell install-vscode` builds the package from the compiled copy that
ships inside pairshell (`pairshell/vscode_ext`) and installs it; no node
needed.  After changing the TypeScript, refresh that copy with
`python tools/build_vscode_bundle.py` (uses `vscode/node_modules/.bin/tsc`
when `npm install` was run, else a global `tsc`).

## Build the `.vsix` with vsce instead

```bat
cd vscode
npm install
npm run compile
npx @vscode/vsce package      :: -> pairshell-vscode-0.1.0.vsix
```

Install on the airgapped workstation with *Extensions: Install from VSIX...*
(or `code --install-extension pairshell-vscode-0.1.0.vsix`).  The extension
has no runtime npm dependencies; only `pairshell` itself must be installed on
the machine.

## Development

Open the `vscode/` folder in VS Code, `npm install`, press F5 (Extension
Development Host).  `npm run watch` recompiles on save.
