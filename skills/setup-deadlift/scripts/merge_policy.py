#!/usr/bin/env -S python3 -I
"""Merges tailnet policy entries into a tailnet policy, keeping the policy's own text.

Usage:
  python3 merge_policy.py <policy.hujson> <entries.hujson>...

<policy.hujson> is the tailnet policy as it is now, in HuJSON (JSON with comments and trailing
commas), as the Tailscale API or the admin console's editor gives it. Each <entries.hujson> is
one HuJSON object of policy entries to merge in, such as the ones `deadlift server provision` or
`deadlift app add` prints. The merged policy goes to stdout; the policy file isn't changed.

How entries merge, section by section and level by level:
  - an object's key the policy lacks is added, with its comments;
  - a key it has merges in turn, when both values are objects or both are arrays;
  - an array's element the policy lacks (by meaning, whatever its layout) is appended;
  - an element or key the policy has already is left as it is.
Each addition goes after the last of its kind, indented as the policy indents, with the
policy's own comma style. Section names match whatever their case, as Tailscale reads them.
Everything else in the policy, comments, layout and order, is kept byte for byte.

Every reason line starts with `deadlift: `, on stderr. Exit codes: 0 merged (or nothing to
merge), 2 refused (a file isn't HuJSON, or an entry contradicts the policy): nothing is printed
on stdout then.
"""

import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REFUSED = 2


class Refused(Exception):
    """The merge can't go ahead; the message is the reason."""


# A HuJSON value, parsed, with where its text is.


@dataclass
class Node:
    start: int
    end: int


@dataclass
class Scalar(Node):
    value: object


@dataclass
class Item:
    """An object's member or an array's element: its text runs from ``lead`` (its leading
    comments, or itself) to ``node.end``; ``comma`` is just after the comma that follows it,
    when one does."""

    lead: int
    node: Node
    key: str = ""
    comma: int | None = None


@dataclass
class Container(Node):
    """An object (``{``) or an array (``[``). ``start`` is its opening bracket, ``end`` just
    after its closing one."""

    items: list[Item] = field(default_factory=list[Item])

    @property
    def is_object(self) -> bool:
        return isinstance(self, Object)


@dataclass
class Object(Container):
    pass


@dataclass
class Array(Container):
    pass


_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_LITERALS = {"true": True, "false": False, "null": None}


class Parser:
    def __init__(self, text: str, name: str) -> None:
        self.text = text
        self.name = name
        self.pos = 0

    def error(self, why: str, at: int | None = None) -> Refused:
        at = self.pos if at is None else at
        line = self.text.count("\n", 0, at) + 1
        column = at - (self.text.rfind("\n", 0, at) + 1) + 1
        return Refused(f"{self.name} isn't HuJSON: {why} at line {line}, column {column}")

    def skip(self) -> None:
        """Skips whitespace and comments."""
        text = self.text
        while self.pos < len(text):
            char = text[self.pos]
            if char in " \t\r\n":
                self.pos += 1
            elif text.startswith("//", self.pos):
                newline = text.find("\n", self.pos)
                self.pos = len(text) if newline < 0 else newline
            elif text.startswith("/*", self.pos):
                close = text.find("*/", self.pos + 2)
                if close < 0:
                    raise self.error("a /* comment with no */")
                self.pos = close + 2
            else:
                return

    def document(self) -> Node:
        self.skip()
        node = self.value()
        self.skip()
        if self.pos != len(self.text):
            raise self.error("more after the end of the value")
        return node

    def value(self) -> Node:
        text = self.text
        if self.pos >= len(text):
            raise self.error("the text ends where a value should be")
        char = text[self.pos]
        if char == "{":
            return self.container(Object(self.pos, self.pos), "}")
        if char == "[":
            return self.container(Array(self.pos, self.pos), "]")
        if char == '"':
            start = self.pos
            return Scalar(start, self.string_end(), json.loads(text[start : self.pos]))
        for word, value in _LITERALS.items():
            if text.startswith(word, self.pos):
                start = self.pos
                self.pos += len(word)
                return Scalar(start, self.pos, value)
        number = _NUMBER.match(text, self.pos)
        if number:
            self.pos = number.end()
            return Scalar(number.start(), number.end(), json.loads(number.group()))
        raise self.error(f"unexpected {char!r}")

    def string_end(self) -> int:
        text = self.text
        pos = self.pos + 1
        while pos < len(text):
            char = text[pos]
            if char == "\\":
                pos += 2
            elif char == '"':
                self.pos = pos + 1
                return self.pos
            elif char == "\n":
                break
            else:
                pos += 1
        raise self.error("a string with no closing quote")

    def container(self, node: Container, close: str) -> Container:
        self.pos += 1
        while True:
            lead = self.pos
            self.skip()
            if self.pos >= len(self.text):
                raise self.error(f"no closing {close}", node.start)
            if self.text[self.pos] == close:
                self.pos += 1
                node.end = self.pos
                return node
            if node.items and node.items[-1].comma is None:
                raise self.error(f"a missing comma or {close}")
            lead = self.lead(lead)
            key: str = ""
            if isinstance(node, Object):
                if self.text[self.pos] != '"':
                    raise self.error("a key that isn't a string")
                start = self.pos
                key = json.loads(self.text[start : self.string_end()])
                self.skip()
                if not self.text.startswith(":", self.pos):
                    raise self.error("a missing :")
                self.pos += 1
                self.skip()
            item = Item(lead, self.value(), key)
            node.items.append(item)
            after = self.pos
            self.skip()
            if self.text.startswith(",", self.pos):
                self.pos += 1
                item.comma = self.pos
            else:
                self.pos = after

    def lead(self, after: int) -> int:
        """Where an item's own text starts, given where the previous item's (or the bracket)
        ended: its first comment or token on a line of its own. A comment on the line of the
        comma before belongs to the item before."""
        text = self.text
        pos = after
        newline = text.find("\n", pos, self.pos)
        if newline >= 0:
            pos = newline + 1
        while pos < self.pos and text[pos] in " \t\r\n":
            pos += 1
        return pos


