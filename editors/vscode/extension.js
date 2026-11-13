// Starts `zylo lsp` and connects VS Code to it: live diagnostics, formatting, outline, completion, hover,
// go to definition, rename and signature help (the client needs no code for them: it follows the server's capabilities).
const vscode = require("vscode");
const { LanguageClient } = require("vscode-languageclient/node");

let client;

function activate(context) {
  const command = vscode.workspace.getConfiguration("zylo").get("path") || "zylo";
  client = new LanguageClient(
    "zylo",
    "Zylo Language Server",
    { command, args: ["lsp"] },
    { documentSelector: [{ scheme: "file", language: "zylo" }, { scheme: "untitled", language: "zylo" }] }
  );
  client.start();
  context.subscriptions.push({ dispose: () => client && client.stop() });
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
