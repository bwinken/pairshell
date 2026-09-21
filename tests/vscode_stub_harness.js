// Loads the compiled extension under a stub `vscode` module and drives it
// against the real pairshell CLI (a local profile).  Exits non-zero on failure.
// Usage: node tests/vscode_stub_harness.js <path/to/extension.js> <pairshell command>
const Module = require("module");
const path = require("path");

const [, , extPath, pairshellCmd] = process.argv;
const log = (...a) => console.log("[harness]", ...a);
const fail = (msg) => { console.error("[harness] FAIL:", msg); process.exit(1); };

// ---- minimal vscode API stub ------------------------------------------------
const commands = new Map();
const config = { "pairshell.path": pairshellCmd, "pairshell.refreshIntervalSeconds": 60, "pairshell.terminalLocation": "editor" };
const state = { statusText: "", terminals: [], errors: [], infos: [], quickPickAnswer: undefined, inputAnswers: [], output: [] };

class EventEmitter { constructor() { this.h = []; this.event = (cb) => { this.h.push(cb); return { dispose() {} }; }; } fire(v) { this.h.forEach((cb) => cb(v)); } dispose() {} }
class TreeItem { constructor(label, collapsibleState) { this.label = label; this.collapsibleState = collapsibleState; } }
class ThemeIcon { constructor(id, color) { this.id = id; this.color = color; } }
class ThemeColor { constructor(id) { this.id = id; } }
class MarkdownString { constructor(v) { this.value = v; } }
const Uri = { file: (p) => ({ fsPath: p, scheme: "file" }) };

const vscode = {
  EventEmitter, TreeItem, ThemeIcon, ThemeColor, MarkdownString, Uri,
  TreeItemCollapsibleState: { None: 0 },
  StatusBarAlignment: { Left: 1 },
  TerminalLocation: { Editor: 1, Panel: 2 },
  workspace: {
    getConfiguration: () => ({ get: (k, d) => (config["pairshell." + k] !== undefined ? config["pairshell." + k] : d) }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
  },
  window: {
    createTreeView: (id, opts) => { state.tree = opts.treeDataProvider; return { dispose() {} }; },
    createStatusBarItem: () => ({ show() {}, hide() {}, dispose() {}, set text(v) { state.statusText = v; }, get text() { return state.statusText; } }),
    createOutputChannel: () => ({ appendLine: (l) => state.output.push(l), clear() {}, show() {}, dispose() {} }),
    createTerminal: (opts) => { state.terminals.push(opts); return { show() {}, dispose() {} }; },
    onDidCloseTerminal: () => ({ dispose() {} }),
    showErrorMessage: async (m) => { state.errors.push(m); },
    showInformationMessage: async (m) => { state.infos.push(m); },
    showWarningMessage: async () => "Remove",
    showQuickPick: async (items) => (state.quickPickAnswer === undefined ? undefined : items[state.quickPickAnswer]),
    showInputBox: async () => state.inputAnswers.shift(),
    showTextDocument: async () => {},
  },
  commands: { registerCommand: (id, fn) => { commands.set(id, fn); return { dispose() {} }; } },
};

const origLoad = Module._load;
Module._load = function (request, ...rest) { return request === "vscode" ? vscode : origLoad.call(this, request, ...rest); };

// ---- drive it ------------------------------------------------------------------
(async () => {
  const ext = require(path.resolve(extPath));
  const context = { subscriptions: [] };
  ext.activate(context);
  const expected = ["pairshell.attach", "pairshell.refresh", "pairshell.add", "pairshell.edit", "pairshell.remove", "pairshell.stop", "pairshell.setCurrent", "pairshell.switchCurrent", "pairshell.screen", "pairshell.openLog"];
  for (const c of expected) if (!commands.has(c)) fail(`command ${c} not registered`);
  log("all", expected.length, "commands registered");

  await commands.get("pairshell.refresh")();
  const items = state.tree.getChildren();
  if (state.tree.lastError) fail("list failed: " + state.tree.lastError);
  if (items.length < 1) fail("tree is empty");
  const item = items[0];
  const treeItem = state.tree.getTreeItem(item);
  log("tree item:", JSON.stringify({ label: treeItem.label, description: treeItem.description, contextValue: treeItem.contextValue }));
  if (!/profile/.test(treeItem.contextValue)) fail("contextValue missing 'profile'");
  if (!/stopped|idle|busy|connecting/.test(treeItem.description)) fail("description lacks a state");

  await commands.get("pairshell.setCurrent")(item);
  await commands.get("pairshell.refresh")();
  if (!state.tree.rows.find((r) => r.current)) fail("setCurrent did not mark a current profile");
  log("status bar:", state.statusText);
  if (!state.statusText.includes(item.row.name)) fail("status bar does not show the current profile");

  await commands.get("pairshell.attach")(item);
  const t = state.terminals[0];
  if (!t || !t.shellArgs.includes("attach") || !t.shellArgs.includes(item.row.name)) fail("attach terminal not created correctly: " + JSON.stringify(t));
  log("attach terminal:", t.shellPath, t.shellArgs.join(" "), "location", t.location);

  // add a profile through the input boxes (ssh, no password)
  state.inputAnswers = ["h2", "192.0.2.10", "22", "alice", "", "h2sess"];
  state.quickPickAnswer = 0; // protocol "ssh"
  await commands.get("pairshell.add")();
  await commands.get("pairshell.refresh")();
  if (!state.tree.rows.find((r) => r.name === "h2" && r.protocol === "ssh" && r.session === "h2sess")) fail("add did not create profile h2: " + JSON.stringify(state.tree.rows.map((r) => r.name)));
  log("add via input boxes ok");

  await commands.get("pairshell.remove")(state.tree.getChildren().find((i) => i.row.name === "h2"));
  await commands.get("pairshell.refresh")();
  if (state.tree.rows.find((r) => r.name === "h2")) fail("remove did not delete h2");
  log("remove ok");

  if (state.errors.length) fail("errors shown: " + state.errors.join(" | "));
  context.subscriptions.forEach((d) => d.dispose && d.dispose());
  log("OK");
  process.exit(0);
})().catch((e) => fail(e.stack || String(e)));