def parse(text: str, name: str) -> Node:
    return Parser(text, name).document()


def meaning(node: Node) -> object:
    """The node as plain JSON data, for comparing entries with what the policy has."""
    if isinstance(node, Object):
        return {item.key: meaning(item.node) for item in node.items}
    if isinstance(node, Array):
        return [meaning(item.node) for item in node.items]
    return node.value if isinstance(node, Scalar) else None


def compact(node: Node) -> str:
    return json.dumps(meaning(node), separators=(",", ":"))


# Indentation.


def indent_unit(text: str) -> str:
    """How the text indents one level: a tab, or its smallest run of leading spaces."""
    smallest = 0
    for line in text.split("\n"):
        stripped = line.lstrip(" \t")
        if not stripped or len(stripped) == len(line):
            continue
        if line.startswith("\t"):
            return "\t"
        spaces = len(line) - len(line.lstrip(" "))
        if smallest == 0 or spaces < smallest:
            smallest = spaces
    return " " * smallest if smallest else "\t"


def line_indent(text: str, pos: int) -> str:
    """The leading whitespace of the line ``pos`` is on."""
    start = text.rfind("\n", 0, pos) + 1
    end = start
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[start:end]


def levels(whitespace: str, unit: str) -> tuple[int, str]:
    """Leading whitespace as levels of ``unit``, and any spaces left over."""
    if unit == "\t":
        tabs = len(whitespace) - len(whitespace.lstrip("\t"))
        return tabs, whitespace[tabs:]
    width = len(unit)
    spaces = whitespace.replace("\t", unit)
    return len(spaces) // width, " " * (len(spaces) % width)


@dataclass(frozen=True)
class Source:
    """A parsed HuJSON file."""

    text: str
    root: "Object"
    unit: str


def item_text(source: Source, item: Item, indent: str, unit: str) -> str:
    """The item's text, comments and all, moved to ``indent`` and indented by ``unit``."""
    text = source.text[item.lead : item.node.end]
    base, _ = levels(line_indent(source.text, item.lead), source.unit)
    lines = text.split("\n")
    moved = [indent + lines[0]]
    for line in lines[1:]:
        stripped = line.lstrip(" \t")
        level, extra = levels(line[: len(line) - len(stripped)], source.unit)
        moved.append(indent + unit * max(level - base, 0) + extra + stripped if stripped else "")
    return "\n".join(moved)


# The merge.


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    text: str


