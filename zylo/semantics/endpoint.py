"""The `endpoint(f)` built-in: the checker turns a typed function into an `Endpoint` value.

Like the typed JSON built-ins (see `derive.py`), `endpoint(f)` is expanded by writing ordinary Zylo source: a handler
`fn(Request) -> Response` that reads the parameters of `f` from the request, calls it and writes the result, and a
function returning `Endpoint(handler, operation, schemas)`. The call is rewritten into a call of that second function.
The generated code is checked like user code and compiled like it. See docs/design.md ("Endpoints").

How a parameter of `f` is read:

- `Request` is the request itself;
- `int`, `float`, `bool`, `str` and `T?` of these come from a path parameter of that name when the route has one,
  from the query string otherwise; a default value or `T?` makes them optional;
- an enum without data is read like a scalar, by variant name;
- a struct, an enum with data, `Json`, a list or a dict is the JSON body (at most one, counting the dependencies);
- a parameter whose default is `depends(g)` is not read from the request: the handler calls the top-level function `g`
  (once per request, however many parameters and other dependencies use it) and passes the result. The parameters of
  `g` are read by these same rules, together with the endpoint's own, and merged into the operation.
"""
import hashlib
import re
from http import HTTPStatus

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
from zylo.semantics.derive import JSON, JSON_T, PLACEHOLDER, SCHEMA_TYPES, Deriver, lit

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

    def endpoint(self, name: str, defn: ast.FnDef, sig: FnT, node: ast.Node, resolve, status: int = 200) -> str:
        """Make sure the endpoint functions of `name` exist and return the name of the one that builds the `Endpoint`.

        Args:
            name: The (linked) name of the function.
            defn: Its definition (parameters with defaults, doc string).
            sig: Its type.
            node: The `endpoint(...)` call, where errors are reported and generated code is positioned.
            resolve: Finds a dependency: maps a function name to its definition and type, or None when it is not a plain
                top-level function.
            status: The success status (`endpoint(f, status=201)`).

        Returns:
            The name of a function with no parameters that returns the `Endpoint`.

        Raises:
            ZyloError: If a parameter or the return type cannot be served.
        """
        shown = name.rsplit("·", 1)[-1]
        slug = re.sub(r"[^0-9a-z]+", "_", shown.lower()).strip("_")[:40]
        digest = hashlib.sha1(f"{name}/{status}".encode()).hexdigest()[:8]
        maker, handler = f"_zw_ep_{slug}_{digest}", f"_zw_h_{slug}_{digest}"
        if maker in self.d.made or self.d.exists(maker):
            return maker
        self.d.where = node
        defaults: dict[str, ast.Expr] = {}
        schema_types: list[Type] = []
        handler_src, op_src = self.plan(name, shown, defn, sig, node, defaults, schema_types, resolve, status)
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

    def plan(self, name: str, shown: str, defn: ast.FnDef, sig: FnT, node: ast.Node, defaults: dict, schema_types: list[Type],
             resolve, status: int):
        """Write the body of the handler and the expression building the `Operation`.

        Returns:
            The handler's body and the source of the `Operation(...)` expression.
        """
        def fail(why: str, at: ast.Node = node) -> ZyloError:
            return ZyloError(f"endpoint({shown}): {why}", at.line, at.col, at.file)

        st = Inputs()
        st.defaults, st.schema_types = defaults, schema_types

        def read(p: ast.Param, ty: Type, owner: str) -> str:
            return self.read(st, p, ty, owner, fail)

        deps = Dependencies(resolve, fail, read)
        args: list[str] = []
        error_statuses: set[int] = set(literal_statuses(defn))
        for p, ty in zip(defn.params, sig.params):
            if isinstance(ty, StructT) and ty.name == REQUEST:
                args.append("zw_req")
            elif ast.is_depends(p.default):
                args.append(deps.inject(p.default.args[0].id, ty, p, [name]))
            else:
                args.append(read(p, ty, shown))
        has_input = st.has_input
        body = "    zw_errors: list[%s] = []\n" % JSON + st.body
        if has_input:
            body += f"    if len(zw_errors) > 0:\n        return {W}_unprocessable(zw_errors)\n"
        body += deps.source()
        for g in deps.order:
            error_statuses.update(literal_statuses(deps.defs[g]))
        params = [st.param_expr(n) for n in st.params]
        request_body = st.request_body
        call = f"{name}({', '.join(args)})"
        reply, response = self.reply(sig.ret, call, schema_types, fail, node, status)
        body += reply
        summary, description = split_doc(defn.doc)
        responses = [f'{W}ResponseSpec({status}, "Successful Response"{response})']
        if has_input:
            responses.append(f'{W}ResponseSpec(422, "Validation Error")')
        if isinstance(sig.ret, ResultT) or deps.order:  # a function that can answer with an HttpError lists the statuses it names
            for code in sorted(error_statuses - {status, 422 if has_input else 0}):
                responses.append(f'{W}ResponseSpec({code}, {lit(status_phrase(code))})')
        parts = [lit(""), lit(""), f"summary={lit(summary)}", f"description={lit(description)}", f"operation_id={lit(shown)}"]
        if params:
            parts.append(f"params=[{', '.join(params)}]")
        if request_body:
            parts.append(f"request_body={request_body}")
        parts.append(f"responses=[{', '.join(responses)}]")
        return body, f"{W}Operation({', '.join(parts)})"

    def read(self, st: "Inputs", p: ast.Param, ty: Type, owner: str, fail) -> str:
        """Plan the reading of one parameter of the endpoint function or of a dependency.

        Args:
            st: What has been read so far (the statements, the merged parameters, the body).
            p: The parameter.
            ty: Its type.
            owner: The function that declares it, for error messages.
            fail: Builds the error to raise.

        Returns:
            The argument to pass to the function.
        """
        st.has_input = True
        st.n += 1
        i = st.n
        inner = ty.inner if isinstance(ty, OptionT) else ty
        if inner in SCALARS or self.dataless_enum(inner):
            chunk, arg = self.scalar(st, i, p, ty, inner, owner, fail)
            st.body += chunk
            return arg
        if isinstance(ty, OptionT) or not isinstance(ty, (StructT, EnumT, ListT, DictT)):
            kinds = ("int, float, bool, str, an optional of these, an enum without data, a struct, an enum, Json, a list, "
                     "a dict or Request")
            raise fail(f"parameter {p.name!r} of {owner} has the type {ty}, which cannot be read from a request "
                       f"(supported: {kinds})", p)
        if isinstance(ty, StructT) and ty.name == RESPONSE:
            raise fail(f"parameter {p.name!r} is a Response; a handler receives the Request", p)
        if st.body_owner:
            where = "a second JSON body" if st.body_owner == owner else f"a JSON body, and {st.body_owner} already reads one"
            raise fail(f"parameter {p.name!r} of {owner} is {where}; an endpoint and its dependencies read at most one", p)
        st.body_owner = owner
        self.d.validate("endpoint", ty, f"parameter {p.name!r}", str(ty), set(), p)
        chunk, arg = self.body(i, p, ty, st.defaults)
        st.body += chunk
        st.schema_types.append(ty)
        st.request_body = f"some({self.d.schema_expr(ty)})"
        return arg

    def dataless_enum(self, ty: Type) -> bool:
        """Whether a type is an enum none of whose variants carries data (read from a query or path value by name)."""
        return (isinstance(ty, EnumT) and ty != JSON_T and ty.name in self.d.enums
                and all(not ts for ts in self.d.enums[ty.name].variants.values()))

    def scalar(self, st: "Inputs", i: int, p: ast.Param, ty: Type, inner: Type, owner: str, fail) -> tuple[str, str]:
        """Source that reads one scalar (or data-less enum) parameter from the path or the query string.

        A parameter of the same name read by another function of the endpoint is merged: the types must agree, the
        parameter is required when any reader requires it, and each problem is reported once.

        Returns:
            The statements and the argument to pass.
        """
        var, name = f"zw_a{i}", lit(p.name)
        defaults = st.defaults
        known = st.params.get(p.name)
        if known and known.inner != inner:
            raise fail(f"parameter {p.name!r} is {known.inner} in {known.owner} but {inner} in {owner}; "
                       f"the same name must have the same type", p)
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
        report_missing = required and not (known and known.required)
        miss = f"            append(zw_errors, {W}_missing(zw_w{i}, {name}))\n" if report_missing else "            pass\n"
        bad = f"                    append(zw_errors, zw_e{i})\n" if known is None else "                    pass\n"
        if inner in SCALARS:
            reader = f"{W}{READERS[inner]}(zw_s{i}, zw_w{i}, {name})"
        else:
            reader = f"{W}_enum_param({self.d.from_expr(inner, f'{JSON}.Str(zw_s{i})', lit('$'))}, zw_w{i}, {name})"
        chunk = (f"{decl}    zw_w{i} = {W}_where(zw_req, {name})\n    match {W}_raw(zw_req, {name}):\n        some(zw_s{i}):\n"
                 f"            match {reader}:\n                ok(zw_v{i}):\n"
                 f"                    {var} = {value}\n                err(zw_e{i}):\n{bad}"
                 f"        none:\n{miss}")
        if known:
            known.required = known.required or required
        else:
            schema = "" if inner in SCALARS else f"some({self.d.schema_expr(inner)})"
            st.params[p.name] = Slot(inner, required, owner, schema)
            if schema:
                st.schema_types.append(inner)
        return chunk, arg

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

    def reply(self, ret: Type, call: str, schema_types: list[Type], fail, node: ast.Node, status: int = 200) -> tuple[str, str]:
        """Source that calls the function and answers with its result.

        Returns:
            The statements, and what to add to the 200 `ResponseSpec(...)` (its schema, or nothing).
        """
        if isinstance(ret, StructT) and ret.name == RESPONSE:
            return f"    return {call}\n", ""
        if ret == UNIT:
            return f"    {call}\n    return {W}json_reply({status}, {JSON}.Null)\n", ""
        if isinstance(ret, ResultT):
            if not (isinstance(ret.err, StructT) and ret.err.name == HTTP_ERROR):
                raise fail(f"the error type of {ret} must be HttpError (from std.web), not {ret.err}")
            model = ret.ok
            if model == UNIT:
                ok_arm = f"            return {W}json_reply({status}, {JSON}.Null)\n"
                schema = ""
            else:
                self.model(model, fail, node)
                ok_arm = f"            return {W}json_reply({status}, {self.d.to_expr(model, 'zw_ok')})\n"
                schema = f", some({self.d.schema_expr(model)})"
                schema_types.append(model)
            return (f"    match {call}:\n        ok(zw_ok):\n{ok_arm}        err(zw_err):\n"
                    f"            return {W}error_reply(zw_err.status, zw_err.detail)\n"), schema
        self.model(ret, fail, node)
        schema_types.append(ret)
        return (f"    zw_out = {call}\n    return {W}json_reply({status}, {self.d.to_expr(ret, 'zw_out')})\n",
                f", some({self.d.schema_expr(ret)})")

    def model(self, ty: Type, fail, node: ast.Node) -> None:
        """Reject a response model that has no JSON form."""
        self.d.validate("endpoint", ty, "the response model", str(ty), set(), node)


