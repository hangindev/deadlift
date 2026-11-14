"""YAML as GitHub reads workflows and actions: by YAML 1.2's core schema, where `on`, `yes` and
`off` are strings and `012` is twelve, not by PyYAML's YAML 1.1, where `on:` would be True.
https://yaml.org/spec/1.2.2/#1032-tag-resolution
"""

import re
from typing import Any

import yaml
from yaml.nodes import ScalarNode

_BOOL = "tag:yaml.org,2002:bool"
_INT = "tag:yaml.org,2002:int"
_FLOAT = "tag:yaml.org,2002:float"


class _Loader(yaml.SafeLoader):
    pass


# Every implicit resolver but YAML 1.1's booleans, integers and floats, then YAML 1.2's.
_RESOLVERS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in (_BOOL, _INT, _FLOAT)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _tag, _regexp, _first in [
    (_BOOL, r"(?:true|True|TRUE|false|False|FALSE)", "tTfF"),
    (_INT, r"(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)", "-+0123456789"),
    (
        _FLOAT,
        r"(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))",
        "-+.0123456789",
    ),
]:
    for _char in _first:
        _RESOLVERS.setdefault(_char, []).append((_tag, re.compile(f"^{_regexp}$")))
_Loader.yaml_implicit_resolvers = _RESOLVERS


def _construct_int(_loader: object, node: ScalarNode) -> int:
    text = str(node.value)
    if text.startswith("0o"):
        return int(text[2:], 8)
    if text.startswith("0x"):
        return int(text[2:], 16)
    return int(text, 10)


def _construct_float(_loader: object, node: ScalarNode) -> float:
    text = str(node.value).lower()
    if text.endswith(".inf"):
        return float(text.replace(".inf", "inf"))
    if text == ".nan":
        return float("nan")
    return float(text)


_Loader.add_constructor(_INT, _construct_int)
_Loader.add_constructor(_FLOAT, _construct_float)


def load(text: str) -> Any:
    """Parses one YAML document."""
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader
