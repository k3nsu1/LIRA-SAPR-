"""Service: deflections of beams and drift of columns of bar frames, as facts for the
explanatory note (RPZ). No verdict: the limits are set per project (SP RK EN 1993-1-1
7.2.1, 7.2.2; national annex NP.2.21, NP.2.23); the engineer compares L / f with them."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lira_sapr_mcp.errors import LiraInputError
from lira_sapr_mcp.numbers import format_numbers
from lira_sapr_mcp.postprocessing import members as pm
from lira_sapr_mcp.services.slabs import MM, Note, open_model, read_mesh

SHOWN = 10  # spans and columns listed to the agent; the table file has all


@dataclass
class Frame:
    """What the post-processing takes: the mesh in metres, the chosen members and the X, Y, Z
    displacement (m) of their nodes in one load case or combination."""

    path: str
    nodes: dict[int, tuple[float, float, float]]
    elements: dict[int, tuple[int, tuple[int, ...]]]
    members: list[pm.Member]
    held: set[int]
    displacement: dict[int, tuple[float, float, float]]
    notes: list[Note] = field(default_factory=list)
    stiffness_of: dict[int, int] = field(default_factory=dict)
    ground: set[int] = field(default_factory=set)  # restrained and one-node support nodes


def _xyz(rows: list[dict], what: str) -> dict[int, tuple[float, float, float]]:
    out = {}
    for r in rows:
        if "shape" in r:
            raise LiraInputError(
                f"{what} is dynamic: deflections need a static load case or a load combination"
            )
        out[int(r["node"])] = (
            float(r.get("X", 0.0)),
            float(r.get("Y", 0.0)),
            float(r.get("Z", 0.0)),
        )
    return out


def read_frame(
    adapter: Any,
    model: str | None,
    within: list[int] | None,
    supported_by: list[int] | None,
    case: int | None,
    combination: int | None,
) -> Frame:
    """On the COM thread: the model's bars, their supports and displacements."""
    path = open_model(adapter, model)
    mesh = read_mesh(adapter, path)
    chosen = set(within) if within is not None else None
    found = pm.members(mesh.nodes, mesh.elements, chosen)
    if not found:
        raise LiraInputError("No bars (FE 1-10) among the chosen elements")
    held = pm.supports(
        mesh.nodes, mesh.elements, mesh.restrained, mesh.groups, set(supported_by or [])
    )
    wanted = sorted({n for m in found for n in m.nodes})
    if combination is None:
        rows = adapter.node_displacements(path, wanted, [case])
        what = f"Load case {case}"
    else:
        rows = adapter.combination_displacements(path, wanted, [combination])
        what = f"Combination {combination}"
    disp = _xyz(rows, what)
    missing = [n for n in wanted if n not in disp]
    if missing:
        raise LiraInputError(
            f"No displacements for {len(missing)} nodes (e.g. {missing[:5]}): run "
            "lira_run_analysis for this version of the model"
        )
    stiffness_of = {
        int(r[0]): int(r[1])
        for r in adapter.read_table(path, "Elements_Stiffnesses")
        if len(r) > 1 and r[0] is not None and r[1] is not None
    }
    ground = pm.supports(mesh.nodes, mesh.elements, mesh.restrained, columns=False)
    return Frame(
        path, mesh.nodes, mesh.elements, found, held, disp, list(mesh.notes), stiffness_of, ground
    )


def _ratio(r: float) -> float | None:
    return None if r != r or r == float("inf") else round(r)


