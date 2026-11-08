"""LIRA text input format: split into numbered documents, read and replace loads.

A text model is a sequence of documents ``( N/ ... )``, each starting a line. Only the
documents the server changes are parsed; all others are kept byte for byte.

- document 6: one load per ``/``-separated entry: ``target type direction row case``;
  ``type`` 0 is a nodal load (target is a node), other types load an element.
- document 7: load values: ``row value value ... /``; document 6 refers to the row.
"""

import re
from dataclasses import dataclass, field

from lira_sapr_mcp.errors import LiraInputError

_DOC_START = re.compile(r"(?m)^[ \t﻿]*\(\s*(\d+)\s*/")

# Load types of document 6 that load a node: 0 forces and moments, 60 (LIRA keeps it on a
# number that only a node has). See LOAD_KINDS in services/loads.py and the research page.
NODAL_LOAD_TYPES = frozenset({0, 60})


@dataclass
class Load:
    case: int
    target: int
    type: int
    direction: int
    values: list[float] = field(default_factory=list)

    @property
    def on_node(self) -> bool:
        return self.type in NODAL_LOAD_TYPES


@dataclass
class TextModel:
    """A text model as an ordered list of ``(number, body)`` documents."""

    docs: list[tuple[int, str]]
    preamble: str = ""

    @classmethod
    def parse(cls, text: str) -> "TextModel":
        starts = list(_DOC_START.finditer(text))
        if not starts:
            raise LiraInputError("Not a LIRA text model: no '( N/' documents found")
        docs = []
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
            chunk = text[m.end() : end]
            close = chunk.rfind(")")
            if close < 0:
                raise LiraInputError(f"Document {m.group(1)} is not closed with ')'")
            docs.append((int(m.group(1)), chunk[:close]))
        return cls(docs, text[: starts[0].start()].lstrip("﻿"))

    def body(self, number: int) -> str | None:
        return next((b for n, b in self.docs if n == number), None)

    def set_body(self, number: int, body: str | None) -> None:
        """Replace a document; ``None`` removes it; a new one goes before document 8+."""
        for i, (n, _) in enumerate(self.docs):
            if n == number:
                if body is None:
                    del self.docs[i]
                else:
                    self.docs[i] = (n, body)
                return
        if body is None:
            return
        pos = next((i for i, (n, _) in enumerate(self.docs) if n > number), len(self.docs))
        self.docs.insert(pos, (number, body))

    def render(self) -> str:
        parts = [self.preamble]
        for n, body in self.docs:
            parts.append(f"( {n}/{body})\n")
        return "".join(parts)

    # --- loads ----------------------------------------------------------------------

    def loads(self) -> list[Load]:
        values = _parse_values(self.body(7) or "")
        loads = []
        for entry in entries(self.body(6) or ""):
            nums = _numbers(entry, 6)
            if len(nums) != 5:
                raise LiraInputError(
                    f"Unsupported load entry in document 6: {entry.strip()!r} "
                    "(expected: target type direction row case)"
                )
            target, typ, direction, row, case = (int(x) for x in nums)
            if row not in values:
                raise LiraInputError(f"Document 6 refers to missing row {row} of document 7")
            loads.append(Load(case, target, typ, direction, values[row]))
        return loads

    def set_loads(self, loads: list[Load]) -> None:
        """Replace all loads; equal value rows are shared in document 7."""
        rows: dict[tuple[float, ...], int] = {}
        doc6, doc7 = [], []
        for load in sorted(loads, key=lambda ld: (ld.case, ld.on_node is False, ld.target)):
            key = tuple(float(v) for v in load.values)
            if key not in rows:
                rows[key] = len(rows) + 1
                doc7.append(f"{rows[key]} {' '.join(_fmt(v) for v in key)} /")
            doc6.append(f"{load.target} {load.type} {load.direction} {rows[key]} {load.case} /")
        self.set_body(6, ("\n" + "\n".join(doc6) + "\n") if doc6 else None)
        self.set_body(7, ("\n" + "\n".join(doc7) + "\n") if doc7 else None)


def entries(body: str) -> list[str]:
    """The entries of a document body, which ``/`` ends."""
    return [e for e in body.split("/") if e.strip()]


def _numbers(entry: str, doc: int) -> list[float]:
    out = []
    for tok in entry.split():
        try:
            out.append(float(tok))
        except ValueError:
            raise LiraInputError(
                f"Unsupported syntax in document {doc}: {entry.strip()!r}"
            ) from None
    return out


def _parse_values(body: str) -> dict[int, list[float]]:
    values = {}
    for entry in entries(body):
        nums = _numbers(entry, 7)
        if nums:
            values[int(nums[0])] = nums[1:]
    return values


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))
