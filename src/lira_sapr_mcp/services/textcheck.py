"""Compare the text of a rebuilt model with the text it was built from, document by
document: the input tables do not show everything a model holds (e.g. reinforcement of
physically nonlinear stiffnesses, ``NSAG`` in document 3; reinforcement types on
elements, document 32; the order of load cases in the construction history, document 16).

What LIRA changes by itself on every rebuild is not a difference:

- document 0: the model name (entry 1) and the name of its stages file (entry 40);
- document 18: the numbers of nonlinear laws, which differ even between two exports of
  one model; document 3 refers to laws by number, so both are compared by the laws'
  content;
- empty documents;
- numbers that differ past LIRA's six significant digits.

Loads (documents 6 and 7) are compared on their own, as loads.
"""

import math

from lira_sapr_mcp.services.textformat import TextModel, entries

LOAD_DOCS = frozenset({6, 7})
# Names of documents from LIRA's help ("Импортировать задачу из текстового файла").
DOC_NAMES = {
    0: "общие данные",
    1: "элементы",
    2: "шарниры",
    3: "жёсткости",
    4: "узлы",
    5: "связи и объединения перемещений",
    8: "таблица РСУ",
    10: "коэффициенты групп МОНТАЖ+",
    14: "жёсткие вставки",
    15: "динамические загружения",
    16: "нелинейные и монтажные загружения",
    17: "углы и местные оси",
    18: "законы нелинейного деформирования, арматура",
    19: "упругое основание",
    24: "дополнительные загружения стадий монтажа",
    28: "динамика во времени",
    32: "назначение типов армирования (ТЗА)",
    160: "история нелинейных загружений",
}


def text_differences(expected: str, got: str, skip: frozenset[int] = LOAD_DOCS) -> list[str]:
    """How the text ``got`` (of the rebuilt model) differs from ``expected``, one line
    per document, in Russian for the user's report."""
    a, b = _canonical(TextModel.parse(expected)), _canonical(TextModel.parse(got))
    out = []
    for n in sorted(a.keys() | b.keys()):
        old, new = a.get(n, []), b.get(n, [])
        same = len(old) == len(new) and all(map(_same_entry, old, new))
        if n not in skip and not same:
            out.append(_describe(n, old, new))
    return out


def _same_entry(a: str, b: str) -> bool:
    """Equal entries, numbers up to LIRA's six significant digits: a density written
    as 2.5 comes back as 2.50001."""
    x, y = a.split(), b.split()
    return len(x) == len(y) and all(map(_same_token, x, y))


def same_number(a: float, b: float) -> bool:
    """Equal up to LIRA's six significant digits."""
    return math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-9)


def _same_token(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        int(a), int(b)
        return False  # node and element numbers are exact
    except ValueError:
        pass
    try:
        return same_number(float(a), float(b))
    except ValueError:
        return False


def _canonical(model: TextModel) -> dict[int, list[str]]:
    docs = {n: _entries(body) for n, body in model.docs}
    if 0 in docs:
        docs[0] = [_general(e) for e in docs[0]]
    laws = {e.split()[0]: " ".join(e.split()[1:]) for e in docs.get(18, [])}
    if 18 in docs:
        docs[18] = sorted(laws.values())
    if 3 in docs:
        docs[3] = [_with_laws(e, laws) for e in docs[3]]
    return {n: entries for n, entries in docs.items() if entries}


def _entries(body: str) -> list[str]:
    return [" ".join(e.split()) for e in entries(body)]


def _general(entry: str) -> str:
    """An entry of document 0 without the names the rebuild changes."""
    for key in ("1;", "40;"):
        if entry.startswith(key):
            return key
    return entry


def _with_laws(entry: str, laws: dict[str, str]) -> str:
    """A stiffness entry with its references to nonlinear laws replaced by the laws
    themselves: ``NSG law`` (concrete), ``NSAG group count steel bars steel bars …``
    (per reinforcement group, pairs of the steel's law and the law placing the bars)."""
    tokens = entry.split()

    def law(i: int) -> None:
        if i < len(tokens):
            tokens[i] = f"[{laws.get(tokens[i], tokens[i])}]"

    if "NSG" in tokens:
        law(tokens.index("NSG") + 1)
    if "NSAG" in tokens:
        for i in range(tokens.index("NSAG") + 3, len(tokens)):
            law(i)
    return " ".join(tokens)


def _describe(n: int, old: list[str], new: list[str], limit: int = 150) -> str:
    name = f" ({DOC_NAMES[n]})" if n in DOC_NAMES else ""
    where = f"раздел {n}{name} текста модели"
    if not new:
        return f"{where} пропал"
    if not old:
        return f"{where} появился: «{clip(new[0], limit)}»"
    pairs = enumerate(zip(old, new, strict=False))
    i = next((i for i, (x, y) in pairs if not _same_entry(x, y)), min(len(old), len(new)))
    was = clip(old[i], limit) if i < len(old) else "нет"
    now = clip(new[i], limit) if i < len(new) else "нет"
    count = f"; записей было {len(old)}, стало {len(new)}" if len(old) != len(new) else ""
    return f"{where} отличается: запись {i + 1} была «{was}», стала «{now}»{count}"


def clip(text: str, limit: int, more: str = "…") -> str:
    """``text`` cut to ``limit`` characters, ``more`` marking the cut."""
    return text if len(text) <= limit else text[:limit] + more
