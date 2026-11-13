"""Built-in function catalog: the names, and the signatures and descriptions that editors show."""

from dataclasses import dataclass

from zylo.domain.types import CellT, DictT, ListT, OptionT, ResultT, SetT, Type

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


@dataclass(frozen=True)
class Builtin:
    """The documentation of a built-in function or method.

    The parameter and return types may mention `T`, `K` and `V` (the element, key and value types of the value a
    method is called on); `describe` replaces them with the real types.

    Attributes:
        name: The name.
        params: The parameters as written (`name: type`); a leading `*` marks the one that takes any number of values.
        ret: The return type as written, or None when nothing is returned.
        doc: A one-line description.
    """

    name: str
    params: tuple[str, ...]
    ret: str | None
    doc: str


def _b(name: str, params: str, ret: str | None, doc: str) -> Builtin:
    """Build a `Builtin` from a comma-separated parameter list."""
    return Builtin(name, tuple(p.strip() for p in params.split(";") if p.strip()), ret, doc)


FUNCTIONS: dict[str, Builtin] = {b.name: b for b in [
    _b("print", "*values", None, "Prints the values separated by spaces, then a newline."),
    _b("eprint", "*values", None, "Like `print`, but to standard error."),
    _b("range", "stop: int", "list[int]", "The integers below `stop`; `range(start, stop)` starts at `start`. Only usable as the iterable of a `for`."),
    _b("len", "x: str | list | dict | set", "int", "The number of characters of a string or of elements of a collection."),
    _b("int", "x: int | float | str", "int", "Converts a number or a string to an integer (a float is truncated)."),
    _b("float", "x: int | float | str", "float", "Converts a number or a string to a float."),
    _b("str", "x: int | float | bool | str", "str", "Converts a value to its text."),
    _b("append", "xs: list[T]; value: T", None, "Adds a value at the end of a list (the same as `xs.append(value)`)."),
    _b("exit", "code: int", None, "Stops the program with an exit code."),
    _b("some", "value: T", "T?", "Wraps a value in an Option."),
    _b("ok", "value: T", "Result[T, E]", "A successful Result; its type comes from the annotation of the variable or return."),
    _b("err", "error: E", "Result[T, E]", "A failed Result; its type comes from the annotation of the variable or return."),
    _b("parse_int", "text: str", "int?", "Reads an integer; none when the text is not one."),
    _b("parse_float", "text: str", "float?", "Reads a float; none when the text is not one."),
    _b("sum", "xs: list[int] | list[float]", "int | float", "The sum of the elements."),
    _b("min", "a: T; b: T", "T", "The smaller of two values, or of a list when given one argument."),
    _b("max", "a: T; b: T", "T", "The larger of two values, or of a list when given one argument."),
    _b("abs", "x: int | float", "int | float", "The absolute value."),
    _b("sorted", "xs: list[T]", "list[T]", "A sorted copy of a list."),
    _b("reversed", "xs: list[T]", "list[T]", "A reversed copy of a list."),
    _b("enumerate", "xs: list[T]", "list[tuple[int, T]]", "Pairs each element with its index."),
    _b("zip", "a: list[A]; b: list[B]", "list[tuple[A, B]]", "Pairs the elements of two lists."),
    _b("sqrt", "x: float", "float", "The square root."),
    _b("pow", "x: float; y: float", "float", "`x` raised to the power `y`."),
    _b("round", "x: float", "int", "The nearest integer."),
    _b("floor", "x: float", "int", "The largest integer not above `x`."),
    _b("ceil", "x: float", "int", "The smallest integer not below `x`."),
    _b("argv", "", "list[str]", "The command-line arguments of the program."),
    _b("input", "prompt: str", "str", "Prints the prompt (when given) and reads a line from standard input."),
    _b("read_line", "", "str?", "Reads a line from standard input; none at the end of the input."),
    _b("read_stdin", "", "str", "Reads all of standard input."),
    _b("read_file", "path: str", "Result[str, str]", "The text of a file, or the error message."),
    _b("write_file", "path: str; text: str", "Result[int, str]", "Writes a file, replacing it; the number of bytes written."),
    _b("append_file", "path: str; text: str", "Result[int, str]", "Appends text to a file, creating it if needed."),
    _b("file_exists", "path: str", "bool", "Whether a file or directory exists."),
    _b("list_dir", "path: str", "Result[list[str], str]", "The names in a directory."),
    _b("env", "name: str", "str?", "The value of an environment variable, or none."),
    _b("time", "", "float", "The Unix time in seconds."),
    _b("sleep", "seconds: float", None, "Pauses the program."),
    _b("cell", "value: T", "Cell[T]", "A new shared mutable box holding a value."),
    _b("to_json", "value: T", "Json", "Converts a struct, enum or collection to a `Json` value."),
    _b("from_json", "type, json: Json", "Result[T, str]", "Reads a value of the given type from a `Json` value; the error names the JSON path."),
    _b("json_schema", "type", "Json", "The JSON Schema of a type."),
    _b("json_schema_defs", "type", "Json", "The JSON Schema of a type, with the `$defs` of the types it uses."),
]}