@dataclass
class Merge:
    policy: Source
    trailing_commas: bool
    edits: list[Edit] = field(default_factory=list[Edit])
    said: list[str] = field(default_factory=list[str])

    def into(self, target: Container, entries: Source, given: Container, path: str) -> None:
        additions: list[Item] = []
        for item in given.items:
            if isinstance(target, Object):
                here = f"{path}.{item.key}" if path else item.key
                found = find_key(target, item.key)
                if found is None:
                    additions.append(item)
                    self.said.append(f"added {here}: {compact(item.node)}")
                    continue
                self.matching(found.node, item.node, entries, here)
            else:
                wanted = meaning(item.node)
                if any(meaning(have.node) == wanted for have in target.items):
                    continue
                if any(meaning(added.node) == wanted for added in additions):
                    continue
                additions.append(item)
                self.said.append(f"added to {path}: {compact(item.node)}")
        if additions:
            self.add(target, entries, additions, root=not path)

    def matching(self, have: Node, given: Node, entries: Source, path: str) -> None:
        if (
            isinstance(have, Container)
            and isinstance(given, Container)
            and have.is_object == given.is_object
        ):
            self.into(have, entries, given, path)
        elif meaning(have) != meaning(given):
            raise Refused(
                f"the policy's {path} is {compact(have)}, and the entries' is {compact(given)}."
                " Merge that one by hand"
            )

    def add(self, target: Container, entries: Source, additions: list[Item], *, root: bool) -> None:
        text = self.policy.text
        unit = self.policy.unit
        inline = "\n" not in text[target.start : target.end]
        if target.items:
            last = target.items[-1]
            trailing = last.comma is not None
            indent = line_indent(text, last.lead)
        else:
            trailing = self.trailing_commas
            indent = line_indent(text, target.start) + unit
        texts = [item_text(entries, item, indent, unit) for item in additions]
        if inline and all("\n" not in t for t in texts):
            self.add_inline(target, [t.removeprefix(indent) for t in texts])
            return
        # The policy's sections, at its top level, are set off by blank lines.
        blank = "\n" if root else ""
        body = f",\n{blank}".join(texts) + ("," if trailing else "")
        if not target.items:
            blank = ""
        if not target.items:
            close = target.end - 1
            if inline:
                closing = line_indent(text, target.start)
                self.edits.append(Edit(target.start + 1, close, f"\n{body}\n{closing}"))
            else:
                line_start = text.rfind("\n", 0, close) + 1
                self.edits.append(Edit(line_start, line_start, f"{body}\n"))
            return
        last = target.items[-1]
        if last.comma is None:
            self.edits.append(Edit(last.node.end, last.node.end, ","))
            after = last.node.end
        else:
            after = last.comma
        newline = text.find("\n", after, target.end)
        at = newline if newline >= 0 and _only_comment(text[after:newline]) else after
        self.edits.append(Edit(at, at, f"\n{blank}{body}"))

    def add_inline(self, target: Container, texts: list[str]) -> None:
        if not target.items:
            self.edits.append(Edit(target.start + 1, target.end - 1, ", ".join(texts)))
            return
        last = target.items[-1]
        if last.comma is None:
            at = last.node.end
            self.edits.append(Edit(at, at, "".join(f", {t}" for t in texts)))
        else:
            at = last.comma
            self.edits.append(Edit(at, at, "".join(f" {t}," for t in texts)))


def _only_comment(rest: str) -> bool:
    stripped = rest.strip(" \t\r")
    return not stripped or stripped.startswith("//")


def find_key(target: Object, key: str) -> Item | None:
    for item in target.items:
        if item.key == key:
            return item
    folded = key.casefold()
    for item in target.items:
        if item.key.casefold() == folded:
            return item
    return None


def uses_trailing_commas(root: Node, text: str) -> bool:
    """Whether the policy ends its multi-line objects and arrays with a comma, as Tailscale's
    own policies do: yes unless it has some and none do."""
    seen = False
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if not isinstance(node, Container):
            continue
        stack.extend(item.node for item in node.items)
        if node.items and "\n" in text[node.start : node.end]:
            if node.items[-1].comma is not None:
                return True
            seen = True
    return not seen


def read(path: str) -> Source:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise Refused(f"couldn't read {path}: {error}") from error
    return source(text, path)


def source(text: str, name: str) -> Source:
    root = parse(text, name)
    if not isinstance(root, Object):
        raise Refused(f"{name} isn't a HuJSON object")
    return Source(text, root, indent_unit(text))


def merged(policy_path: str, entries_paths: Sequence[str]) -> tuple[str, list[str]]:
    """The merged policy's text, and what was added."""
    policy = read(policy_path)
    text = policy.text
    said: list[str] = []
    for entries_path in entries_paths:
        entries = read(entries_path)
        work = Merge(policy, uses_trailing_commas(policy.root, policy.text))
        work.into(policy.root, entries, entries.root, "")
        # From the end back, so each edit's place is still where it was found. Two at one
        # place go in the order they were made.
        edits = sorted(enumerate(work.edits), key=lambda e: (e[1].start, e[0]), reverse=True)
        for _, edit in edits:
            text = text[: edit.start] + edit.text + text[edit.end :]
        said += work.said
        # The next entries merge into this result.
        policy = source(text, policy_path)
    return text, said


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write(
            "deadlift: merge_policy.py needs the policy and at least one file of entries to merge"
            " into it. Nothing was merged.\n"
            "Usage: python3 merge_policy.py <policy.hujson> <entries.hujson>...\n"
        )
        return REFUSED
    try:
        text, said = merged(argv[0], argv[1:])
    except Refused as refused:
        sys.stderr.write(f"deadlift: {refused}. Nothing was merged.\n")
        return REFUSED
    for line in said:
        sys.stderr.write(f"deadlift: {line}\n")
    if not said:
        sys.stderr.write("deadlift: the policy has every entry already, so nothing was added.\n")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
