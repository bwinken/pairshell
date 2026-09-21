# pairshell for VS Code

A thin UI over the `pairshell` CLI:

* **Profiles view** (activity bar): every profile with its live state
  (stopped / connecting / idle / busy), refreshed every few seconds from
  `pairshell list --json`.  Click a profile to open an editor-area terminal
  running `pairshell attach <profile>`.
* **Status bar item**: the agent's current target (`pairshell current`) and
  whether the shared pane is idle or busy; click to switch the target.
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

## Build the `.vsix` (for offline installation)

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
