"""GitHub Actions' expression language, as far as the starter workflows use it.

Literals, contexts with `.`, `[…]` and `*` (object filters), `!`, `==`, `!=`, `<`, `>`, `<=`,
`>=`, `&&`, `||`, parentheses, and the functions contains, startsWith, endsWith, format, join,
toJSON, fromJSON and the status checks. Comparisons follow GitHub's rules: strings
case-insensitively, other mixed types as numbers.
https://docs.github.com/en/actions/reference/workflows-and-actions/expressions
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

type Value = bool | int | float | str | list[Value] | dict[str, Value] | None


@dataclass(frozen=True)
class Scope:
    """The contexts by name (github, env, vars, inputs, steps, needs, …), and what success(),
    failure() and cancelled() return here."""

    contexts: dict[str, Value]
    success: bool
    failure: bool
    cancelled: bool


_TOKEN = re.compile(
    r"\s*(?:('(?:[^']|'')*')|(\d+(?:\.\d+)?)|(==|!=|<=|>=|&&|\|\||[!<>().,[\]*])"
    r"|([A-Za-z_][A-Za-z0-9_-]*))"
)


def _tokenize(source: str) -> list[str]:
    tokens: list[str] = []
    at = 0
    while at < len(source):
        if not source[at:].strip():
            break
        m = _TOKEN.match(source, at)
        if m is None:
            raise ValueError(f'can\'t read the expression at "{source[at:]}"')
        tokens.append(next(group for group in m.groups() if group is not None))
        at = m.end()
    return tokens


def evaluate(source: str, scope: Scope) -> Value:
    """Evaluates one expression, without `${{ }}`."""
    return _Parser(source, scope).parse()


def interpolate(text: str, scope: Scope) -> str:
    """Replaces each `${{ … }}` in a string with its value, as a string."""
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: to_text(evaluate(m[1], scope)), text, flags=re.S)


def condition(text: str, scope: Scope) -> bool:
    """Evaluates an `if:`, which may be wrapped in `${{ }}` or not."""
    bare = re.sub(r"^\$\{\{(.*)\}\}$", r"\1", text.strip(), flags=re.S)
    uses_status = re.search(r"\b(success|failure|cancelled|always)\s*\(", bare) is not None
    # GitHub adds success() && to a condition that names no status function.
    return _truthy(evaluate(bare if uses_status else f"success() && ({bare})", scope))


class _Parser:
    def __init__(self, source: str, scope: Scope) -> None:
        self.source = source
        self.scope = scope
        self.tokens = _tokenize(source)
        self.at = 0

    def parse(self) -> Value:
        result = self.or_()
        if self.at < len(self.tokens):
            raise ValueError(f'unexpected {self.tokens[self.at]} in "{self.source}"')
        return _value(result)

    def peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self, expected: str | None = None) -> str | None:
        t = self.peek()
        self.at += 1
        if expected is not None and t != expected:
            raise ValueError(f'expected {expected} in "{self.source}", got {t or "the end"}')
        return t

    def or_(self) -> _Result:
        left = self.and_()
        while self.peek() == "||":
            self.take()
            right = self.and_()
            left = left if _truthy(left) else right
        return left

    def and_(self) -> _Result:
        left = self.comparison()
        while self.peek() == "&&":
            self.take()
            right = self.comparison()
            left = right if _truthy(left) else left
        return left

    def comparison(self) -> _Result:
        left = self.unary()
        while (op := self.peek()) in ("==", "!=", "<", ">", "<=", ">="):
            self.take()
            right = self.unary()
            left = _compare(op, _value(left), _value(right))
        return left

    def unary(self) -> _Result:
        if self.peek() == "!":
            self.take()
            return not _truthy(self.unary())
        return self.postfix()

    def postfix(self) -> _Result:
        result = self.primary()
        while True:
            if self.peek() == ".":
                self.take()
                name = self.take() or ""
                result = _star(result) if name == "*" else _property(result, name)
            elif self.peek() == "[":
                self.take()
                if self.peek() == "*":
                    self.take()
                    self.take("]")
                    result = _star(result)
                    continue
                index = self.or_()
                self.take("]")
                result = _property(result, to_text(_value(index)))
            else:
                return result

    def primary(self) -> _Result:
        t = self.take()
        if t is None:
            raise ValueError(f'"{self.source}" ends too soon')
        if t == "(":
            result = self.or_()
            self.take(")")
            return result
        if t.startswith("'"):
            return t[1:-1].replace("''", "'")
        if t[0].isdigit():
            return float(t) if "." in t else int(t)
        if t in ("true", "false"):
            return t == "true"
        if t == "null":
            return None
        if self.peek() == "(":
            self.take()
            args: list[Value] = []
            while self.peek() != ")":
                args.append(_value(self.or_()))
                if self.peek() == ",":
                    self.take()
            self.take(")")
            return _call(t, args, self.scope)
        for name, value in self.scope.contexts.items():
            if name.lower() == t.lower():
                return value
        raise ValueError(f'no context named {t} in "{self.source}"')


def to_text(v: Value) -> str:
    """A value as a string, as GitHub puts it in `${{ }}`: null is empty, objects are JSON."""
    if v is None:
        return ""
    if isinstance(v, list | dict):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        return repr(v)
    return str(v)


def _call(name: str, v: list[Value], scope: Scope) -> Value:
    match name.lower():
        case "success":
            return scope.success
        case "failure":
            return scope.failure
        case "cancelled":
            return scope.cancelled
        case "always":
            return True
        case "contains":
            if isinstance(v[0], list):
                return any(_equal(item, v[1]) for item in v[0])
            return to_text(v[1]).lower() in to_text(v[0]).lower()
        case "startswith":
            return to_text(v[0]).lower().startswith(to_text(v[1]).lower())
        case "endswith":
            return to_text(v[0]).lower().endswith(to_text(v[1]).lower())
        case "format":
            return re.sub(r"\{(\d+)\}", lambda m: to_text(v[int(m[1]) + 1]), to_text(v[0]))
        case "join":
            if isinstance(v[0], list):
                return (to_text(v[1]) if len(v) > 1 else ",").join(map(to_text, v[0]))
            return to_text(v[0])
        case "tojson":
            return json.dumps(v[0], ensure_ascii=False, indent=2)
        case "fromjson":
            parsed: Value = json.loads(to_text(v[0]))
            return parsed
        case _:
            raise ValueError(f"the test runner has no function {name}()")


@dataclass(frozen=True)
class _Filtered:
    """An object filter's result (`labels.*.name`), which further properties map over."""

    items: list[Value]


