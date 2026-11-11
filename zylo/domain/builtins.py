"""Built-in function catalog."""

BUILTINS = {"print", "range", "len", "int", "float", "str", "append", "exit",
            "some", "ok", "err", "parse_int", "parse_float",
            "sum", "min", "max", "abs", "sorted", "reversed", "enumerate", "zip",
            "sqrt", "pow", "round", "floor", "ceil",
            "argv", "input", "read_line", "read_stdin", "read_file", "write_file", "append_file",
            "file_exists", "list_dir", "env", "eprint", "time", "sleep"}
# Builtins a program may redefine with its own function (the core ones may not).
SHADOWABLE = BUILTINS - {"print", "range", "len", "int", "float", "str", "append", "exit", "some", "ok", "err"}
