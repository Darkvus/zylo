"""Built-in function catalog."""

BUILTINS = {"print", "range", "len", "int", "float", "str", "append", "exit",
            "some", "ok", "err", "parse_int", "parse_float",
            "sum", "min", "max", "abs", "sorted", "reversed", "enumerate", "zip",
            "sqrt", "pow", "round", "floor", "ceil",
            "argv", "input", "read_line", "read_stdin", "read_file", "write_file", "append_file",
            "file_exists", "list_dir", "env", "eprint", "time", "sleep",
            "cell", "to_json", "from_json", "json_schema", "json_schema_defs"}
# Built-ins whose first argument is a type, written like any type (`from_json(list[int], j)`).
TYPE_ARG_BUILTINS = {"from_json", "json_schema", "json_schema_defs"}
# Builtins a program may redefine with its own function (the core ones may not).
SHADOWABLE = BUILTINS - {"cell", "to_json", "from_json", "json_schema", "json_schema_defs", "print", "range", "len", "int", "float", "str", "append", "exit", "some", "ok", "err"}
