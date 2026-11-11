import pytest

from zylo.domain import ast
from zylo.domain.errors import ZyloError
from zylo.domain.types import FLOAT, INT, STR, UNIT, ListT, StructT
from zylo.semantics.checker import check
from zylo.syntax.parser import parse

POINT = "struct Point:\n    x: float\n    y: float\n"


def checked(src: str) -> ast.Program:
    return check(parse(src))


def fails(src: str, match: str, line: int | None = None) -> ZyloError:
    with pytest.raises(ZyloError, match=match) as e:
        checked(src)
    if line is not None:
        assert e.value.line == line
    return e.value


def method(prog: ast.Program, struct: int = 0, idx: int = 0) -> ast.FnDef:
    return [s for s in prog.body if isinstance(s, ast.StructDef)][struct].methods[idx]


# ---- parser ----------------------------------------------------------------------------

def test_parse_struct_fields_and_methods():
    s = parse(POINT + "\n    fn norm(self) -> float:\n        return self.x\n").body[0]
    assert isinstance(s, ast.StructDef) and s.name == "Point"
    assert [(f.name, f.type.name) for f in s.fields] == [("x", "float"), ("y", "float")]
    m = s.methods[0]
    assert m.has_self and m.params == [] and m.ret.name == "float"


def test_parse_method_with_self_and_params():
    s = parse("struct A:\n    n: int\n\n    fn add(self, k: int, j: int):\n        self.n += k\n").body[0]
    m = s.methods[0]
    assert m.has_self and [p.name for p in m.params] == ["k", "j"]


def test_parse_attr_and_method_call_and_chains():
    e = parse("a.b.c\n").body[0].expr
    assert isinstance(e, ast.Attr) and e.name == "c" and e.value.name == "b"
    e = parse("p.dist(1, 2)\n").body[0].expr
    assert isinstance(e, ast.MethodCall) and e.name == "dist" and len(e.args) == 2
    e = parse("xs[0].x\n").body[0].expr
    assert isinstance(e, ast.Attr) and isinstance(e.value, ast.Index)
    e = parse("a.b().c.d()\n").body[0].expr
    assert isinstance(e, ast.MethodCall) and isinstance(e.value, ast.Attr)


def test_parse_attr_assign_and_augmented():
    s = parse("p.x = 1\n").body[0]
    assert isinstance(s, ast.Assign) and isinstance(s.target, ast.Attr)
    s = parse("p.x += 2\n").body[0]
    assert isinstance(s.target, ast.Attr) and s.value.op == "+"


def test_parse_float_literal_not_confused_with_attr():
    assert parse("x = 1.5\n").body[0].value.value == 1.5


def test_struct_body_rejects_statements():
    with pytest.raises(ZyloError, match="can only contain fields"):
        parse("struct A:\n    x: int\n    print(1)\n")


def test_struct_empty_body_is_error():
    with pytest.raises(ZyloError, match="an indented block"):
        parse("struct A:\nx = 1\n")


def test_self_outside_struct_is_error():
    with pytest.raises(ZyloError, match="can only be the first parameter"):
        parse("fn f(self):\n    return\n")


def test_node_position_of_attr_is_the_field_name():
    e = parse("print(p.zzz)\n").body[0].expr.args[0]
    assert (e.line, e.col) == (1, 9)


# ---- tipos ----------------------------------------------------------------------------------

def test_field_and_constructor_types():
    p = checked(POINT + "a = Point(1.0, 2.0)\nb = a.x\n")
    assert p.body[1].value.ty == StructT("Point")
    assert p.body[2].value.ty == FLOAT
    assert p.body[0].fields[0].ty == FLOAT


def test_struct_with_list_and_nested_struct_fields():
    p = checked(POINT + "struct Poly:\n    pts: list[Point]\n    origin: Point\n"
                "q = Poly([], Point(0.0, 0.0))\nn = len(q.pts)\n")
    assert p.body[1].fields[0].ty == ListT(StructT("Point"))
    assert p.body[-1].value.ty == INT


def test_forward_reference_between_structs():
    checked("struct A:\n    b: B\nstruct B:\n    n: int\nx = A(B(1))\nprint(x.b.n)\n")


