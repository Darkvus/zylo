# Zylo for Visual Studio Code

Syntax highlighting and a language server for [Zylo](https://github.com/Darkvus/zylo).

- **Highlighting** for `.zy` files: keywords, types, built-in functions, f-strings with their fields, escapes, comments.
- **Live diagnostics**: every syntax and type error, checked on the text you are typing (not only on save).
- **Format document** (`zylo fmt`'s style), the **outline** of functions and structs, and **completion** (members after a dot by type, names in scope, named arguments, imports).
- **Hover** (types, signatures, descriptions), **go to definition** across files, **rename** (F2, checked for collisions), **signature help** (also for built-in functions and methods) and **quick fixes** (add a missing import, add the missing `match` arms).

Everything but the highlighting comes from `zylo lsp`, so [Zylo must be installed](../../docs/install.md) and on
your `PATH` (or set `zylo.path`).

## Install from this repository

```
cd editors/vscode
npm install
npx vsce package          # creates zylo-<version>.vsix
code --install-extension zylo-*.vsix
```

The grammar in `syntaxes/` is generated from the language's own keyword and built-in lists
(`python scripts/generate_vscode_grammar.py`), so it stays in step with the compiler.