J_PARSE = "std·json·parse"


class Slot:
    """One query or path parameter of the operation, shared by every function of the endpoint that reads that name.

    Attributes:
        inner: Its type without the optional.
        required: Whether any reader requires it.
        owner: The first function that declared it.
        schema: The `some(schema)` expression of an enum parameter, or "".
    """

    def __init__(self, inner: Type, required: bool, owner: str, schema: str = "") -> None:
        """Create the slot."""
        self.inner, self.required, self.owner, self.schema = inner, required, owner, schema


class Inputs:
    """What the handler reads from the request: the statements, the merged parameters and the body.

    Attributes:
        body: The statements parsing and validating every input, in order.
        params: The scalar parameters by name (the operation's `params`).
        request_body: The `Optional[Json]` expression of the body schema, or "".
        body_owner: The function that reads the JSON body, or "".
        has_input: Whether anything is read from the request (so a 422 is possible).
        n: A counter that gives every parameter its own variable names.
        defaults: The default values to substitute into the generated function.
        schema_types: The types whose schemas the endpoint needs.
    """

    def __init__(self) -> None:
        """Create an empty plan."""
        self.body = ""
        self.params: dict[str, Slot] = {}
        self.request_body = ""
        self.body_owner = ""
        self.has_input = False
        self.n = -1
        self.defaults: dict = {}
        self.schema_types: list[Type] = []

    def param_expr(self, name: str) -> str:
        """The `Param(...)` expression of one merged parameter."""
        slot = self.params[name]
        text = f'{W}Param({lit(name)}, "query", "", {"true" if slot.required else "false"}, {lit(SCHEMA_TYPES.get(slot.inner, "string"))}'
        if slot.schema:
            text += f", {slot.schema}"
        return text + ")"


