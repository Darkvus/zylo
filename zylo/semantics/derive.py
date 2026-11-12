"""Typed JSON: the conversion functions behind `to_json`, `from_json`, `json_schema` and `json_schema_defs`.

Zylo has no generics, so these built-ins are expanded per type. For every type that is converted, `Deriver` writes the
source of an ordinary Zylo function, parses it and hands it to the checker, which declares it, adds it to the program and
checks it like user code. Calls of the built-in are rewritten into calls of these functions, so the borrow inference, the
code generator and the module system need no special cases. See docs/design.md ("Typed JSON").

Helpers are named after their type (`_zj_to_user_1a2b3c4d`), so asking twice for the same type finds the first one, also
when the checker runs again over a tree it already expanded. The parts that do not depend on the type (reading numbers,
error paths, schema pieces) are private functions of `std.json`.
"""
import copy
import dataclasses
import hashlib
import re
from collections.abc import Callable

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import (
    BOOL,
    FLOAT,
    INT,
    STR,
    UNIT,
    DictT,
    EnumT,
    FnT,
    ListT,
    OptionT,
    ResultT,
    SetT,
    StructT,
    TupleT,
    Type,
)
from zylo.syntax.parser import parse

J = "std\u00b7json\u00b7"  # the linker's name for the functions of std.json
JSON = J + "Json"
JSON_T = EnumT(JSON)
PLACEHOLDER = "__zd{}__"  # stands for a field's default value in generated source; replaced by a copy of its node
SCHEMA_TYPES = {INT: "integer", FLOAT: "number", BOOL: "boolean", STR: "string"}


def lit(text: str) -> str:
    """Write a string as a Zylo string literal."""
    body = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return '"' + body.replace("\0", "\\0") + '"'


def helper_name(kind: str, ty: Type) -> str:
    """The name of the generated function of one kind for one type.

    Args:
        kind: `to`, `from`, `def`, `schema` or `defs`.
        ty: The type.

    Returns:
        A readable, lowercase name ending in a hash of the type, so different types never collide.
    """
    text = str(ty)
    slug = re.sub(r"[^0-9a-z]+", "_", text.lower()).strip("_")[:40]
    return f"_zj_{kind}_{slug}_{hashlib.sha1(text.encode()).hexdigest()[:8]}"


