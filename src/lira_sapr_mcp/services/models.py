"""Model-level operations that combine several adapter calls (run on the COM thread)."""

from collections import Counter
from dataclasses import asdict
from typing import Any

from lira_sapr_mcp.adapters.base import MODEL_TYPES
from lira_sapr_mcp.errors import LiraComError


def _rows(adapter: Any, ref: str, table: str) -> list[list]:
    try:
        return adapter.read_table(ref, table)
    except LiraComError:
        # LIRA refuses to build some tables for an empty model; treat as no rows.
        return []


def model_summary(adapter: Any, ref: str) -> dict[str, Any]:
    """What an agent needs first: size, element types, stiffnesses, load cases, units."""
    info = adapter.document_info(ref)
    nodes = _rows(adapter, ref, "Nodes_Coordinates")
    elements = _rows(adapter, ref, "Elements_TypeAndNodes")
    stiffnesses = _rows(adapter, ref, "Stiffnesses")
    load_cases = _rows(adapter, ref, "LoadCases")
    by_type = Counter(int(row[1]) for row in elements if row and row[1] is not None)
    results = results_summary(adapter, info.path, len(nodes), len(elements))
    return {
        "model": asdict(info),
        "model_type_meaning": MODEL_TYPES.get(info.model_type, "see LIRA documentation"),
        "units": asdict(adapter.units()),
        "nodes": len(nodes),
        "elements": len(elements),
        "elements_by_fe_type": {str(k): v for k, v in sorted(by_type.items())},
        "stiffnesses": [
            {"number": row[0], "parameters": next((c for c in row[3:] if c), None)}
            for row in stiffnesses
        ],
        "load_cases": [
            {"number": row[0], "name": row[1], "kind": row[3] if len(row) > 3 else None}
            for row in load_cases
        ],
        "results": results,
    }


def results_summary(adapter: Any, path: str, nodes: int, elements: int) -> dict[str, Any]:
    """Analysis results under the model's name, with a note when they may not be its own."""
    if not path:
        return {"present": False, "note": "the model is not saved, so it has no results"}
    info = adapter.results_info(path)
    if not info["present"]:
        return info
    if (info["nodes"], info["elements"]) != (nodes, elements):
        info["note"] = (
            f"the results have {info['nodes']} nodes and {info['elements']} elements, the "
            f"model has {nodes} and {elements}: they belong to another version of the model "
            "or to another model of the same name; run lira_run_analysis again"
        )
    return info