def compute(frame: Frame) -> dict[str, Any]:
    """Spans with their largest deflection, the worst first; columns with their drift."""
    found, unsupported = pm.spans(frame.members, frame.nodes, frame.held)
    read = [pm.span_deflection(s, frame.nodes, frame.displacement) for s in found]
    blind = [d for d in read if d.node is None]
    measured = sorted((d for d in read if d.node is not None), key=lambda d: d.ratio)
    joined = pm.joints(frame.members, frame.elements)
    drifts = sorted(
        (
            pm.drift(k, m, frame.nodes, frame.displacement, joined)
            for k, m in enumerate(frame.members)
            if m.column
        ),
        key=lambda d: d.ratio,
    )
    notes = [n.ru for n in frame.notes]
    if blind:
        notes.append(
            f"Пролётов без промежуточных узлов: {len(blind)} (например, элементы "
            f"{', '.join(map(str, _blind_examples(frame, blind)))}): "
            "прогиб внутри одного конечного элемента по узлам не определяется — разбейте стержень "
            "в ЛИРЕ."
        )
    if unsupported:
        notes.append(
            f"Балок без найденных опор: {len(unsupported)}; пролёт принят между их концами. "
            "Укажите несущие элементы (supported_by), если балка опирается на другие балки."
        )
    return {
        "spans": [span_row(frame, d) for d in measured],
        "columns": [drift_row(frame, d) for d in drifts],
        "blind_spans": len(blind),
        "notes": notes,
    }


def _blind_examples(frame: Frame, blind: list[pm.SpanDeflection], count: int = 5) -> list[int]:
    """The element of each span with no inner node (a span of one bar), without repeats."""
    out: list[int] = []
    for d in blind:
        m = frame.members[d.span.member]
        i, j = (m.nodes.index(n) for n in d.span.nodes[:2])
        el = m.elements[min(i, j)]
        if el not in out:
            out.append(el)
        if len(out) == count:
            break
    return out


def span_elements(m: pm.Member, span: pm.Span) -> list[int]:
    """The bars of a span: those of its member between the span's end nodes."""
    i, j = sorted((m.nodes.index(span.nodes[0]), m.nodes.index(span.nodes[-1])))
    return list(m.elements[i:j])


def span_row(frame: Frame, d: pm.SpanDeflection) -> dict[str, Any]:
    m = frame.members[d.span.member]
    return {
        # LIRA's notation: "22308-22326" would mean every element between, not 7 of them
        "elements": format_numbers(span_elements(m, d.span)),
        "kind": "консоль" if d.span.cantilever else "пролёт",
        "from_node": d.span.nodes[0],
        "to_node": d.span.nodes[-1],
        "length_m": round(d.span.length, 3),
        "node": d.node,
        "f_mm": round(d.f * MM, 2),
        "fz_mm": round(d.fz * MM, 2),
        "L_to_f": _ratio(d.ratio),
    }


def drift_row(frame: Frame, d: pm.Drift) -> dict[str, Any]:
    m = frame.members[d.member]
    worst = max(d.parts, key=lambda p: p[3] / p[2] if p[2] > 0 else 0, default=None)
    row = {
        "elements": format_numbers(list(m.elements)),
        "base_node": d.base,
        "top_node": d.top,
        "height_m": round(d.h, 3),
        "u_mm": round(d.u * MM, 2),
        "H_to_u": _ratio(d.ratio),
    }
    if worst and len(d.parts) > 1:
        lo, hi, h, u = worst
        row["worst_storey"] = {
            "from_node": lo,
            "to_node": hi,
            "height_m": round(h, 3),
            "u_mm": round(u * MM, 2),
            "h_to_u": _ratio(h / u if u > pm.SAME else float("inf")),
        }
    return row


def for_agent(result: dict[str, Any]) -> dict[str, Any]:
    """The worst spans and columns; how many there are in all."""
    out = {
        "spans": result["spans"][:SHOWN],
        "spans_total": len(result["spans"]),
        "columns": result["columns"][:SHOWN],
        "columns_total": len(result["columns"]),
    }
    if result["notes"]:
        out["notes"] = result["notes"]
    return out


SPAN_HEADS = [
    ("elements", "Элементы"),
    ("kind", "Вид"),
    ("from_node", "Узел начала"),
    ("to_node", "Узел конца"),
    ("length_m", "L, м"),
    ("node", "Узел max f"),
    ("f_mm", "f, мм"),
    ("fz_mm", "fz, мм"),
    ("L_to_f", "L/f"),
]
COLUMN_HEADS = [
    ("elements", "Элементы"),
    ("base_node", "Узел низа"),
    ("top_node", "Узел верха"),
    ("height_m", "H, м"),
    ("u_mm", "u, мм"),
    ("H_to_u", "H/u"),
]