class Deriver:
    """Generates the helper functions for the typed JSON built-ins of one program.

    Attributes:
        fresh: Functions written since the checker last took them; each still has to be declared and added to the program.
    """

    def __init__(self, structs: dict, enums: dict, exists: Callable[[str], bool]) -> None:
        """Create a deriver.

        Args:
            structs: The checker's struct table (name to `StructInfo`).
            enums: The checker's enum table (name to `EnumInfo`).
            exists: Tells whether a function of that name is already declared.
        """
        self.structs = structs
        self.enums = enums
        self.exists = exists
        self.made: set[str] = set()
        self.fresh: list[ast.FnDef] = []
        self.where: ast.Node | None = None  # the call being expanded: generated code is positioned there

    # ---- public entry points ----------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether `std.json` is part of the program."""
        return JSON in self.enums

    def take(self) -> list[ast.FnDef]:
        """Return the functions written since the last call, and forget them."""
        new, self.fresh = self.fresh, []
        return new

    def to_json(self, ty: Type, node: ast.Node) -> str:
        """Name of the function that turns a `ty` into `Json`."""
        return self.request("to_json", "to", ty, node)

    def from_json(self, ty: Type, node: ast.Node) -> str:
        """Name of the function `(Json, path) -> Result[ty, str]`."""
        return self.request("from_json", "from", ty, node)

    def json_schema(self, ty: Type, node: ast.Node) -> str:
        """Name of the function `() -> Json` returning the schema of `ty`."""
        return self.request("json_schema", "schema", ty, node)

    def json_schema_defs(self, ty: Type, node: ast.Node) -> str:
        """Name of the function `() -> Json` returning the schemas of every named type inside `ty`."""
        return self.request("json_schema_defs", "defs", ty, node)

    # ---- validation ----------------------------------------------------------------------------------

    def request(self, op: str, kind: str, ty: Type, node: ast.Node) -> str:
        """Make sure the function exists (writing it and the ones it calls if not) and return its name."""
        self.where = node
        self.validate(op, ty, str(ty), str(ty), set(), node)
        return self.want(kind, ty)

    def validate(self, op: str, ty: Type, top: Type | str, trail: str, seen: set[str], node: ast.Node) -> None:
        """Reject a type that has no JSON form, naming where it hides (`Config.handler`)."""
        def fail(why: str) -> ZyloError:
            return ZyloError(f"{op} cannot handle {top}: {trail} {why}", node.line, node.col, node.file)

        if ty in SCHEMA_TYPES or ty == JSON_T:
            return
        if isinstance(ty, FnT):
            raise fail(f"has the function type {ty}, and functions are not data")
        if isinstance(ty, ResultT):
            raise fail(f"is a {ty}: it is control flow, not data; `match` on it and convert the value")
        if ty == UNIT:
            raise fail("has no value")
        if isinstance(ty, DictT):
            if ty.key not in (STR, INT):
                raise fail(f"is a {ty}, but JSON object keys must be str or int")
            self.validate(op, ty.val, top, f"{trail}[{ty.key}]", seen, node)
        elif isinstance(ty, (ListT, SetT)):
            self.validate(op, ty.elem, top, f"{trail}[]", seen, node)
        elif isinstance(ty, OptionT):
            self.validate(op, ty.inner, top, trail, seen, node)
        elif isinstance(ty, TupleT):
            for i, t in enumerate(ty.elems):
                self.validate(op, t, top, f"{trail}[{i}]", seen, node)
        elif isinstance(ty, StructT) and ty.name not in seen:
            seen.add(ty.name)
            for name, t in self.structs[ty.name].fields.items():
                self.validate(op, t, top, f"{self.shown(ty.name)}.{name}", seen, node)
        elif isinstance(ty, EnumT) and ty.name not in seen:
            seen.add(ty.name)
            for name, ts in self.enums[ty.name].variants.items():
                for t in ts:
                    self.validate(op, t, top, f"{self.shown(ty.name)}.{name}", seen, node)

    def shown(self, name: str) -> str:
        """A struct's or enum's name as the user wrote it."""
        d = (self.structs.get(name) or self.enums[name]).defn
        return d.display or d.name

    def component(self, name: str) -> str:
        """The name a struct or enum has under `components/schemas`.

        It is the plain name, unless two types share it (from different modules): then the module is kept (`shapes.Point`).
        """
        plain = self.shown(name)
        clash = [n for n in (*self.structs, *self.enums) if n != name and self.shown(n) == plain]
        return name.replace("\u00b7", ".") if clash else plain

    # ---- writing functions -----------------------------------------------------------------------------

    def want(self, kind: str, ty: Type) -> str:
        """Return the name of a helper, writing it first when it does not exist yet."""
        name = helper_name(kind, ty)
        if name in self.made or self.exists(name):
            return name
        self.made.add(name)
        defaults: dict[str, ast.Expr] = {}
        src = getattr(self, f"gen_{kind}")(name, ty, defaults)
        prog = parse(src)
        fn = prog.body[0]
        for n in ast.walk(fn):  # an error in generated code is reported where the built-in is called
            n.line, n.col, n.file = self.where.line, self.where.col, self.where.file
        self.substitute(fn, defaults)
        self.fresh.append(fn)
        return name

    def substitute(self, node: ast.Node, defaults: dict[str, ast.Expr]) -> None:
        """Replace each placeholder name in a generated function by a copy of the default value it stands for."""
        for f in dataclasses.fields(node):
            if f.name in ast._ANNOTATIONS:
                continue
            v = getattr(node, f.name)
            if isinstance(v, ast.Name) and v.id in defaults:
                setattr(node, f.name, copy.deepcopy(defaults[v.id]))
            elif isinstance(v, ast.Node):
                self.substitute(v, defaults)
            elif isinstance(v, list):
                for i, x in enumerate(v):
                    if isinstance(x, ast.Name) and x.id in defaults:
                        v[i] = copy.deepcopy(defaults[x.id])
                    elif isinstance(x, ast.Node):
                        self.substitute(x, defaults)

    # ---- to_json ------------------------------------------------------------------------------------------

    def to_expr(self, ty: Type, v: str) -> str:
        """Source of an expression of type `Json` holding the value `v` of type `ty`."""
        if ty == INT:
            return f"{J}_int_num({v})"
        if ty == FLOAT:
            return f"{JSON}.Num({v})"
        if ty == BOOL:
            return f"{JSON}.Bool({v})"
        if ty == STR:
            return f"{JSON}.Str({v})"
        if ty == JSON_T:
            return v
        return f"{self.want('to', ty)}({v})"

    def gen_to(self, name: str, ty: Type, defaults: dict) -> str:
        """Source of the function that turns a `ty` into `Json`."""
        head = f"fn {name}(v: {ty}) -> {JSON}:\n"
        if ty == JSON_T or ty in SCHEMA_TYPES:
            return head + f"    return {self.to_expr(ty, 'v')}\n"
        if isinstance(ty, (ListT, SetT)):
            return (head + f"    out: list[{JSON}] = []\n    for x in v:\n"
                    f"        append(out, {self.to_expr(ty.elem, 'x')})\n    return {JSON}.Arr(out)\n")
        if isinstance(ty, DictT):
            key = "k" if ty.key == STR else "str(k)"
            return (head + f"    out: dict[str, {JSON}] = {{}}\n    for k, x in v.items():\n"
                    f"        out[{key}] = {self.to_expr(ty.val, 'x')}\n    return {JSON}.Obj(out)\n")
        if isinstance(ty, TupleT):
            items = ", ".join(self.to_expr(t, f"v[{i}]") for i, t in enumerate(ty.elems))
            return head + f"    return {JSON}.Arr([{items}])\n"
        if isinstance(ty, OptionT):
            return (head + f"    match v:\n        some(x):\n            return {self.to_expr(ty.inner, 'x')}\n"
                    f"        none:\n            return {JSON}.Null\n")
        if isinstance(ty, StructT):
            body = f"    out: dict[str, {JSON}] = {{}}\n"
            for fname, ft in self.structs[ty.name].fields.items():
                body += f"    out[{lit(fname)}] = {self.to_expr(ft, 'v.' + fname)}\n"
            return head + body + f"    return {JSON}.Obj(out)\n"
        return head + self.enum_to(ty)

    def enum_to(self, ty: EnumT) -> str:
        """The body of `gen_to` for an enum."""
        info = self.enums[ty.name]
        unit_only = all(not ts for ts in info.variants.values())
        body = "    match v:\n"
        for vname, ts in info.variants.items():
            binds = ", ".join(f"a{i}" for i in range(len(ts)))
            body += f"        {ty.name}.{vname}{f'({binds})' if ts else ''}:\n"
            if unit_only:
                body += f"            return {JSON}.Str({lit(vname)})\n"
                continue
            body += f"            out: dict[str, {JSON}] = {{}}\n            out[\"type\"] = {JSON}.Str({lit(vname)})\n"
            if len(ts) == 1:
                body += f"            out[\"value\"] = {self.to_expr(ts[0], 'a0')}\n"
            elif ts:
                items = ", ".join(self.to_expr(t, f"a{i}") for i, t in enumerate(ts))
                body += f"            out[\"values\"] = {JSON}.Arr([{items}])\n"
            body += f"            return {JSON}.Obj(out)\n"
        return body

    # ---- from_json -----------------------------------------------------------------------------------------

    def from_expr(self, ty: Type, j: str, path: str) -> str:
        """Source of an expression of type `Result[ty, str]` reading the `Json` expression `j`."""
        reader = {INT: "int", FLOAT: "float", BOOL: "bool", STR: "str"}.get(ty)
        if reader:
            return f"{J}_read_{reader}({j}, {path})"
        return f"{self.want('from', ty)}({j}, {path})"

    def gen_from(self, name: str, ty: Type, defaults: dict) -> str:
        """Source of the function `(Json, path) -> Result[ty, str]`."""
        head = f"fn {name}(j: {JSON}, path: str) -> Result[{ty}, str]:\n"
        if ty == JSON_T:
            return head + "    return ok(j)\n"
        if ty in SCHEMA_TYPES:
            return head + f"    return {self.from_expr(ty, 'j', 'path')}\n"
        if isinstance(ty, (ListT, SetT)):
            add = "append(out, {})" if isinstance(ty, ListT) else "out.add({})"
            read = self.from_expr(ty.elem, "it", f"{J}_at(path, i)")
            return (head + f"    items = {J}_read_arr(j, path)?\n    out: {ty} = {'[]' if isinstance(ty, ListT) else '{}'}\n"
                    f"    i = 0\n    for it in items:\n        {add.format(read + '?')}\n        i += 1\n    return ok(out)\n")
        if isinstance(ty, DictT):
            if ty.key == STR:
                key, key_line = "k", ""
            else:
                key, key_line = "n", f"        n = {J}_read_int_key(k, path)?\n"
            read = self.from_expr(ty.val, "x", f"{J}_key(path, k)")
            return (head + f"    o = {J}_read_obj(j, path)?\n    out: {ty} = {{}}\n    for k, x in o.items():\n{key_line}"
                    f"        out[{key}] = {read}?\n    return ok(out)\n")
        if isinstance(ty, TupleT):
            n = len(ty.elems)
            body = f"    items = {J}_read_arr(j, path)?\n    {J}_read_len(items, {n}, path)?\n"
            for i, t in enumerate(ty.elems):
                body += f"    f{i} = {self.from_expr(t, f'items[{i}]', f'{J}_at(path, {i})')}?\n"
            return head + body + f"    return ok(({', '.join(f'f{i}' for i in range(n))}))\n"
        if isinstance(ty, OptionT):
            return (head + f"    match j:\n        {JSON}.Null:\n            return ok(none)\n        _:\n"
                    f"            v = {self.from_expr(ty.inner, 'j', 'path')}?\n            return ok(some(v))\n")
        if isinstance(ty, StructT):
            return head + self.struct_from(ty, defaults)
        return head + self.enum_from(ty)

    def struct_from(self, ty: StructT, defaults: dict) -> str:
        """The body of `gen_from` for a struct."""
        info = self.structs[ty.name]
        body = f"    o = {J}_read_obj(j, path)?\n"
        fields = info.defn.fields
        for i, f in enumerate(fields):
            ft = info.fields[f.name]
            fpath = f"{J}_field(path, {lit(f.name)})"
            if f.default is not None:
                ph = PLACEHOLDER.format(i)
                defaults[ph] = f.default
                body += (f"    f{i}: {ft} = {ph}\n    m{i} = o.get({lit(f.name)})\n    if m{i} is some:\n"
                         f"        f{i} = {self.from_expr(ft, f'm{i}', fpath)}?\n")
            elif isinstance(ft, OptionT):
                body += (f"    j{i} = {JSON}.Null\n    m{i} = o.get({lit(f.name)})\n    if m{i} is some:\n        j{i} = m{i}\n"
                         f"    f{i} = {self.from_expr(ft, f'j{i}', fpath)}?\n")
            else:
                body += (f"    j{i} = {J}_member(o, {lit(f.name)}, path)?\n"
                         f"    f{i} = {self.from_expr(ft, f'j{i}', fpath)}?\n")
        args = ", ".join(f"f{i}" for i in range(len(fields)))
        return body + f"    return ok({ty.name}({args}))\n"

    def enum_from(self, ty: EnumT) -> str:
        """The body of `gen_from` for an enum."""
        info = self.enums[ty.name]
        names = ", ".join(lit(v) for v in info.variants)
        unit_only = all(not ts for ts in info.variants.values())
        if unit_only:
            body = f"    s = {J}_read_str(j, path)?\n"
            for vname in info.variants:
                body += f"    if s == {lit(vname)}:\n        return ok({ty.name}.{vname})\n"
            return body + f"    return err({J}_bad_variant(path, s, {lit(names)}))\n"
        tpath = f"{J}_field(path, \"type\")"
        body = (f"    o = {J}_read_obj(j, path)?\n    tag = {J}_read_str({J}_member(o, \"type\", path)?, {tpath})?\n")
        for vname, ts in info.variants.items():
            body += f"    if tag == {lit(vname)}:\n"
            if not ts:
                body += f"        return ok({ty.name}.{vname})\n"
            elif len(ts) == 1:
                vpath = f"{J}_field(path, \"value\")"
                member = f'{J}_member(o, "value", path)?'
                body += (f"        v0 = {self.from_expr(ts[0], member, vpath)}?\n"
                         f"        return ok({ty.name}.{vname}(v0))\n")
            else:
                vpath = f"{J}_field(path, \"values\")"
                body += (f"        items = {J}_read_arr({J}_member(o, \"values\", path)?, {vpath})?\n"
                         f"        {J}_read_len(items, {len(ts)}, {vpath})?\n")
                for i, t in enumerate(ts):
                    body += f"        v{i} = {self.from_expr(t, f'items[{i}]', f'{J}_at({vpath}, {i})')}?\n"
                body += f"        return ok({ty.name}.{vname}({', '.join(f'v{i}' for i in range(len(ts)))}))\n"
        return body + f"    return err({J}_bad_variant({tpath}, tag, {lit(names)}))\n"

    # ---- json_schema ------------------------------------------------------------------------------------------

    def schema_expr(self, ty: Type) -> str:
        """Source of an expression of type `Json`: the schema of `ty` (a `$ref` for a struct or an enum)."""
        if ty in SCHEMA_TYPES:
            return f"{J}_schema_type({lit(SCHEMA_TYPES[ty])})"
        if ty == JSON_T:
            return f"{J}_schema_any()"
        if isinstance(ty, (ListT, SetT)):
            return f"{J}_schema_array({self.schema_expr(ty.elem)}, {'true' if isinstance(ty, SetT) else 'false'})"
        if isinstance(ty, DictT):
            return f"{J}_schema_map({self.schema_expr(ty.val)}, {'true' if ty.key == INT else 'false'})"
        if isinstance(ty, TupleT):
            return f"{J}_schema_tuple([{', '.join(self.schema_expr(t) for t in ty.elems)}])"
        if isinstance(ty, OptionT):
            if ty.inner in SCHEMA_TYPES:
                return f"{J}_schema_nullable({lit(SCHEMA_TYPES[ty.inner])})"
            return f"{J}_schema_any_null({self.schema_expr(ty.inner)})"
        self.want("def", ty)  # a named type: the schema is a reference, and its definition must exist
        return f"{J}_schema_ref({lit(self.component(ty.name))})"

    def gen_schema(self, name: str, ty: Type, defaults: dict) -> str:
        """Source of the function returning the schema of `ty` (a named type is expanded, not referenced)."""
        if isinstance(ty, (StructT, EnumT)) and ty != JSON_T:
            expr = f"{self.want('def', ty)}()"
        else:
            expr = self.schema_expr(ty)
        return f"fn {name}() -> {JSON}:\n    return {expr}\n"

    def gen_def(self, name: str, ty: Type, defaults: dict) -> str:
        """Source of the function returning the full schema of a struct or an enum."""
        head = f"fn {name}() -> {JSON}:\n"
        if isinstance(ty, StructT):
            info = self.structs[ty.name]
            body = f"    props: dict[str, {JSON}] = {{}}\n    required: list[str] = []\n"
            for i, f in enumerate(info.defn.fields):
                ft = info.fields[f.name]
                s = self.schema_expr(ft)
                if f.default is not None:
                    ph = PLACEHOLDER.format(i)
                    defaults[ph] = f.default
                    s = f"{J}_schema_with({s}, \"default\", {self.to_expr(ft, ph)})"
                else:
                    if not isinstance(ft, OptionT):
                        body += f"    append(required, {lit(f.name)})\n"
                if f.doc:
                    s = f"{J}_schema_doc({s}, {lit(f.doc)})"
                body += f"    props[{lit(f.name)}] = {s}\n"
            doc = info.defn.doc or ""
            return head + body + f"    return {J}_schema_doc({J}_schema_object(props, required), {lit(doc)})\n"
        info = self.enums[ty.name]
        doc = lit(info.defn.doc or "")
        if all(not ts for ts in info.variants.values()):
            names = ", ".join(lit(v) for v in info.variants)
            return head + f"    return {J}_schema_doc({J}_schema_strings([{names}]), {doc})\n"
        body = f"    variants: list[{JSON}] = []\n"
        for v in info.defn.variants:
            ts = info.variants[v.name]
            if not ts:
                payload, key = f"{J}_schema_type(\"null\")", ""
            elif len(ts) == 1:
                payload, key = self.schema_expr(ts[0]), "value"
            else:
                payload, key = f"{J}_schema_tuple([{', '.join(self.schema_expr(t) for t in ts)}])", "values"
            body += f"    append(variants, {J}_schema_variant({lit(v.name)}, {lit(key)}, {payload}, {lit(v.doc or '')}))\n"
        return head + body + f"    return {J}_schema_doc({J}_schema_one_of(variants), {doc})\n"

    def reachable(self, ty: Type, out: dict[str, Type]) -> None:
        """Collect the structs and enums that are, or are inside, `ty`, by component name."""
        if isinstance(ty, (ListT, SetT)):
            self.reachable(ty.elem, out)
        elif isinstance(ty, DictT):
            self.reachable(ty.val, out)
        elif isinstance(ty, OptionT):
            self.reachable(ty.inner, out)
        elif isinstance(ty, TupleT):
            for t in ty.elems:
                self.reachable(t, out)
        elif isinstance(ty, (StructT, EnumT)) and ty != JSON_T and self.component(ty.name) not in out:
            out[self.component(ty.name)] = ty
            parts = self.structs[ty.name].fields.values() if isinstance(ty, StructT) else \
                [t for ts in self.enums[ty.name].variants.values() for t in ts]
            for t in parts:
                self.reachable(t, out)

    def gen_defs(self, name: str, ty: Type, defaults: dict) -> str:
        """Source of the function returning `{component name: schema}` for every named type inside `ty`."""
        found: dict[str, Type] = {}
        self.reachable(ty, found)
        body = f"    out: dict[str, {JSON}] = {{}}\n"
        for comp, t in found.items():
            body += f"    out[{lit(comp)}] = {self.want('def', t)}()\n"
        return f"fn {name}() -> {JSON}:\n{body}    return {JSON}.Obj(out)\n"
