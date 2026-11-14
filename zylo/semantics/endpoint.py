"""The `endpoint(f)` built-in: the checker turns a typed function into an `Endpoint` value.

Like the typed JSON built-ins (see `derive.py`), `endpoint(f)` is expanded by writing ordinary Zylo source: a handler
`fn(Request) -> Response` that reads the parameters of `f` from the request, calls it and writes the result, and a
function returning `Endpoint(handler, operation, schemas)`. The call is rewritten into a call of that second function.
The generated code is checked like user code and compiled like it. See docs/design.md ("Endpoints").

How a parameter of `f` is read:

- `Request` is the request itself;
- `int`, `float`, `bool`, `str` and `T?` of these come from a path parameter of that name when the route has one,
  from the query string otherwise; a default value or `T?` makes them optional;
- a struct, an enum, `Json`, a list or a dict is the JSON body (at most one).
"""
import hashlib
import re

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
    StructT,
    TupleT,
    Type,
)
from zylo.semantics.derive import JSON, PLACEHOLDER, SCHEMA_TYPES, Deriver, lit

W = "std·web·"  # the linker's name for the items of std.web
HTTP = "std·http·"
REQUEST = HTTP + "Request"
RESPONSE = HTTP + "Response"
HTTP_ERROR = W + "HttpError"
ENDPOINT = W + "Endpoint"
SCALARS = (INT, FLOAT, BOOL, STR)
READERS = {INT: "_int", FLOAT: "_float", BOOL: "_bool", STR: "_str"}


