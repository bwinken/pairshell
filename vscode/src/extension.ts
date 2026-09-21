// pairshell VS Code extension: a thin UI over the pairshell CLI.
// All logic lives in Python; this file only shells out to
// `pairshell list --json`, `status --json`, `attach`, `stop`, `current`, `add`, `edit`, `rm`.

import * as cp from "child_process";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

interface ProfileRow {
  name: string;
  protocol: string;
  host: string;
  port: number;
  user: string;
  target: string;
  session: string;
  rpc_port: number;
  key_path: string;
  last_used: number;
  current: boolean;
  running: boolean;
  pid: number | null;
  state: string; // stopped | connecting | idle | busy | error
  detail: string;
  foreground?: string | null;
}

interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

function config<T>(key: string, fallback: T): T {
  return vscode.workspace.getConfiguration("pairshell").get<T>(key, fallback);
}

/** Split the configured command ("python -m pairshell") into executable + args. */
function pairshellCommand(): { exe: string; args: string[] } {
  const raw = config<string>("path", "pairshell").trim() || "pairshell";
  const parts = raw.match(/(?:[^\s"]+|"[^"]*")+/g) ?? [raw];
  const clean = parts.map((p) => p.replace(/^"|"$/g, ""));
  return { exe: clean[0], args: clean.slice(1) };
}

function runPairshell(args: string[], input?: string, timeoutMs = 20000): Promise<RunResult> {
  const { exe, args: base } = pairshellCommand();
  return new Promise((resolve) => {
    const child = cp.execFile(
      exe,
      [...base, ...args],
      { timeout: timeoutMs, windowsHide: true, maxBuffer: 16 * 1024 * 1024, encoding: "utf8" },
      (error, stdout, stderr) => {
        const code = error && typeof (error as cp.ExecFileException).code === "number"
          ? ((error as cp.ExecFileException).code as number)
          : error ? 2 : 0;
        resolve({ code, stdout: stdout ?? "", stderr: (stderr ?? "") + (error && !stderr ? `\n${error.message}` : "") });
      }
    );
    if (input !== undefined && child.stdin) {
      child.stdin.write(input);
      child.stdin.end();
    }
  });
}

function stateIcon(row: ProfileRow): vscode.ThemeIcon {
  switch (row.state) {
    case "idle":
      return new vscode.ThemeIcon("circle-filled", new vscode.ThemeColor("testing.iconPassed"));
    case "busy":
      return new vscode.ThemeIcon("sync~spin", new vscode.ThemeColor("charts.yellow"));
    case "connecting":
      return new vscode.ThemeIcon("loading~spin");
    case "error":
      return new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed"));
    default:
      return new vscode.ThemeIcon("circle-outline");
  }
}

class ProfileItem extends vscode.TreeItem {
  constructor(public readonly row: ProfileRow) {
    super(row.name, vscode.TreeItemCollapsibleState.None);
    const state = row.state === "busy" && row.foreground ? `busy (${row.foreground})` : row.state;
    this.description = `${row.protocol}  ${row.target}  ·  ${state}${row.current ? "  ·  agent target" : ""}`;
    this.tooltip = new vscode.MarkdownString(
      [
        `**${row.name}** (${row.protocol} ${row.target})`,
        `tmux session: \`${row.session}\``,
        `state: ${state}${row.detail ? ` – ${row.detail}` : ""}`,
        `serve: ${row.running ? `running (pid ${row.pid}, rpc ${row.rpc_port})` : "stopped"}`,
        row.current ? "current target for `pairshell exec`" : "",
      ]
        .filter(Boolean)
        .join("\n\n")
    );
    this.iconPath = stateIcon(row);
    this.contextValue = `profile${row.running ? " running" : ""}${row.current ? " current" : ""}`;
    this.command = { command: "pairshell.attach", title: "Attach", arguments: [this] };
  }
}

class ProfileTree implements vscode.TreeDataProvider<ProfileItem> {
  private readonly emitter = new vscode.EventEmitter<ProfileItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;
  rows: ProfileRow[] = [];
  lastError = "";

  async refresh(): Promise<void> {
    const res = await runPairshell(["list", "--json"]);
    if (res.code === 0) {
      try {
        this.rows = JSON.parse(res.stdout) as ProfileRow[];
        this.lastError = "";
      } catch (e) {
        this.lastError = `cannot parse pairshell output: ${String(e)}`;
      }
    } else {
      this.lastError = res.stderr.trim() || `pairshell exited with ${res.code}`;
    }
    this.emitter.fire(undefined);
  }

  getTreeItem(element: ProfileItem): vscode.TreeItem {
    return element;
  }

  getChildren(): ProfileItem[] {
    return this.rows.map((r) => new ProfileItem(r));
  }
}

async function pickProfile(tree: ProfileTree, placeHolder: string): Promise<ProfileRow | undefined> {
  if (tree.rows.length === 0) {
    await tree.refresh();
  }
  const picked = await vscode.window.showQuickPick(
    tree.rows.map((r) => ({ label: r.name, description: `${r.protocol} ${r.target}`, detail: r.state, row: r })),
    { placeHolder }
  );
  return picked?.row;
}

function rowFrom(arg: unknown): ProfileRow | undefined {
  return arg instanceof ProfileItem ? arg.row : undefined;
}

function openAttachTerminal(name: string): void {
  const { exe, args } = pairshellCommand();
  const location =
    config<string>("terminalLocation", "editor") === "panel"
      ? vscode.TerminalLocation.Panel
      : vscode.TerminalLocation.Editor;
  const terminal = vscode.window.createTerminal({
    name: `pairshell: ${name}`,
    shellPath: exe,
    shellArgs: [...args, "attach", name],
    location,
    iconPath: new vscode.ThemeIcon("terminal"),
  });
  terminal.show();
}

async function promptProfileFields(existing?: ProfileRow): Promise<string[] | undefined> {
  const name =
    existing?.name ??
    (await vscode.window.showInputBox({
      prompt: "Profile name",
      validateInput: (v) => (/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(v) ? undefined : "letters, digits, '_', '.', '-'"),
    }));
  if (!name) {
    return undefined;
  }
  const protocol = await vscode.window.showQuickPick(["ssh", "telnet"], {
    placeHolder: existing ? `Protocol (was ${existing.protocol})` : "Protocol",
  });
  if (!protocol) {
    return undefined;
  }
  const host = await vscode.window.showInputBox({ prompt: "Host", value: existing?.host ?? "" });
  if (!host) {
    return undefined;
  }
  const port = await vscode.window.showInputBox({
    prompt: "Port",
    value: existing && existing.protocol === protocol ? String(existing.port) : protocol === "telnet" ? "23" : "22",
    validateInput: (v) => (/^\d+$/.test(v) && Number(v) > 0 && Number(v) < 65536 ? undefined : "1-65535"),
  });
  if (!port) {
    return undefined;
  }
  const user = await vscode.window.showInputBox({ prompt: "User", value: existing?.user ?? os.userInfo().username });
  if (!user) {
    return undefined;
  }
  const args = [name, "--protocol", protocol, "--host", host, "--port", port, "--user", user];
  if (protocol === "ssh") {
    const key = await vscode.window.showInputBox({
      prompt: "Private key path (empty = ssh defaults / agent)",
      value: existing?.key_path ?? path.join(os.homedir(), ".ssh", "id_ed25519"),
    });
    if (key === undefined) {
      return undefined;
    }
    args.push("--key", key);
  }
  const session = await vscode.window.showInputBox({
    prompt: "tmux session name",
    value: existing?.session ?? name.replace(/[^A-Za-z0-9_-]/g, "_"),
  });
  if (session === undefined) {
    return undefined;
  }
  if (session) {
    args.push("--session", session);
  }
  return args;
}

async function promptPassword(existing: boolean): Promise<string | undefined | null> {
  // null = keep the stored one, undefined = cancelled
  const pw = await vscode.window.showInputBox({
    prompt: existing ? "Telnet password (leave empty to keep the stored one)" : "Telnet password",
    password: true,
  });
  if (pw === undefined) {
    return undefined;
  }
  if (pw === "" && existing) {
    return null;
  }
  return pw;
}

export function activate(context: vscode.ExtensionContext): void {
  const tree = new ProfileTree();
  const view = vscode.window.createTreeView("pairshell.profiles", { treeDataProvider: tree, showCollapseAll: false });
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  status.command = "pairshell.switchCurrent";
  const output = vscode.window.createOutputChannel("pairshell");
  context.subscriptions.push(view, status, output);

  const updateStatusBar = () => {
    const cur = tree.rows.find((r) => r.current);
    if (tree.lastError) {
      status.text = "$(terminal) pairshell: unavailable";
      status.tooltip = tree.lastError;
    } else if (!cur) {
      status.text = "$(terminal) pairshell: no target";
      status.tooltip = "Click to choose the profile the agent talks to";
    } else {
      const icon = cur.state === "busy" ? "$(sync~spin)" : cur.state === "idle" ? "$(check)" : "$(circle-slash)";
      status.text = `$(terminal) ${cur.name} ${icon} ${cur.state}`;
      status.tooltip = `pairshell agent target: ${cur.name} (${cur.protocol} ${cur.target}) – ${cur.state}${
        cur.detail ? `: ${cur.detail}` : ""
      }\nClick to switch`;
    }
    status.show();
  };

  const refresh = async () => {
    await tree.refresh();
    updateStatusBar();
  };

  let timer: NodeJS.Timeout | undefined;
  const schedule = () => {
    if (timer) {
      clearInterval(timer);
    }
    const seconds = Math.max(1, config<number>("refreshIntervalSeconds", 3));
    timer = setInterval(() => void refresh(), seconds * 1000);
  };
  schedule();
  context.subscriptions.push({ dispose: () => timer && clearInterval(timer) });
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("pairshell")) {
        schedule();
        void refresh();
      }
    }),
    vscode.window.onDidCloseTerminal(() => void refresh())
  );

  const report = (title: string, res: RunResult) => {
    if (res.code !== 0) {
      void vscode.window.showErrorMessage(`${title}: ${res.stderr.trim() || res.stdout.trim() || `exit ${res.code}`}`);
    } else if (res.stderr.trim()) {
      output.appendLine(res.stderr.trim());
    }
  };

  context.subscriptions.push(
    vscode.commands.registerCommand("pairshell.refresh", refresh),

    vscode.commands.registerCommand("pairshell.attach", async (arg?: unknown) => {
      const row = rowFrom(arg) ?? (await pickProfile(tree, "Attach to which profile?"));
      if (!row) {
        return;
      }
      openAttachTerminal(row.name);
      setTimeout(() => void refresh(), 1500);
    }),

    vscode.commands.registerCommand("pairshell.setCurrent", async (arg?: unknown) => {
      const row = rowFrom(arg) ?? (await pickProfile(tree, "Which profile should the agent talk to?"));
      if (!row) {
        return;
      }
      report("pairshell current", await runPairshell(["current", row.name]));
      await refresh();
    }),

    vscode.commands.registerCommand("pairshell.switchCurrent", async () => {
      const row = await pickProfile(tree, "Which profile should the agent talk to?");
      if (!row) {
        return;
      }
      report("pairshell current", await runPairshell(["current", row.name]));
      await refresh();
    }),

    vscode.commands.registerCommand("pairshell.stop", async (arg?: unknown) => {
      const row = rowFrom(arg) ?? (await pickProfile(tree, "Stop serve for which profile?"));
      if (!row) {
        return;
      }
      report("pairshell stop", await runPairshell(["stop", row.name]));
      await refresh();
    }),

    vscode.commands.registerCommand("pairshell.screen", async (arg?: unknown) => {
      const row = rowFrom(arg) ?? (await pickProfile(tree, "Show the screen of which profile?"));
      if (!row) {
        return;
      }
      const res = await runPairshell(["screen", "--to", row.name, "-n", "50"], undefined, 60000);
      output.clear();
      output.appendLine(res.stderr.trim());
      output.appendLine(res.stdout);
      output.show(true);
    }),

    vscode.commands.registerCommand("pairshell.openLog", async (arg?: unknown) => {
      const row = rowFrom(arg) ?? (await pickProfile(tree, "Open the serve log of which profile?"));
      if (!row) {
        return;
      }
      const home =
        process.env.PAIRSHELL_HOME ||
        (process.platform === "win32"
          ? path.join(process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming"), "pairshell")
          : path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "pairshell"));
      const uri = vscode.Uri.file(path.join(home, "run", `${row.name}.log`));
      try {
        await vscode.window.showTextDocument(uri, { preview: true });
      } catch {
        void vscode.window.showInformationMessage(`No log yet at ${uri.fsPath}`);
      }
    }),

    vscode.commands.registerCommand("pairshell.add", async () => {
      const args = await promptProfileFields();
      if (!args) {
        return;
      }
      let input: string | undefined;
      if (args.includes("telnet")) {
        const pw = await promptPassword(false);
        if (pw === undefined) {
          return;
        }
        if (pw) {
          args.push("--password-stdin");
          input = pw + "\n";
        }
      }
      report("pairshell add", await runPairshell(["add", ...args], input));
      await refresh();
    }),

    vscode.commands.registerCommand("pairshell.edit", async (arg?: unknown) => {
      const row = rowFrom(arg) ?? (await pickProfile(tree, "Edit which profile?"));
      if (!row) {
        return;
      }
      const args = await promptProfileFields(row);
      if (!args) {
        return;
      }
      let input: string | undefined;
      if (args.includes("telnet")) {
        const pw = await promptPassword(true);
        if (pw === undefined) {
          return;
        }
        if (pw) {
          args.push("--password-stdin");
          input = pw + "\n";
        }
      }
      report("pairshell edit", await runPairshell(["edit", ...args], input));
      await refresh();
    }),

    vscode.commands.registerCommand("pairshell.remove", async (arg?: unknown) => {
      const row = rowFrom(arg) ?? (await pickProfile(tree, "Remove which profile?"));
      if (!row) {
        return;
      }
      const ok = await vscode.window.showWarningMessage(
        `Remove profile ${row.name} (${row.target})? The remote tmux session is not touched.`,
        { modal: true },
        "Remove"
      );
      if (ok !== "Remove") {
        return;
      }
      report("pairshell rm", await runPairshell(["rm", row.name, "-y"]));
      await refresh();
    })
  );

  void refresh();
}

export function deactivate(): void {
  // nothing to clean up beyond the subscriptions
}