STR_METHODS: dict[str, Builtin] = {b.name: b for b in [
    _b("strip", "", "str", "The string without whitespace at both ends."),
    _b("upper", "", "str", "The string in upper case."),
    _b("lower", "", "str", "The string in lower case."),
    _b("split", "sep: str", "list[str]", "The pieces between occurrences of `sep` (whitespace when it is left out)."),
    _b("join", "parts: list[str]", "str", "Joins the parts with this string between them."),
    _b("replace", "old: str; new: str", "str", "Replaces every occurrence of `old` with `new`."),
    _b("startswith", "prefix: str", "bool", "Whether the string begins with `prefix`."),
    _b("endswith", "suffix: str", "bool", "Whether the string ends with `suffix`."),
    _b("find", "sub: str", "int?", "The index of the first occurrence of `sub`, or none."),
    _b("count", "sub: str", "int", "How many times `sub` occurs."),
    _b("lines", "", "list[str]", "The lines of the string."),
    _b("chars", "", "list[str]", "The characters of the string, one per element."),
    _b("repeat", "n: int", "str", "The string repeated `n` times."),
]}

LIST_METHODS: dict[str, Builtin] = {b.name: b for b in [
    _b("append", "value: T", None, "Adds a value at the end."),
    _b("pop", "", "T?", "Removes and returns the last element, or none when the list is empty."),
    _b("insert", "index: int; value: T", None, "Inserts a value before the element at `index`."),
    _b("remove", "value: T", "bool", "Removes the first element equal to `value`; whether one was found."),
    _b("clear", "", None, "Removes every element."),
    _b("reverse", "", None, "Reverses the list in place."),
    _b("sort", "", None, "Sorts the list in place."),
    _b("extend", "other: list[T]", None, "Adds every element of another list at the end."),
    _b("index", "value: T", "int?", "The index of the first element equal to `value`, or none."),
    _b("count", "value: T", "int", "How many elements equal `value`."),
    _b("map", "f: fn(T) -> U", "list[U]", "A new list with `f` applied to each element."),
    _b("filter", "f: fn(T) -> bool", "list[T]", "The elements for which `f` is true."),
    _b("any", "f: fn(T) -> bool", "bool", "Whether `f` is true for some element."),
    _b("all", "f: fn(T) -> bool", "bool", "Whether `f` is true for every element."),
    _b("find", "f: fn(T) -> bool", "T?", "The first element for which `f` is true, or none."),
    _b("fold", "init: A; f: fn(A, T) -> A", "A", "Combines the elements into one value, starting from `init`."),
    _b("reduce", "f: fn(T, T) -> T", "T?", "Combines the elements with `f`; none for an empty list."),
    _b("sort_by", "key: fn(T) -> K", None, "Sorts the list in place by the key `key` gives for each element."),
]}

