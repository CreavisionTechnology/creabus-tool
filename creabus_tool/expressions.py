# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""The expression language used by device profiles, and its boundary.

A profile can compute one register from others - `voltage * current * pf` -
which is what makes a simulated device internally consistent rather than
merely random. That means a profile file carries code, and profiles are meant
to be shared: copied off a forum, sent by a colleague, merged from a pull
request. So the boundary around that code has to be a real one.

WHY `eval` WITH NO BUILTINS IS NOT A BOUNDARY

Removing `__builtins__` stops a typo from reaching `open()`. It does not stop
anyone who is trying, because Python objects carry their whole type hierarchy
with them:

    ().__class__.__base__.__subclasses__()

That expression, in an environment with nothing in it at all, walks from an
empty tuple to `object` to every class the interpreter has loaded - and from
there to the ones that run commands. Every escape of this kind needs the same
first step: an attribute access.

WHAT THIS DOES INSTEAD

The expression is parsed to a syntax tree and every node is checked against an
allowlist before anything runs. Attribute access is not on it, so the walk
above is rejected at `.__class__` rather than sandboxed afterwards. Neither
are subscripts, lambdas, comprehensions, assignments or f-strings. What is
left is arithmetic, comparisons, a conditional, and calls to the named
functions below - enough to describe a meter, and not enough to describe
anything else.

This runs when a profile is loaded, so a profile that would be refused never
reaches the simulation, and the editor's Check button reports it like any
other mistake.
"""

from __future__ import annotations

import ast
import math
import random
from typing import Any

# Calls are allowed only to these, by bare name. Anything else is refused.
SAFE_FUNCTIONS: dict[str, Any] = {
    "abs": abs, "min": min, "max": max, "round": round, "int": int, "float": float,
    "pow": pow, "sum": sum, "len": len, "bool": bool,
    "pi": math.pi, "e": math.e, "inf": math.inf,
    "uniform": random.uniform, "gauss": random.gauss, "randint": random.randint,
    "choice": lambda *items: random.choice(items if len(items) > 1 else items[0]),
}
SAFE_FUNCTIONS.update(
    {name: getattr(math, name) for name in (
        "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
        "degrees", "radians", "log", "log10", "exp", "floor", "ceil", "fmod", "hypot",
    )}
)

# Names the evaluator provides on top of the registers themselves.
RUNTIME_NAMES = frozenset({"t", "now"})

# A literal exponent larger than this is refused. 10**10**8 is valid Python,
# finishes eventually, and would hang the simulation thread until it did;
# nothing describing a real device needs it.
MAX_LITERAL_EXPONENT = 1024

_ALLOWED_NODES: tuple = (
    ast.Expression,
    ast.Constant,
    ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
    ast.Tuple, ast.List,                       # choice(a, b, c) and choice([a, b])
    # operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)

# Named so the error message can say what was wrong rather than "node type 47".
_REJECTED_NODES = {
    ast.Attribute: "attribute access (a.b)",
    ast.Subscript: "indexing (a[b])",
    ast.Lambda: "lambda",
    ast.ListComp: "a comprehension",
    ast.SetComp: "a comprehension",
    ast.DictComp: "a comprehension",
    ast.GeneratorExp: "a generator",
    ast.Dict: "a dict literal",
    ast.Set: "a set literal",
    ast.Starred: "argument unpacking (*a)",
    ast.JoinedStr: "an f-string",
    ast.NamedExpr: "assignment (:=)",
    ast.Await: "await",
    ast.Yield: "yield",
}


class ExpressionError(Exception):
    """An expression is malformed, refers to nothing, or is not allowed."""


def check_expression(source: str, names: set[str] | frozenset[str]) -> ast.Expression:
    """Parse and vet an expression, or raise ExpressionError.

    `names` is every identifier the expression may read: the registers of the
    profile it belongs to, plus RUNTIME_NAMES. The safe functions are added
    here so a caller cannot forget them.

    Returns the parsed tree, so a caller that is about to compile it does not
    have to parse twice.
    """
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error: {exc.msg}") from None

    allowed = set(names) | set(SAFE_FUNCTIONS) | RUNTIME_NAMES

    for node in ast.walk(tree):
        for kind, description in _REJECTED_NODES.items():
            if isinstance(node, kind):
                raise ExpressionError(
                    f"{description} is not allowed in an expression")
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"{type(node).__name__} is not allowed in an expression")

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError("only the built-in functions may be called")
            if node.func.id not in SAFE_FUNCTIONS:
                raise ExpressionError(f"unknown function '{node.func.id}'")
            if node.keywords:
                raise ExpressionError("keyword arguments are not supported")

        if isinstance(node, ast.Name) and node.id not in allowed:
            raise ExpressionError(
                f"'{node.id}' is not a register in this profile or a known function")

        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            # Folded rather than pattern matched, because ** is right
            # associative: in 10**10**8 the exponent is the subexpression
            # 10**8, not a literal, and it is the tower that hangs the
            # simulation thread rather than any single literal.
            exponent = _fold(node.right)
            if exponent is not None and abs(exponent) > MAX_LITERAL_EXPONENT:
                raise ExpressionError(
                    f"an exponent above {MAX_LITERAL_EXPONENT} is not allowed")

    return tree


def _fold(node: ast.AST) -> float | None:
    """The value of a subtree built only from literals, or None.

    Used to see how big an exponent really is before anything runs. Anything
    involving a register or a function call is not constant, so it folds to
    None and is left alone - at runtime those are floats, and a float power
    overflows to inf quickly instead of computing for ever.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp):
        inner = _fold(node.operand)
        if inner is None:
            return None
        if isinstance(node.op, ast.USub):
            return -inner
        if isinstance(node.op, ast.UAdd):
            return inner
        return None
    if isinstance(node, ast.BinOp):
        left, right = _fold(node.left), _fold(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_LITERAL_EXPONENT:
                    raise ExpressionError(
                        f"an exponent above {MAX_LITERAL_EXPONENT} is not allowed")
                return left ** right
        except ZeroDivisionError:
            return None
        except OverflowError:
            return float("inf")
    return None


def compile_expression(source: str, names: set[str] | frozenset[str]):
    """Vet an expression and compile it, ready for eval()."""
    return compile(check_expression(source, names), "<expression>", "eval")


def evaluation_globals() -> dict[str, Any]:
    """The globals an expression is evaluated with: the functions, nothing else.

    Builtins are still stripped. The allowlist is what actually holds, but a
    second lock on a door that is already welded shut costs nothing.
    """
    return {"__builtins__": {}}