def write_tables(result: dict[str, Any], out_dir: Path, stem: str, source: str) -> list[str]:
    """An Excel book with all spans and columns and a Word file with the same tables for the
    RPZ. Existing files are replaced."""
    from docx import Document
    from openpyxl import Workbook

    out_dir.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheets = [
        ("Прогибы балок", SPAN_HEADS, result["spans"]),
        ("Смещения колонн", COLUMN_HEADS, result["columns"]),
    ]
    first = True
    for title, heads, rows in sheets:
        ws = book.active if first else book.create_sheet()
        first = False
        ws.title = title
        ws.append([h for _, h in heads])
        for r in rows:
            ws.append([r.get(k) for k, _ in heads])
    xlsx = out_dir / f"{stem}_members.xlsx"
    book.save(xlsx)

    doc = Document()
    doc.add_paragraph(f"Перемещения стержневых элементов: {source}.")
    doc.add_paragraph(
        "Прогиб f — перемещение узла поперёк стержня относительно прямой, соединяющей опоры "
        "пролёта (у консоли — относительно опоры); fz — его вертикальная составляющая со знаком "
        "ЛИРЫ. Предельные значения устанавливаются для проекта (СП РК EN 1993-1-1, п. 7.2.1, "
        "7.2.2; НП.2.21, НП.2.23)."
    )
    for title, heads, rows in sheets:
        doc.add_paragraph(title)
        table = doc.add_table(rows=1, cols=len(heads))
        table.style = "Table Grid"
        for cell, (_, h) in zip(table.rows[0].cells, heads, strict=True):
            cell.text = h
        for r in rows:
            cells = table.add_row().cells
            for cell, (k, _) in zip(cells, heads, strict=True):
                v = r.get(k)
                cell.text = "" if v is None else str(v).replace(".", ",")
    for n in result["notes"]:
        doc.add_paragraph(n)
    docx = out_dir / f"{stem}_members.docx"
    doc.save(docx)
    return [str(xlsx), str(docx)]


def pictures(frame: Frame, out_dir: Path, stem: str, source: str, view: str = "iso") -> list[str]:
    """PNG for the RPZ appendices: the scheme by stiffness, the deformed scheme and the spans
    by L / f, of the chosen bars. Existing files are replaced."""
    from lira_sapr_mcp.postprocessing import frame_plots as fp

    bars = {el: frame.elements[el][1] for m in frame.members for el in m.elements}
    used = {n for ab in bars.values() for n in ab}
    # On the scheme only what holds the model: restraints and one-node supports; not the
    # tops of columns, where a beam rests and its span is cut.
    supports = {n for n in frame.ground if n in used}
    found, _ = pm.spans(frame.members, frame.nodes, frame.held)
    read = [pm.span_deflection(sp, frame.nodes, frame.displacement) for sp in found]
    spans = [(d.span.nodes, d.ratio) for d in read if d.node is not None and d.ratio == d.ratio]
    spans = [(row, min(r, 1e9)) for row, r in spans]
    out = [
        fp.scheme(
            frame.nodes,
            bars,
            frame.stiffness_of,
            supports,
            view,
            out_dir / f"{stem}_scheme_{view}.png",
            "Расчётная схема: стержни по жёсткостям",
        ),
        fp.deformed(
            frame.nodes,
            bars,
            frame.displacement,
            view,
            out_dir / f"{stem}_deformed_{view}.png",
            f"Деформированная схема: {source}",
        )[0],
        fp.spans_map(
            frame.nodes,
            bars,
            spans,
            view,
            out_dir / f"{stem}_spans_{view}.png",
            f"Прогибы пролётов L/f: {source}",
        ),
    ]
    return [str(p) for p in out]