DICT_METHODS: dict[str, Builtin] = {b.name: b for b in [
    _b("get", "key: K; default: V", "V", "The value of a key: `default` when it is missing, or an Option (`V?`) when left out."),
    _b("keys", "", "list[K]", "The keys."),
    _b("values", "", "list[V]", "The values."),
    _b("items", "", "list[tuple[K, V]]", "The `(key, value)` pairs."),
    _b("pop", "key: K", "V?", "Removes a key and returns its value, or none."),
    _b("clear", "", None, "Removes every entry."),
]}

SET_METHODS: dict[str, Builtin] = {b.name: b for b in [
    _b("add", "value: T", None, "Adds an element."),
    _b("remove", "value: T", "bool", "Removes an element; whether it was there."),
    _b("union", "other: set[T]", "set[T]", "The elements of both sets."),
    _b("intersection", "other: set[T]", "set[T]", "The elements in both sets."),
    _b("difference", "other: set[T]", "set[T]", "The elements of this set that are not in the other."),
    _b("clear", "", None, "Removes every element."),
]}

CELL_METHODS: dict[str, Builtin] = {b.name: b for b in [
    _b("get", "", "T", "The value in the cell."),
    _b("set", "value: T", None, "Replaces the value in the cell."),
    _b("update", "f: fn(T) -> T", None, "Replaces the value with `f` applied to it."),
]}

OPTION_METHODS: dict[str, Builtin] = {b.name: b for b in [
    _b("unwrap", "", "T", "The value; stops the program when there is none."),
    _b("unwrap_or", "default: T", "T", "The value, or `default` when there is none."),
]}

RESULT_METHODS: dict[str, Builtin] = {b.name: b for b in [
    _b("unwrap", "", "T", "The value; stops the program when it is an error."),
    _b("unwrap_or", "default: T", "T", "The value, or `default` when it is an error."),
]}

METHODS: dict[str, dict[str, Builtin]] = {
    "str": STR_METHODS, "list": LIST_METHODS, "dict": DICT_METHODS, "set": SET_METHODS, "Cell": CELL_METHODS,
    "Option": OPTION_METHODS, "Result": RESULT_METHODS,
}


def receiver_kind(t: Type | None) -> str | None:
    """Which table of built-in methods a type uses.

    Args:
        t: The type of the value a method is called on.

    Returns:
        A key of `METHODS`, or None for a type without built-in methods.
    """
    if t is None:
        return None
    if str(t) == "str":
        return "str"
    for cls, kind in ((ListT, "list"), (DictT, "dict"), (SetT, "set"), (CellT, "Cell"), (OptionT, "Option"),
                      (ResultT, "Result")):
        if isinstance(t, cls):
            return kind
    return None


def _bindings(t: Type | None) -> dict[str, str]:
    """What `T`, `K` and `V` stand for when a method is called on a value of this type."""
    if isinstance(t, (ListT, SetT)):
        return {"T": str(t.elem)}
    if isinstance(t, DictT):
        return {"K": str(t.key), "V": str(t.val)}
    if isinstance(t, (CellT, OptionT)):
        return {"T": str(t.inner)}
    if isinstance(t, ResultT):
        return {"T": str(t.ok)}
    return {}


def _fill(text: str, names: dict[str, str]) -> str:
    """Replace the single-letter type names of a template, wherever they stand alone."""
    out, word = [], ""
    for ch in [*text, ""]:
        if ch.isalnum() or ch == "_":
            word += ch
            continue
        out.append(names.get(word, word))
        out.append(ch)
        word = ""
    return "".join(out)


def describe(b: Builtin, receiver: Type | None = None) -> tuple[list[str], str | None]:
    """The parameters and return type of a built-in, with the types of the receiver filled in.

    Args:
        b: The built-in.
        receiver: The type of the value a method is called on, or None for a function.

    Returns:
        The parameters (`name: type`) and the return type (None when nothing is returned).
    """
    names = _bindings(receiver)
    return [_fill(p, names) for p in b.params], (_fill(b.ret, names) if b.ret else None)
