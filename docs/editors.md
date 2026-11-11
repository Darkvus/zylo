# Editor support

Zylo ships a language server (`zylo lsp`), a formatter (`zylo fmt`) and a Visual Studio Code extension.

## What you get

| Feature | Notes |
|---|---|
| **Diagnostics** | Every syntax and type error, checked on the text you are typing, not only on save. A syntax error stops type checking (it would only add noise); an error inside an imported module is shown at the top of the file that imports it, naming the module. |
| **Formatting** | The same canonical style as `zylo fmt`. |
| **Outline** | Functions, structs (with their fields and methods) and top-level variables. |
| **Completion** | Keywords, built-in functions and the functions, structs and variables of the file. |

Not yet: hover, go to definition, rename and signature help.

## Visual Studio Code

The extension adds highlighting and starts `zylo lsp` for you. [Install Zylo](install.md) first, so that `zylo` is on your
`PATH` (or point the `zylo.path` setting at it). Then, from a clone of the repository:

```
cd editors/vscode
npm install
npx vsce package
code --install-extension zylo-*.vsix
```

Turn on *Format on Save* in VS Code to format `.zy` files whenever you save.

## Other editors

Any editor with Language Server Protocol support can run `zylo lsp` (it talks over standard input and output).

**Neovim** (0.9+):

```lua
vim.filetype.add({ extension = { zy = "zylo" } })
vim.api.nvim_create_autocmd("FileType", {
  pattern = "zylo",
  callback = function()
    vim.lsp.start({ name = "zylo", cmd = { "zylo", "lsp" }, root_dir = vim.fs.root(0, { "zylo.toml", ".git" }) })
  end,
})
```

**Helix** (`languages.toml`):

```toml
[language-server.zylo]
command = "zylo"
args = ["lsp"]

[[language]]
name = "zylo"
scope = "source.zylo"
file-types = ["zy"]
comment-token = "#"
indent = { tab-width = 4, unit = "    " }
language-servers = ["zylo"]
formatter = { command = "zylo", args = ["fmt", "--stdout"] }
```

**Emacs** (eglot):

```elisp
(add-to-list 'auto-mode-alist '("\\.zy\\'" . prog-mode))
(add-to-list 'eglot-server-programs '(prog-mode . ("zylo" "lsp")))
```

## The formatter

```
zylo fmt src/            # rewrite every .zy file under src/
zylo fmt --check .       # exit 1 if anything would change (for CI)
zylo fmt --stdout a.zy   # print the formatted text instead of rewriting
```

It prints the syntax tree again, so it can never change what a program means (the tests check the tree before and
after, and that formatting twice changes nothing). Comments are kept next to the statements they were written beside.
The style: four-space indentation, one space around operators and after commas, double-quoted strings, one blank line
around top-level definitions and between methods, and at most one blank line in a row. A comment at the end of a block
moves to the indentation of the statement that follows it.

## Troubleshooting

- *No diagnostics appear*: run `zylo lsp` in a terminal; it should wait silently for input (press Ctrl-C to stop).
  If the command is not found, the editor cannot find it either.
- The language server logs unexpected errors to its standard error, which most editors show in an output panel.