class EndpointDeriver:
    """Writes the handler and the `Endpoint` constructor of the functions passed to `endpoint`.

    Attributes:
        d: The typed JSON deriver, whose conversion functions the handler calls and whose queue the new functions join.
    """

    def __init__(self, deriver: Deriver) -> None:
        """Create the deriver.

        Args:
            deriver: The program's typed JSON deriver.
        """
        self.d = deriver

    @property
    def available(self) -> bool:
        """Whether `std.web` is part of the program."""
        return ENDPOINT in self.d.structs

    def endpoint(self, name: str, defn: ast.FnDef, sig: FnT, node: ast.Node) -> str:
        """Make sure the endpoint functions of `name` exist and return the name of the one that builds the `Endpoint`.

        Args:
            name: The (linked) name of the function.
            defn: Its definition (parameters with defaults, doc string).
            sig: Its type.
            node: The `endpoint(...)` call, where errors are reported and generated code is positioned.

        Returns:
            The name of a function with no parameters that returns the `Endpoint`.

        Raises:
            ZyloError: If a parameter or the return type cannot be served.
        """
        shown = name.rsplit("·", 1)[-1]
        slug = re.sub(r"[^0-9a-z]+", "_", shown.lower()).strip("_")[:40]
        digest = hashlib.sha1(name.encode()).hexdigest()[:8]
        maker, handler = f"_zw_ep_{slug}_{digest}", f"_zw_h_{slug}_{digest}"
        if maker in self.d.made or self.d.exists(maker):
            return maker
        self.d.where = node
        defaults: dict[str, ast.Expr] = {}
        schema_types: list[Type] = []
        handler_src, op_src = self.plan(name, shown, defn, sig, node, defaults, schema_types)
        self.d.made.add(maker)
        self.d.emit(f"fn {handler}(zw_req: {REQUEST}) -> {RESPONSE}:\n{handler_src}", defaults)
        if schema_types:
            found = schema_types[0] if len(schema_types) == 1 else TupleT(tuple(schema_types))
            schemas = f"{W}_dict({self.d.want('defs', found)}())"
        else:
            schemas = f"{W}_no_schemas()"
        self.d.emit(
            f"fn {maker}() -> {ENDPOINT}:\n    op = {op_src}\n    return {ENDPOINT}({handler}, op, {schemas})\n", {}
        )
        return maker

    # ---- planning ------------------------------------------------------------------------------------------

    def plan(self, name: str, shown: str, defn: ast.FnDef, sig: FnT, node: ast.Node, defaults: dict, schema_types: list[Type]):
        """Write the body of the handler and the expression building the `Operation`.

        Returns:
            The handler's body and the source of the `Operation(...)` expression.
        """
        def fail(why: str, at: ast.Node = node) -> ZyloError:
            return ZyloError(f"endpoint({shown}): {why}", at.line, at.col, at.file)

        body = "    zw_errors: list[%s] = []\n" % JSON
        args: list[str] = []
        params: list[str] = []
        request_body = ""
        has_input = False
        bodies = 0
        for i, (p, ty) in enumerate(zip(defn.params, sig.params)):
            if isinstance(ty, StructT) and ty.name == REQUEST:
                args.append("zw_req")
                continue
            has_input = True
            inner = ty.inner if isinstance(ty, OptionT) else ty
            if inner in SCALARS:
                chunk, arg, param = self.scalar(i, p, ty, inner, defaults)
                body += chunk
                args.append(arg)
                params.append(param)
                continue
            if isinstance(ty, OptionT) or not isinstance(ty, (StructT, EnumT, ListT, DictT)):
                kinds = "int, float, bool, str, an optional of these, a struct, an enum, Json, a list, a dict or Request"
                raise fail(f"parameter {p.name!r} has the type {ty}, which cannot be read from a request (supported: {kinds})", p)
            if isinstance(ty, StructT) and ty.name == RESPONSE:
                raise fail(f"parameter {p.name!r} is a Response; a handler receives the Request", p)
            bodies += 1
            if bodies > 1:
                raise fail(f"parameter {p.name!r} is a second JSON body; a function reads at most one", p)
            self.d.validate("endpoint", ty, f"parameter {p.name!r}", str(ty), set(), p)
            chunk, arg = self.body(i, p, ty, defaults)
            body += chunk
            args.append(arg)
            schema_types.append(ty)
            request_body = f"some({self.d.schema_expr(ty)})"
        if has_input:
            body += f"    if len(zw_errors) > 0:\n        return {W}_unprocessable(zw_errors)\n"
        call = f"{name}({', '.join(args)})"
        reply, response = self.reply(sig.ret, call, schema_types, fail, node)
        body += reply
        summary, description = split_doc(defn.doc)
        responses = [f'{W}ResponseSpec(200, "Successful Response"{response})']
        if has_input:
            responses.append(f'{W}ResponseSpec(422, "Validation Error")')
        parts = [lit(""), lit(""), f"summary={lit(summary)}", f"description={lit(description)}"]
        if params:
            parts.append(f"params=[{', '.join(params)}]")
        if request_body:
            parts.append(f"request_body={request_body}")
        parts.append(f"responses=[{', '.join(responses)}]")
        return body, f"{W}Operation({', '.join(parts)})"

    def scalar(self, i: int, p: ast.Param, ty: Type, inner: Type, defaults: dict) -> tuple[str, str, str]:
        """Source that reads one scalar parameter from the path or the query string.

        Returns:
            The statements, the argument to pass, and the `Param(...)` expression for the operation.
        """
        var, name = f"zw_a{i}", lit(p.name)
        optional = isinstance(ty, OptionT)
        if p.default is not None:
            ph = PLACEHOLDER.format(f"e{i}")
            defaults[ph] = p.default
            decl, arg, required = f"    {var}: {ty} = {ph}\n", var, False
        elif optional:
            decl, arg, required = f"    {var}: {ty} = none\n", var, False
        else:
            decl, arg, required = f"    {var}: {ty}? = none\n", f"{var}.unwrap()", True
        value = f"some(zw_v{i})" if (optional or required) else f"zw_v{i}"
        miss = f"            append(zw_errors, {W}_missing(zw_w{i}, {name}))\n" if required else "            pass\n"
        chunk = (f"{decl}    zw_w{i} = {W}_where(zw_req, {name})\n    match {W}_raw(zw_req, {name}):\n        some(zw_s{i}):\n"
                 f"            match {W}{READERS[inner]}(zw_s{i}, zw_w{i}, {name}):\n                ok(zw_v{i}):\n"
                 f"                    {var} = {value}\n                err(zw_e{i}):\n                    append(zw_errors, zw_e{i})\n"
                 f"        none:\n{miss}")
        param = f'{W}Param({name}, "query", "", {"true" if required else "false"}, {lit(SCHEMA_TYPES[inner])})'
        return chunk, arg, param

    def body(self, i: int, p: ast.Param, ty: Type, defaults: dict) -> tuple[str, str]:
        """Source that parses the request body as JSON into a parameter.

        Returns:
            The statements and the argument to pass.
        """
        var = f"zw_a{i}"
        if p.default is not None:
            ph = PLACEHOLDER.format(f"e{i}")
            defaults[ph] = p.default
            decl, arg, required = f"    {var}: {ty} = {ph}\n", var, False
            value = f"zw_v{i}"
        else:
            decl, arg, required = f"    {var}: {ty}? = none\n", f"{var}.unwrap()", True
            value = f"some(zw_v{i})"
        empty = f"        append(zw_errors, {W}_missing_body())\n" if required else "        pass\n"
        read = self.d.from_expr(ty, f"zw_j{i}", lit("$"))
        chunk = (f"{decl}    if {W}_blank(zw_req.body):\n{empty}    else:\n"
                 f"        match {J_PARSE}(zw_req.body):\n            err(zw_m{i}):\n"
                 f"                return {W}error_reply(400, \"invalid JSON body: \" + zw_m{i})\n"
                 f"            ok(zw_j{i}):\n                match {read}:\n                    ok(zw_v{i}):\n"
                 f"                        {var} = {value}\n                    err(zw_e{i}):\n"
                 f"                        append(zw_errors, {W}_body_item(zw_e{i}))\n")
        return chunk, arg

    def reply(self, ret: Type, call: str, schema_types: list[Type], fail, node: ast.Node) -> tuple[str, str]:
        """Source that calls the function and answers with its result.

        Returns:
            The statements, and what to add to the 200 `ResponseSpec(...)` (its schema, or nothing).
        """
        if isinstance(ret, StructT) and ret.name == RESPONSE:
            return f"    return {call}\n", ""
        if ret == UNIT:
            return f"    {call}\n    return {W}json_reply(200, {JSON}.Null)\n", ""
        if isinstance(ret, ResultT):
            if not (isinstance(ret.err, StructT) and ret.err.name == HTTP_ERROR):
                raise fail(f"the error type of {ret} must be HttpError (from std.web), not {ret.err}")
            model = ret.ok
            if model == UNIT:
                ok_arm = f"            return {W}json_reply(200, {JSON}.Null)\n"
                schema = ""
            else:
                self.model(model, fail, node)
                ok_arm = f"            return {W}json_reply(200, {self.d.to_expr(model, 'zw_ok')})\n"
                schema = f", some({self.d.schema_expr(model)})"
                schema_types.append(model)
            return (f"    match {call}:\n        ok(zw_ok):\n{ok_arm}        err(zw_err):\n"
                    f"            return {W}error_reply(zw_err.status, zw_err.detail)\n"), schema
        self.model(ret, fail, node)
        schema_types.append(ret)
        return (f"    zw_out = {call}\n    return {W}json_reply(200, {self.d.to_expr(ret, 'zw_out')})\n",
                f", some({self.d.schema_expr(ret)})")

    def model(self, ty: Type, fail, node: ast.Node) -> None:
        """Reject a response model that has no JSON form."""
        self.d.validate("endpoint", ty, "the response model", str(ty), set(), node)


J_PARSE = "std·json·parse"


def split_doc(doc: str | None) -> tuple[str, str]:
    """Split a doc string into the summary (its first line) and the description (the rest).

    Args:
        doc: The doc string, or None.

    Returns:
        The summary and the description, both possibly empty.
    """
    lines = [line.strip() for line in (doc or "").strip().splitlines()]
    if not lines:
        return "", ""
    return lines[0], "\n".join(lines[1:]).strip()