def test_struct_type_in_function_signature():
    p = checked(POINT + "fn norm2(p: Point) -> float:\n    return p.x * p.x + p.y * p.y\nprint(norm2(Point(3.0, 4.0)))\n")
    assert p.body[1].ty.params == (StructT("Point"),)


def test_equality_of_structs_ok_ordering_not():
    checked(POINT + "a = Point(1.0, 2.0) == Point(1.0, 2.0)\n")
    fails(POINT + "a = Point(1.0, 2.0) < Point(1.0, 2.0)\n", "is not defined for Point")


def test_method_return_type_and_args():
    p = checked("struct A:\n    n: int\n\n    fn plus(self, k: int) -> int:\n        return self.n + k\nx = A(1).plus(2)\n")
    assert p.body[1].value.ty == INT


def test_chained_nested_attr_and_method():
    checked("struct B:\n    n: int\n\n    fn get(self) -> int:\n        return self.n\n"
            "struct A:\n    b: B\nprint(A(B(1)).b.get())\n")


# ---- field and constructor errors --------------------------------------------------------------

def test_unknown_field_lists_available_ones():
    e = fails(POINT + "p = Point(1.0, 2.0)\nprint(p.z)\n", "has no field 'z'", line=5)
    assert "x, y" in e.msg


def test_field_access_on_non_struct():
    fails("x = 1\nprint(x.y)\n", "int has no fields")


def test_method_used_as_field_and_field_as_method():
    fails("struct A:\n    n: int\n\n    fn m(self):\n        return\na = A(1)\nprint(a.m)\n", "is a method")
    fails("struct A:\n    n: int\na = A(1)\na.n()\n", "is a field")


def test_unknown_method():
    fails(POINT + "p = Point(1.0, 2.0)\np.fly()\n", "has no method 'fly'")


def test_method_call_on_non_struct():
    fails("x = 1\nx.foo()\n", "int has no methods")


def test_constructor_arity_and_types():
    fails(POINT + "p = Point(1.0)\n", "Point takes 2 argument", line=4)
    e = fails(POINT + "p = Point(1.0, 2)\n", "argument 2 of Point: expected float, got int")
    assert e.line == 4


def test_method_arity_and_types():
    src = "struct A:\n    n: int\n\n    fn add(self, k: int):\n        self.n += k\na = A(1)\n"
    fails(src + "a.add()\n", "A.add takes 1 argument")
    fails(src + "a.add(\"x\")\n", "argument 1 of A.add")


def test_struct_name_used_as_value():
    fails(POINT + "p = Point\n", "is a struct")


def test_unknown_type_in_field_param_and_annotation():
    e = fails("struct A:\n    n: banana\n", "unknown type 'banana'", line=2)
    assert e.col == 8
    fails("fn f(a: Nope):\n    return\n", "unknown type 'Nope'")
    fails("x: Nope = 1\n", "unknown type 'Nope'")


def test_field_assignment_type_checked():
    fails(POINT + "p = Point(1.0, 2.0)\np.x = 1\n", "field 'x' is float but int is assigned", line=5)
    fails("x = 1\nx.y = 2\n", "int has no fields")


# ---- definition errors ------------------------------------------------------------------------------

def test_duplicate_struct_field_method():
    fails(POINT + POINT, "is already defined", line=4)
    fails("struct A:\n    x: int\n    x: int\n", "duplicate field 'x'", line=3)
    fails("struct A:\n    n: int\n\n    fn m(self):\n        return\n\n    fn m(self):\n        return\n", "method 'm' is already defined")
    fails("struct A:\n    m: int\n\n    fn m(self):\n        return\n", "both a field and a method")


def test_empty_struct_and_method_without_self():
    fails("struct A:\n    fn m(self):\n        return\n", "at least one field")
    fails("struct A:\n    n: int\n\n    fn make() -> int:\n        return 1\n", "must take `self`")


def test_reserved_and_clashing_names():
    fails("struct int:\n    n: int\n", "reserved name")
    fails("struct print:\n    n: int\n", "reserved name")
    fails("struct A:\n    n: int\nfn A():\n    return\n", "is already a struct")