class Dependencies:
    """The `depends(g)` functions of one endpoint, resolved at compile time.

    The handler calls each distinct function once, dependencies before their dependents, so a function used by two
    parameters (or by two other dependencies) runs once per request and every user sees the same value.

    Attributes:
        order: The functions to call, in call order.
        defs: Their definitions.
    """

    def __init__(self, resolve, fail, read) -> None:
        """Create the resolver.

        Args:
            resolve: Maps a function name to its definition and type, or None when it is not a plain top-level function.
            fail: Builds the error to raise (`fail(message, node)`).
            read: Plans the reading of a request parameter of a dependency: `read(param, type, function)` gives the argument.
        """
        self.resolve, self.fail, self.read = resolve, fail, read
        self.order: list[str] = []
        self.defs: dict[str, ast.FnDef] = {}
        self.vars: dict[str, str] = {}
        self.rets: dict[str, Type] = {}
        self.args: dict[str, list[str]] = {}
        self.unwrap: set[str] = set()

    def inject(self, g: str, want: Type, at: ast.Node, stack: list[str]) -> str:
        """The expression that gives a parameter of type `want` the value of dependency `g`.

        The value is `g`'s result itself, or its `ok` value when `g` returns `Result[T, HttpError]` and the parameter is
        a `T` (an `err` then answers the request).

        Raises:
            ZyloError: If `g` cannot be a dependency, or its result does not fit the parameter.
        """
        var = self.add(g, at, stack)
        ret = self.rets[g]
        if want == ret:
            return var
        if is_http_result(ret) and want == ret.ok:
            self.unwrap.add(g)
            return f"{var}.unwrap()"
        shown = g.rsplit("·", 1)[-1]
        raise self.fail(f"{shown} returns {ret}, which does not fit {want}"
                        + (f"; declare it {ret.ok} or {ret}" if is_http_result(ret) else ""), at)

    def add(self, g: str, at: ast.Node, stack: list[str]) -> str:
        """Plan the call of `g` (and of what it depends on) and return the variable that holds its result."""
        if g in self.vars:
            return self.vars[g]
        shown = g.rsplit("·", 1)[-1]
        if g in stack:
            chain = " -> ".join(n.rsplit("·", 1)[-1] for n in [*stack[stack.index(g):], g])
            raise self.fail(f"dependency cycle: {chain}", at)
        found = self.resolve(g)
        if found is None:
            raise self.fail(f"depends({shown}): {shown!r} must be a plain top-level function (not generic, not an `extern fn`)", at)
        gdef, gsig = found
        if gsig.ret == UNIT:
            raise self.fail(f"depends({shown}): the function returns nothing, so there is nothing to inject", at)
        args: list[str] = []
        for gp, gt in zip(gdef.params, gsig.params):
            if isinstance(gt, StructT) and gt.name == REQUEST:
                args.append("zw_req")
            elif ast.is_depends(gp.default):
                args.append(self.inject(gp.default.args[0].id, gt, gp, [*stack, g]))
            else:
                args.append(self.read(gp, gt, shown))
        var = f"zw_d{len(self.order)}"
        self.vars[g], self.rets[g], self.defs[g], self.args[g] = var, gsig.ret, gdef, args
        self.order.append(g)
        return var

    def source(self) -> str:
        """The statements that call every dependency, answering the request when one returns an `err` that is unwrapped."""
        out = ""
        for g in self.order:
            var = self.vars[g]
            out += f"    {var} = {g}({', '.join(self.args[g])})\n"
            if g in self.unwrap:
                out += (f"    match {var}:\n        err(zw_de{var[4:]}):\n"
                        f"            return {W}error_reply(zw_de{var[4:]}.status, zw_de{var[4:]}.detail)\n"
                        f"        ok(zw_dv{var[4:]}):\n            pass\n")
        return out


def is_http_result(t: Type) -> bool:
    """Whether a type is `Result[T, HttpError]`."""
    return isinstance(t, ResultT) and isinstance(t.err, StructT) and t.err.name == HTTP_ERROR


def literal_statuses(defn: ast.FnDef) -> list[int]:
    """The statuses of the `HttpError(404, ...)` constructors written in a function, when the status is a number literal."""
    found: list[int] = []
    for n in ast.walk(defn):
        if isinstance(n, ast.Call) and n.func == HTTP_ERROR:
            status = n.args[0] if n.args else next((a.value for a in n.named if a.name == "status"), None)
            if isinstance(status, ast.Int) and 100 <= status.value <= 599:
                found.append(status.value)
    return found


def status_phrase(code: int) -> str:
    """The standard reason phrase of a status code (`Not Found`), or `Error` for an unknown one."""
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Error"


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
