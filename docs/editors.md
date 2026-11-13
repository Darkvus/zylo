# Editor support

Zylo ships a language server (`zylo lsp`), a formatter (`zylo fmt`) and a Visual Studio Code extension.

## What you get

| Feature | Notes |
|---|---|
| **Diagnostics** | Every syntax and type error, checked on the text you are typing, not only on save. A syntax error stops type checking (it would only add noise); an error inside an imported module is shown at the top of the file that imports it, naming the module. |
| **Formatting** | The same canonical style as `zylo fmt`. |
| **Outline** | Functions, structs (with their fields and methods) and top-level variables. |
| **Completion** | See [Completion](#completion): what you can type at the cursor, by what comes before it. |
| **Hover** | The type of a variable, parameter or loop variable (narrowed by an `is` check where it is used), the signature and description of a function, method or built-in, the fields of a struct, the variants of an enum, and the description of each. Inside a generic definition, a name whose type mentions a type parameter also shows the parameter with its bounds and the types the program instantiated it with (or that it is not instantiated). |
| **Go to definition** | Variables, parameters, functions, structs, enums, variants, methods and fields, across files: through `import`, `from ... import ... as ...` and `pub from` re-exports, down to where the thing is really defined. On a module name it opens the module's file; on `self`, its struct. |
| **Rename** | Safe renaming with a preview of every file changed; see [Renaming](#renaming). |
| **Signature help** | While you type the arguments of a call, the signature of the function, method, built-in, struct constructor or enum variant, with the current parameter highlighted. Defaults are shown (`b: int = 2`), and a named argument (`b=`) highlights its own parameter. It works while the call is still unfinished. |
| **Quick fixes** | Add a missing import; add the missing arms of a `match`. See [Quick fixes](#quick-fixes). |

## Completion

Completion is requested as you type and when you press `.`. What is offered depends on the text before the cursor, and the
line being typed does not have to be valid:

| Where | What you get |
|---|---|
| After `value.` | The **fields and methods** of the value's struct or enum (by the type the checker finds for it: `p.`, `p.move(1.0).`, `self.`, `names[0].`), or the **methods of `str`, `list`, `dict`, `set`, `Cell`, options and results**, with their signatures (`append(value: str)` on a `list[str]`) and descriptions. |
| After `module.` | The functions, structs and enums the module exports, `pub from` re-exports included; names starting with `_` are private and not offered. |
| After `Enum.` (also `module.Enum.`) | The variants, with their data (`Circle(float)`) and descriptions. |
| In the parentheses of a call | The parameters (or struct fields) that have not been given yet, as `name=`, besides the names in scope. Positional arguments already written use up the first parameters. |
| Where a type is expected (after `->`, after the `:` of a parameter, a field or an annotated variable, inside `list[`) | `int`, `str`, `list`..., the structs and enums of the file and of its imports, and the modules (for `module.Type`). |
| After `import` and `from ... import` | The modules (the standard library, and every `.zy` file of the workspace), then the names a module exports. |
| Anywhere else | The variables and parameters in scope, the definitions, imports and modules of the file, the built-in functions and the keywords, in that order. |

Nothing is offered inside a string or a comment, but `{...}` inside an f-string is code.

How it copes with incomplete code: variables in scope are found from the indentation, so the unfinished line itself is never
needed; the value in front of a dot is typed by asking the checker about that value alone in place of the unfinished line; and
statements that do not parse are skipped instead of stopping the analysis. What is still needed is that the *rest* of the program
links (its imports exist) and that the statements before the cursor, in the same function, type check: a type error above the
cursor in the same function hides the members of values below it.

## Quick fixes

Place the cursor on the name (or on the `match`) and open the quick fix menu (`Ctrl+.` in VS Code).

- **Add an import.** On a name the file does not define, the other files of the workspace and the standard library are searched
  for a function, struct or enum with that name, and one fix is offered for each module that has it:
  `Import 'double' from 'util'` adds `from util import double` after the existing imports, or joins the `from util import ...`
  line the file already has. In front of a dot (`json.parse(...)`), a module with that name is offered instead:
  `Import the module 'std.json'`. Private names (`_name`) and names the file already binds are never offered.
- **Add the missing `match` arms.** On a `match` over an enum that does not cover every variant (an arm with a guard does not
  count, a `_` arm covers everything), the missing variants are added after the last arm, spelled the way the other arms spell
  the enum (`Shape.Circle` or `shapes.Shape.Circle`), with `_` for each piece of data and a `print("TODO: ...")` body to replace.
  In a function that returns a value, the placeholder still leaves the function without a `return`, which the compiler then points out.

## Navigation

Hover and go to definition work on any name in the file, including inside f-strings. They resolve names with the
language's own scoping rules (a variable belongs to the block where it was first assigned; a function does not see the
variables of the top level) and use the type checker to tell what `p.x` or `p.move()` means from the type of `p`. So:

- Definitions, imports and the scoping of variables work even when the program has type errors or an unfinished
  statement elsewhere (a statement that does not parse is skipped).
- The *types* of variables, and which field or method a `value.name` is, need the program to link and check as far as
  that expression. A statement that does not parse is skipped, so the rest of the file keeps its types; but when the
  program cannot link at all (an import that does not exist) or the type of `value` is unknown, hovering a variable
  shows no type and `value.name` has no definition.
- Built-in functions (`print`, `len`...) and the methods of `str`, lists, dicts, sets, cells, options and results (`.append`,
  `.get`...) have hover and signature help too, with the types of the receiver filled in (`list[int].append(value: int)`).
  They have no definition to go to and cannot be renamed.
- A function's description is a string written as the first statement of its body, like the description of a struct
  (`fn area(s: Shape) -> float:` then `"Area of a shape."`); a struct, field, enum or variant takes the string right after it.

### Renaming

`prepareRename` selects the name and refuses what cannot be renamed (built-ins, modules, `self`, and anything defined in
a package or the standard library). A rename then changes **every** occurrence the language server can prove refers to
the same thing:

| What | Where it is renamed |
|---|---|
| A local variable, loop variable, pattern variable or closure parameter | Its own scope only: another function's `n` is untouched. |
| A function or struct parameter | Its body and every `name=value` argument that sets it, in any file. |
| A function, struct or enum | Its definition, every use, the type annotations, and the `import`, `from ... import` and `pub from` lines of every file of the workspace that mentions it. |
| A name imported with `as` | Only the importing file; the original keeps its name. |
| A field, method or variant | Its definition, every access (`p.x`, `p.move()`, `Shape.Circle(...)`), constructor arguments (`Point(x=1.0)`) and `match` patterns. Other structs with a field of the same name are not touched. |

The workspace is the directory of the project's `zylo.toml` (or the folder of the file when there is none). Hidden
folders, `node_modules` and `target` are skipped, and the unsaved text of open files is used instead of what is on disk.

A rename is **refused with a reason** rather than done halfway when:

- the new name is not an identifier, is a keyword (`if`, `none`...), `self` or `_`, or a built-in name (`print`, `int`, `Result`...);
- it would collide: another definition or import with that name in the module, a variable already visible where the
  name is declared, or any use inside its scope that would be captured by the new name (for example renaming
  a variable to the name of a function it calls); two members of one struct or enum with the same name;
- it would make a name that other files use private (`_name`);
- a file that mentions the name has a syntax error (a statement that does not parse cannot be searched), or the type
  of a value that a field or method name is used on is unknown, so some use could be missed. Fix the errors first;
- the thing is defined outside the workspace.

Renaming a field also changes the key that `to_json` and `from_json` use for it, since JSON keys are field names.

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
zylo fmt --line-length 100 src/   # wrap at 100 columns instead of 88
```

It prints the syntax tree again, so it can never change what a program means (the tests check the tree before and
after, and that formatting twice changes nothing). Comments are kept next to the statements they were written beside.
The style: four-space indentation, one space around operators and after commas, double-quoted strings, one blank line
around top-level definitions and between methods, and at most one blank line in a row. A comment at the end of a block
moves to the indentation of the statement that follows it.

### Line wrapping

A line that would be longer than the line length (88 by default) is wrapped. The outermost construct is broken first,
and each part is tried on one line again, so only as much is wrapped as needed:

- Calls, struct constructors, method arguments and list / dict / set / tuple literals go one item per line, with a
  trailing comma and the closing bracket on its own line.
- Long `and` / `or` / arithmetic expressions and chains of two or more method calls break inside parentheses, one
  operand or call per line (`.name(...)` lines start with the dot). A chain whose calls do not fit even one per line
  breaks the arguments of the long call instead.
- Function and method signatures, `extern fn` signatures and `from ... import` lists go one parameter or name per line;
  an import list is written inside `(...)`.
- A long `match` or-pattern is written inside parentheses, one alternative per line: `(A | B | C) if guard:`.
- A tuple after `=` or `return` gets parentheses when it has to wrap. Strings and f-strings are never split.

```zylo
total = (
    base_price * quantity
    + shipping_cost(weight, destination)
    - discount_for(customer, quantity)
)
```

Set the width with `--line-length N`, or for a whole project in `zylo.toml` (the flag wins; the language server uses the
setting too):

```toml
[format]
line-length = 100
```

A trailing comment of a wrapped statement stays on its first line. Comments written between the items of a multi-line
literal move below the statement, as before.

## Troubleshooting

- *No diagnostics appear*: run `zylo lsp` in a terminal; it should wait silently for input (press Ctrl-C to stop).
  If the command is not found, the editor cannot find it either.
- *Hover shows no type, a field has no definition or `value.` offers nothing*: the language server needs the file to link and
  type check up to that point (see [Navigation](#navigation) and [Completion](#completion)); fix the missing import or the
  earlier type error in the same function and try again.
- The language server logs unexpected errors to its standard error, which most editors show in an output panel.