def test_recursive_struct_rejected_but_list_allowed():
    fails("struct Node:\n    next: Node\n", "contains itself", line=1)
    fails("struct A:\n    b: B\nstruct B:\n    a: A\n", "contains itself")
    checked("struct Node:\n    children: list[Node]\n    n: int\n")


def test_struct_only_at_top_level():
    fails("if true:\n    struct A:\n        n: int\n", "top level", line=2)


def test_self_cannot_be_reassigned_or_declared():
    fails("struct A:\n    n: int\n\n    fn m(self):\n        self = A(1)\n", "cannot reassign `self`", line=5)
    fails("self = 1\n", "cannot be declared")
    fails("for self in [1]:\n    print(self)\n", "loop variable")


def test_self_is_undefined_outside_methods():
    fails("fn f() -> int:\n    return self.n\n", "undefined variable 'self'")


# ---- inference of self mutability ------------------------------------------------------------------------

def test_direct_mutation_marks_self_mut():
    p = checked("struct A:\n    n: int\n\n    fn inc(self):\n        self.n += 1\n\n    fn get(self) -> int:\n        return self.n\n")
    assert method(p, 0, 0).self_mut is True
    assert method(p, 0, 1).self_mut is False


def test_list_field_mutation_marks_self_mut():
    src = ("struct A:\n    xs: list[int]\n\n    fn add(self, v: int):\n        append(self.xs, v)\n\n"
           "    fn set0(self, v: int):\n        self.xs[0] = v\n\n    fn first(self) -> int:\n        return self.xs[0]\n")
    p = checked(src)
    assert [method(p, 0, i).self_mut for i in range(3)] == [True, True, False]


def test_mutation_propagates_through_method_calls_in_any_order():
    # `twice` is defined BEFORE `inc`, and `quad` depends on `twice`: needs the fixed point
    src = ("struct A:\n    n: int\n\n"
           "    fn quad(self):\n        self.twice()\n        self.twice()\n\n"
           "    fn twice(self):\n        self.inc()\n        self.inc()\n\n"
           "    fn inc(self):\n        self.n += 1\n\n"
           "    fn peek(self) -> int:\n        return self.n\n")
    p = checked(src)
    assert [method(p, 0, i).self_mut for i in range(4)] == [True, True, True, False]


def test_receiver_variable_is_mutable_only_for_mutating_methods():
    src = "struct A:\n    n: int\n\n    fn inc(self):\n        self.n += 1\n\n    fn get(self) -> int:\n        return self.n\n"
    p = checked(src + "a = A(1)\nb = A(2)\na.inc()\nprint(b.get())\n")
    a, b = p.body[1], p.body[2]
    assert a.var.mutable is True and b.var.mutable is False


def test_mutating_method_through_index_and_field_marks_root():
    src = ("struct A:\n    n: int\n\n    fn inc(self):\n        self.n += 1\n"
           "struct B:\n    a: A\n")
    p = checked(src + "xs = [A(1)]\nxs[0].inc()\nb = B(A(1))\nb.a.inc()\n")
    assert p.body[2].var.mutable is True   # xs
    assert p.body[4].var.mutable is True   # b


def test_field_assignment_marks_variable_and_param_mutable():
    p = checked(POINT + "p = Point(1.0, 2.0)\np.x = 5.0\nfn f(q: Point):\n    q.y = 1.0\n")
    assert p.body[1].var.mutable is True
    assert p.body[3].params[0].info.mutable is True


def test_loop_variable_field_assignment_is_allowed_on_the_copy():
    p = checked(POINT + "ps = [Point(1.0, 2.0)]\nfor q in ps:\n    q.x = 9.0\n")
    assert p.body[2].info.mutable is True


def test_unit_method_result_cannot_be_assigned():
    fails("struct A:\n    n: int\n\n    fn m(self):\n        return\nx = A(1).m()\n", "produces no value")


def test_fn_signature_types_for_methods():
    p = checked("struct A:\n    n: int\n\n    fn m(self, s: str) -> str:\n        return s\n")
    sig = method(p).ty
    assert sig.params == (STR,) and sig.ret == STR
    assert method(checked("struct A:\n    n: int\n\n    fn m(self):\n        return\n")).ty.ret == UNIT