type _Result = Value | _Filtered


def _value(r: _Result) -> Value:
    return r.items if isinstance(r, _Filtered) else r


def _property(v: _Result, name: str) -> _Result:
    if isinstance(v, _Filtered):
        picked = (_value(_property(item, name)) for item in v.items)
        return _Filtered([item for item in picked if item is not None])
    if isinstance(v, list):
        return v[int(name)] if name.isdigit() and int(name) < len(v) else None
    if isinstance(v, dict):
        for key, item in v.items():
            if key.lower() == name.lower():
                return item
    return None


def _truthy(r: _Result) -> bool:
    v = _value(r)
    if v is None or v is False or v == "":
        return False
    if isinstance(v, int | float) and not isinstance(v, bool):
        return not (v == 0 or math.isnan(v))
    return True


_DECIMAL = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_RADIX = {"0x": 16, "0o": 8, "0b": 2}


def _to_number(v: Value) -> float:
    """As JavaScript's Number(): null 0, booleans 0 or 1, a string parsed (blank is 0), else NaN."""
    if v is None:
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int | float):
        return v
    if isinstance(v, str):
        text = v.strip()
        if text == "":
            return 0
        if _DECIMAL.fullmatch(text):
            return float(text)
        if text in ("Infinity", "+Infinity", "-Infinity"):
            return -math.inf if text[0] == "-" else math.inf
        radix = _RADIX.get(text[:2].lower())
        if radix is not None:
            try:
                return int(text[2:], radix)
            except ValueError:
                return math.nan
    return math.nan


def _equal(a: Value, b: Value) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if isinstance(a, list | dict) or isinstance(b, list | dict):
        return a is b
    return _to_number(a) == _to_number(b)


def _compare(op: str, a: Value, b: Value) -> bool:
    if op == "==":
        return _equal(a, b)
    if op == "!=":
        return not _equal(a, b)
    if isinstance(a, str) and isinstance(b, str):
        x, y = a.lower(), b.lower()
        return x < y if op == "<" else x > y if op == ">" else x <= y if op == "<=" else x >= y
    m, n = _to_number(a), _to_number(b)
    return m < n if op == "<" else m > n if op == ">" else m <= n if op == "<=" else m >= n


def _star(r: _Result) -> _Filtered:
    items = r.items if isinstance(r, _Filtered) else [r]
    picked: list[Value] = []
    for item in items:
        if isinstance(item, list):
            picked.extend(item)
        elif isinstance(item, dict):
            picked.extend(item.values())
    return _Filtered(picked)
