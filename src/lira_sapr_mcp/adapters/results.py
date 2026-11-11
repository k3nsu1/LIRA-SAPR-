"""Reading analysis results through LiraResAPI.dll (runs on the COM thread).

The library reads LIRA's result files by model name (no path), in fixed units: meters,
radians, ton-force (tf) and their products. Indices: load cases and design sections from
1, history 0 outside non-linear histories. A static load case has one deformed shape (1);
a dynamic one has several, which displacements address by mode number (e.g. 1, 5, 11)
and forces by position (1, 2, 3) - checked on a live LIRA 2024 R2. Shape -1 is the
summary shape of a dynamic load case (LIRA's "summary" values, combined over the shapes:
the square root of the sum of squares, with a sign), in displacements and forces alike;
the ForcesSum flag of the request changes nothing.
"""

import logging
from pathlib import Path
from typing import Any

from lira_sapr_mcp.adapters.discovery import LiraInstall
from lira_sapr_mcp.adapters.typelibs import (
    RESULTS_ACCESS,
    coclass_id,
    registered_typelibs,
    results_library,
)
from lira_sapr_mcp.errors import LiraComError, LiraInputError
from lira_sapr_mcp.numbers import parse_numbers

log = logging.getLogger(__name__)

# What the library raises when the model has no results files (E_UNEXPECTED).
NO_RESULTS_HRESULT = -2147418113

# Results library of LIRA 2024 R2: used when a release's own is not registered.
RES_TYPELIB = ("{BD6C09E6-B5EE-4DAB-8D61-C009BA755C46}", 1, 0)
RES_CLASS = "{4F4546AE-AC54-48A2-9414-1B9EF6519A37}"
NUMBERS_AT_ONCE = 5000  # numbers per request where a slab asks for many, to keep each short


DISPLACEMENTS = {1: "X", 2: "Y", 3: "Z", 4: "UX", 5: "UY", 6: "UZ"}

# LiraElementFamilyEnum -> (family name, getter prefix)
FAMILIES = {
    1: ("bar", "GetBar"),
    2: ("plate", "GetPlate"),
    3: ("solid", "GetSolid"),
    4: ("special", "GetSpecElement"),
    5: ("special_58_59", "GetSpec_58_59_"),
    6: ("special_310", "GetSpec_310_"),
    7: ("special_264", "GetSpec_264_"),
}
F, M, BI = "force", "moment", "bimoment"
FPL, MPL, S = "force_per_length", "moment_per_length", "stress"
# LiraForceEnum -> (family, component, unit kind); units as the type library states them
# (it gives Mx, My, Mz of special elements in tf, not tf*m).
FORCES = {
    1: (1, "N", F), 2: (1, "Mx", M), 3: (1, "My", M), 4: (1, "Qz", F), 5: (1, "Mz", M),
    6: (1, "Qy", F), 7: (1, "Ry", F), 8: (1, "Rz", F), 9: (1, "Bw", BI), 10: (1, "Tw", M),
    11: (2, "Nx", S), 12: (2, "Ny", S), 13: (2, "Nz", S), 14: (2, "Txy", S),
    15: (2, "Txz", S), 16: (2, "Mx", MPL), 17: (2, "My", MPL), 18: (2, "Mxy", MPL),
    19: (2, "Qx", FPL), 20: (2, "Qy", FPL), 21: (2, "Rz", S),
    22: (3, "Nx", S), 23: (3, "Ny", S), 24: (3, "Nz", S), 25: (3, "Txy", S),
    26: (3, "Txz", S), 27: (3, "Tyz", S),
    28: (4, "N", F), 29: (4, "Nx", F), 30: (4, "Ny", F), 31: (4, "Nz", F), 32: (4, "Mx", F),
    33: (4, "My", F), 34: (4, "Mz", F), 35: (4, "Qy", F), 36: (4, "Qz", F), 37: (4, "Rx", F),
    38: (4, "Ry", F), 39: (4, "Rz", F), 40: (4, "Rux", M), 41: (4, "Ruy", M),
    42: (4, "Ruz", M),
    43: (5, "Qz", F), 44: (5, "Ny", F), 45: (5, "Qx", F),
    46: (6, "N", F), 47: (6, "My", M), 48: (6, "Mx", M), 49: (6, "Qz", F), 50: (6, "Mz", M),
    51: (6, "Qy", F),
    52: (7, "N", F), 53: (7, "Qz", F), 54: (7, "Qy", F),
}  # fmt: skip


# LiraLimitStateForcesEnum: ultimate / serviceability, full or long-term loads.
LIMIT_STATES = {1: "ultimate", 2: "ultimate_long", 3: "serviceability", 4: "serviceability_long"}
# LiraInternalGroupsEnum: DCF groups as LIRA names them.
GROUPS = {1: "A1", 2: "B1", 3: "C1", 4: "D1", 5: "A2", 6: "B2", 7: "C2", 8: "D2", 9: "E1"}
CRANE_SEISMIC = {1: "crane", 2: "seismic", 3: "crane+seismic"}
COMBINATION_CODES = {
    0: "SNiP", 1: "Eurocode", 2: "ACI", 3: "BAEL", 4: "IBC", 5: "DBN", 6: "STB EN", 7: "SP",
    8: "SP RK EN", 9: "TKP EN", 10: "EN",
}  # fmt: skip
NO_DCL = "set load combinations (input table DCL) and run lira_run_analysis"
NO_DCF = (
    "set design combinations of forces (DCF: document 8 of the text format or LIRA's DCF "
    "dialog) and run lira_run_analysis"
)


# Shape -1 of a dynamic load case is LIRA's summary shape; rows label it "sum".
SUMMARY_SHAPE, SUMMARY = -1, "sum"
NO_DYNAMICS = "the model has no dynamic load cases, or run lira_run_analysis first"
NO_DESIGN = (
    "no reinforcement was selected: set reinforced concrete design data and run "
    "lira_run_analysis (it includes the design stage)"
)
NO_FRAGMENT = "define a fragment for loads on fragment in LIRA and run lira_run_analysis"
NO_PUNCHING = (
    "run lira_punching_check after lira_run_analysis (LIRA computes them only in its "
    "punching shear check, for punching contours defined in the model)"
)

# Result name -> getter of ILiraPeriodsOfVibrationsResponse (arrays per mode).
MODES = {
    "period": "GetPeriods",
    "frequency_hz": "GetFrequenciesHz",
    "frequency_rad_s": "GetFrequenciesRadPerSec",
    "modal_mass": "GetModalMasses",
    "accumulated_modal_mass": "GetAccumulatedModalMasses",
    "participation": "GetParticipFactors",
}
BAR_REINFORCEMENT = ("AU1", "AU2", "AU3", "AU4", "AS1", "AS2", "AS3", "AS4", "PERC",
                     "ASW1", "ASW2")  # fmt: skip
PLATE_AREAS = ("AS1", "AS2", "AS3", "AS4", "ASW1", "ASW2")
REINFORCEMENT_NAMES = {"CrackSHORT": "crack_short", "CrackLONG": "crack_long"}
REINFORCEMENT_NOT_COMPUTED = 200
# LiraReinforcementErrorEnum; the numbered ones are LIRA's design error codes.
REINFORCEMENT_ERRORS = {
    -1: "section is not adequate",
    -71: "LIRA design warning 71",
    -67: "LIRA design warning 67",
}
REINFORCEMENT_WARNINGS = (-71, -67)  # the value is still given
FRAGMENT = {1: "RX", 2: "RY", 3: "RZ", 4: "RUX", 5: "RUY", 6: "RUZ", 7: "RBW"}
PUNCHING = {1: "N", 2: "MX", 3: "MY", 4: "QX", 5: "QY", 6: "MZ"}
FRAGMENT_KIND = {"RX": F, "RY": F, "RZ": F, "RUX": M, "RUY": M, "RUZ": M, "RBW": BI}
PUNCHING_KIND = {"N": F, "QX": F, "QY": F, "MX": M, "MY": M, "MZ": M}


def force_kinds(family: str) -> dict[str, str]:
    """Unit kind of each component of a family, e.g. {"N": "force", "My": "moment"}."""
    code = next(c for c, (name, _) in FAMILIES.items() if name == family)
    return {comp: kind for fam, comp, kind in FORCES.values() if fam == code}


class ResultsReader:
    """A fresh results object per request: a second request through the same object
    returns garbage numbers (LIRA 2024, checked on a live install)."""

    def __init__(self, install: LiraInstall) -> None:
        self.install = install
        self._c: Any = None
        self._library: tuple[str, int, int, str] | None = None

    def _choose_library(self) -> tuple[str, int, int, str]:
        """The release's own results library, else the one of LIRA 2024 R2."""
        if self._library is None:
            library = (*RES_TYPELIB, RES_CLASS)
            try:
                own = results_library(self.install, registered_typelibs())
                if own is not None:
                    cls = coclass_id(own.path, RESULTS_ACCESS)
                    if cls:  # all four from one release, never a mix
                        library = (own.guid, *own.major_minor, cls)
            except Exception as exc:  # registry or type library unreadable
                log.warning("Results library of LIRA %s not found: %s", self.install.year, exc)
            log.info("results library %s %d.%d, class %s", *library)
            self._library = library
        return self._library

    def _access(self) -> Any:
        import win32com.client
        from win32com.client import gencache

        guid, major, minor, cls = self._choose_library()
        try:
            if self._c is None:
                gencache.EnsureModule(guid, 0, major, minor)
                self._c = win32com.client.constants
            obj = win32com.client.Dispatch(cls)
        except Exception as exc:
            raise LiraComError(f"LIRA results library is not available: {exc}") from exc
        return win32com.client.CastTo(obj, "ILiraResultsAccess")

    def close(self) -> None:
        self._c = None

    def _query(
        self,
        model_path: str,
        kind: str,
        iface: str,
        method: str,
        fill: Any = None,
        missing: str = "run lira_run_analysis for this model first",
    ):
        import win32com.client

        access = self._access()
        req = win32com.client.CastTo(access.CreateNewRequest(getattr(self._c, kind)), iface)
        req.DocumentName = Path(model_path).stem
        if fill:
            fill(req)
        try:
            return getattr(access, method)(req)
        except Exception as exc:
            if _scode(exc) == NO_RESULTS_HRESULT:
                raise LiraInputError(
                    f"No analysis results of this kind for {Path(model_path).stem!r}: {missing}"
                ) from exc
            raise LiraComError(
                f"Reading results of {Path(model_path).stem!r} failed: {exc}"
            ) from exc

    def node_displacements(
        self, model_path: str, nodes: list[int] | None, cases: list[int] | None
    ) -> list[dict]:
        def fill(req: Any) -> None:
            if nodes:
                req.Nodes.AddFromString(" ".join(map(str, nodes)))

        resp = self._query(
            model_path,
            "kLiraRequest_LoadCaseDisplacement",
            "ILiraLoadCaseDisplacementsRequest",
            "LoadCaseDisplacements",
            fill,
        )
        directions = _numbers(resp.Directions.AddToString())
        getters = {d: getattr(resp, f"GetNode{name}") for d, name in DISPLACEMENTS.items()}
        shapes = _shapes(resp.LoadCases, cases, summary=True)
        dynamic = _dynamic_cases(resp.LoadCases)
        rows = []
        for node in _items(resp.NodeNumbers):
            for case, modes in shapes:
                for _, mode in modes:
                    row: dict[str, Any] = {"node": node, "case": case}
                    if len(modes) > 1 or case in dynamic:
                        row["shape"] = _shape_label(mode)
                    for d in directions:
                        if d in getters:
                            row[DISPLACEMENTS[d]] = float(getters[d](node, case, mode, 0))
                    rows.append(row)
        return rows

    def vertical_displacements(
        self,
        model_path: str,
        nodes: list[int],
        case: int | None = None,
        combination: int | None = None,
    ) -> dict[int, float]:
        """Z displacement (m) of each node in one static load case or one load
        combination (DCL): only Z is read, so a slab of many nodes reads fast. Asked in
        parts."""
        out: dict[int, float] = {}
        for start in range(0, len(nodes), NUMBERS_AT_ONCE):
            part = nodes[start : start + NUMBERS_AT_ONCE]
            out |= self._vertical_part(model_path, part, case, combination)
        return out

    def _vertical_part(
        self, model_path: str, nodes: list[int], case: int | None, combination: int | None
    ) -> dict[int, float]:
        wanted = " ".join(map(str, nodes))
        if combination is None:
            resp = self._query(
                model_path,
                "kLiraRequest_LoadCaseDisplacement",
                "ILiraLoadCaseDisplacementsRequest",
                "LoadCaseDisplacements",
                lambda req: req.Nodes.AddFromString(wanted),
            )
            ((_, shapes),) = _shapes(resp.LoadCases, [case])
            if len(shapes) > 1 or case in _dynamic_cases(resp.LoadCases):
                raise LiraInputError(
                    f"Load case {case} is dynamic: deflections need a static load case or a "
                    "load combination"
                )
            get = resp.GetNodeZ
            mode = shapes[0][1]
            return {n: float(get(n, case, mode, 0)) for n in _items(resp.NodeNumbers)}
        resp = self._query(
            model_path,
            "kLiraRequest_LoadCombinationDisplacements",
            "ILiraLoadCombinationDisplacementsRequest",
            "LoadCombinationDisplacements",
            lambda req: req.Nodes.AddFromString(wanted),
            missing=NO_DCL,
        )
        _chosen(_combination_infos(resp.LoadCombinations), [combination])
        get = resp.GetNodeZ
        return {n: float(get(n, combination)) for n in _items(resp.NodeNumbers)}

    def element_forces(
        self,
        model_path: str,
        elements: list[int] | None,
        cases: list[int] | None,
        families: list[str] | None = None,
    ) -> list[dict]:
        """Forces and stresses per element, load case, shape and design section; each row
        names its element family, since components differ between families."""
        elements = self._elements(model_path, elements)
        wanted = " ".join(map(str, elements))
        resp = self._query(
            model_path,
            "kLiraRequest_LoadCaseForces",
            "ILiraLoadCaseForcesRequest",
            "LoadCaseForces",
            lambda req: req.Elements.AddFromString(wanted),
        )
        shapes = _shapes(resp.LoadCases, cases, summary=True)
        dynamic = _dynamic_cases(resp.LoadCases)
        getters: dict[int, Any] = {}
        rows = []
        for el in elements:
            found = _components(resp, el, families, getters)
            if found is None:
                continue
            family, components = found
            sections = resp.GetSectionCount(el)
            for case, modes in shapes:
                for position, mode in modes:
                    for section in range(1, sections + 1):
                        row: dict[str, Any] = {"family": family, "element": el, "case": case}
                        if len(modes) > 1 or case in dynamic:
                            row["shape"] = _shape_label(mode)
                        row["section"] = section
                        for name, get in components:
                            row[name] = float(get(el, section, case, position, 0))
                        rows.append(row)
        return rows

    def main_data(self, model_path: str) -> dict[str, int] | None:
        """Node and element counts of the results kept under this model's name, or None
        when there are no results."""
        try:
            main = self._query(
                model_path, "kLiraRequest_MainData", "ILiraMainDataRequest", "MainData"
            )
        except LiraInputError:
            return None
        return {"nodes": int(main.GetNodeCount()), "elements": int(main.GetElementCount())}

    def _elements(self, model_path: str, elements: list[int] | None) -> list[int]:
        if elements:
            return elements
        main = self._query(model_path, "kLiraRequest_MainData", "ILiraMainDataRequest", "MainData")
        return list(range(1, main.GetElementCount() + 1))

    def load_combinations(self, model_path: str) -> dict[str, Any]:
        """The load combinations (DCL) of the analysed model and their building code."""
        resp = self._query(
            model_path,
            "kLiraRequest_LoadCombinationDisplacements",
            "ILiraLoadCombinationDisplacementsRequest",
            "LoadCombinationDisplacements",
            lambda req: req.Nodes.AddFromString("1"),
            missing=NO_DCL,
        )
        return _combination_infos(resp.LoadCombinations)

    def combination_displacements(
        self, model_path: str, nodes: list[int] | None, combinations: list[int] | None
    ) -> list[dict]:
        def fill(req: Any) -> None:
            if nodes:
                req.Nodes.AddFromString(" ".join(map(str, nodes)))

        resp = self._query(
            model_path,
            "kLiraRequest_LoadCombinationDisplacements",
            "ILiraLoadCombinationDisplacementsRequest",
            "LoadCombinationDisplacements",
            fill,
            missing=NO_DCL,
        )
        chosen = _chosen(_combination_infos(resp.LoadCombinations), combinations)
        directions = _numbers(resp.Directions.AddToString())
        getters = {d: getattr(resp, f"GetNode{name}") for d, name in DISPLACEMENTS.items()}
        rows = []
        for node in _items(resp.NodeNumbers):
            for dcl in chosen:
                row: dict[str, Any] = {"node": node, "combination": dcl}
                for d in directions:
                    if d in getters:
                        row[DISPLACEMENTS[d]] = float(getters[d](node, dcl))
                rows.append(row)
        return rows

    def combination_forces(
        self,
        model_path: str,
        elements: list[int] | None,
        combinations: list[int] | None,
        families: list[str] | None = None,
        limit_states: list[int] | None = None,
    ) -> list[dict]:
        """Forces in load combinations (DCL) per element, combination, limit state and
        design section."""
        elements = self._elements(model_path, elements)
        wanted = " ".join(map(str, elements))
        resp = self._query(
            model_path,
            "kLiraRequest_LoadCombinationForces",
            "ILiraLoadCombinationForcesRequest",
            "LoadCombinationForces",
            lambda req: req.Elements.AddFromString(wanted),
            missing=NO_DCL,
        )
        chosen = _chosen(_combination_infos(resp.LoadCombinations), combinations)
        states = _limit_states(limit_states)
        getters: dict[int, Any] = {}
        rows = []
        for el in elements:
            found = _components(resp, el, families, getters)
            if found is None:
                continue
            family, components = found
            sections = resp.GetSectionCount(el)
            for dcl in chosen:
                for ls in states:
                    for section in range(1, sections + 1):
                        row: dict[str, Any] = {
                            "family": family,
                            "element": el,
                            "combination": dcl,
                            "limit_state": LIMIT_STATES[ls],
                            "section": section,
                        }
                        for name, get in components:
                            row[name] = float(get(el, section, dcl, ls))
                        rows.append(row)
        return rows

    def design_forces(
        self,
        model_path: str,
        elements: list[int] | None,
        families: list[str] | None = None,
        limit_states: list[int] | None = None,
    ) -> list[dict]:
        """Design combinations of forces (DCF) per element, design section, limit state
        and combination, with the criterion and the load cases that formed each one."""
        elements = self._elements(model_path, elements)
        wanted = " ".join(map(str, elements))
        resp = self._query(
            model_path,
            "kLiraRequest_DesignCombinationForces",
            "ILiraDesignCombinationForcesRequest",
            "DesignCombinationForces",
            lambda req: req.Elements.AddFromString(wanted),
            missing=NO_DCF,
        )
        states = _limit_states(limit_states)
        getters: dict[int, Any] = {}
        rows = []
        for el in elements:
            found = _components(resp, el, families, getters)
            if found is None:
                continue
            family, components = found
            for section in range(1, resp.GetSectionCount(el) + 1):
                for ls in states:
                    for k in range(1, resp.GetDCLCount(el, section, ls) + 1):
                        row: dict[str, Any] = {
                            "family": family,
                            "element": el,
                            "section": section,
                            "limit_state": LIMIT_STATES[ls],
                            "dcf": k,
                            "criterion": int(resp.GetCriterionNumber(el, section, ls, k)),
                            "group": GROUPS.get(int(resp.GetInternalGroup(el, section, ls, k))),
                            "load_cases": str(resp.GetLoadCases(el, section, ls, k).AddToString()),
                        }
                        flag = int(resp.GetCraneEarthquakeFlag(el, section, ls, k))
                        if flag:
                            row["with"] = CRANE_SEISMIC[flag]
                        for name, get in components:
                            row[name] = float(get(el, section, ls, k))
                        rows.append(row)
        return rows

    def periods(self, model_path: str, cases: list[int] | None) -> list[dict]:
        """Modes of the dynamic load cases: period, frequencies, modal masses."""
        resp = self._query(
            model_path,
            "kLiraRequest_PeriodsOfVibrations",
            "ILiraPeriodsOfVibrationsRequest",
            "PeriodsOfVibrations",
            missing=NO_DYNAMICS,
        )
        dynamic = _numbers(resp.LoadCaseNumbers.AddToString())
        missing = sorted(set(cases or []) - set(dynamic))
        if missing:
            raise LiraInputError(f"Load cases {missing} are not dynamic; dynamic: {dynamic}")
        out = []
        for case in [c for c in dynamic if cases is None or c in cases]:
            columns = {
                name: _doubles(getattr(resp, getter)(case)) for name, getter in MODES.items()
            }
            modes = [
                {"mode": i + 1} | {name: values[i] for name, values in columns.items()
                                   if i < len(values)}
                for i in range(len(columns["period"]))
            ]  # fmt: skip
            code = int(resp.GetDynamicLoadCaseType(case))
            out.append({"case": case, "type": code, "modes": modes})
        return out

    def reinforcement(
        self, model_path: str, elements: list[int] | None, design_option: int = 1
    ) -> list[dict]:
        """Reinforcement selected by LIRA's design: bars per design section (asymmetric and
        symmetric variants, when computed) and plates per element."""
        elements = self._elements(model_path, elements)
        wanted = " ".join(map(str, elements))

        def fill(req: Any) -> None:
            req.DesignOption = design_option
            req.Elements.AddFromString(wanted)

        resp = self._query(
            model_path,
            "kLiraRequest_SelectedReinforcement",
            "ILiraSelectedReinforcementRequest",
            "SelectedReinforcement",
            fill,
            missing=NO_DESIGN,
        )
        total = 1  # kLiraReinforcementData_Total
        rows = []
        for el in elements:
            family = int(resp.GetFamily(el))
            if family == 1:
                for section in range(1, resp.GetSectionCount(el) + 1):
                    for symmetric in (0, 1):
                        values = {n: getattr(resp, f"GetBar{n}")(el, section, total, symmetric, 0)
                                  for n in BAR_REINFORCEMENT}  # fmt: skip
                        for n in ("CrackSHORT", "CrackLONG"):
                            values[n] = getattr(resp, f"GetBar{n}")(el, section, symmetric, 0)
                        # A variant LIRA did not design reads as zeros without cracks.
                        if values["CrackSHORT"][1] == REINFORCEMENT_NOT_COMPUTED and not any(
                            v for v, err in values.values() if err == 0
                        ):
                            continue
                        row = _reinforcement_row("bar", el, values)
                        if row is not None:
                            rows.append(row | {"section": section, "symmetric": bool(symmetric)})
            elif family == 2:
                values = {n: getattr(resp, f"GetPlate{n}")(el, total, 0) for n in PLATE_AREAS}
                for n in ("PercX", "PercY", "PercXY", "CrackSHORT", "CrackLONG"):
                    values[n] = getattr(resp, f"GetPlate{n}")(el, 0)
                row = _reinforcement_row("plate", el, values)
                if row is not None:
                    rows.append(row)
        return rows

    def plate_reinforcement(
        self,
        model_path: str,
        elements: list[int],
        components: list[str],
        design_option: int = 1,
    ) -> dict[str, dict[int, float]]:
        """Reinforcement areas (cm2/m) LIRA selected for plates, only the asked
        components (AS1-AS4, ASW1, ASW2): per component, element -> area, NaN where
        LIRA could not design the section. Elements that are not plates or have no
        design are left out. Asked in parts, as vertical_displacements."""
        out: dict[str, dict[int, float]] = {c: {} for c in components}
        for start in range(0, len(elements), NUMBERS_AT_ONCE):
            part = elements[start : start + NUMBERS_AT_ONCE]
            found = self._reinforcement_part(model_path, part, components, design_option)
            for c in components:
                out[c] |= found[c]
        return out

    def _reinforcement_part(
        self, model_path: str, elements: list[int], components: list[str], design_option: int
    ) -> dict[str, dict[int, float]]:
        wanted = " ".join(map(str, elements))

        def fill(req: Any) -> None:
            req.DesignOption = design_option
            req.Elements.AddFromString(wanted)

        resp = self._query(
            model_path,
            "kLiraRequest_SelectedReinforcement",
            "ILiraSelectedReinforcementRequest",
            "SelectedReinforcement",
            fill,
            missing=NO_DESIGN,
        )
        total = 1  # kLiraReinforcementData_Total
        getters = {c: getattr(resp, f"GetPlate{c}") for c in components}
        out: dict[str, dict[int, float]] = {c: {} for c in components}
        for el in elements:
            if int(resp.GetFamily(el)) != 2:
                continue
            for c, get in getters.items():
                value, err = get(el, total, 0)
                if err == 0 or err in REINFORCEMENT_WARNINGS:
                    out[c][el] = float(value)
                elif err != REINFORCEMENT_NOT_COMPUTED:
                    out[c][el] = float("nan")
        return out

    def fragment_loads(
        self, model_path: str, nodes: list[int] | None, cases: list[int] | None
    ) -> list[dict]:
        """Loads that a fragment of the model transfers to its nodes (as LIRA's "loads
        on fragment"), per node, load case and shape."""
        return self._nodal_loads(
            model_path, nodes, cases, "kLiraRequest_FragmLoadForces", "ILiraFragmLoadsRequest",
            "FragmLoads", FRAGMENT, NO_FRAGMENT,
        )  # fmt: skip

    def punching_loads(
        self, model_path: str, nodes: list[int] | None, cases: list[int] | None
    ) -> list[dict]:
        """Loads on punching shear contours around nodes, per node, load case and shape.

        LIRA answers for every node of the model, with zeros where there is no contour:
        only nodes with a load in some load case are kept."""
        rows = self._nodal_loads(
            model_path, nodes, cases, "kLiraRequest_PunchLoadForces", "ILiraPunchLoadsRequest",
            "PunchLoads", PUNCHING, NO_PUNCHING,
        )  # fmt: skip
        loaded = {r["node"] for r in rows if any(r.get(name) for name in PUNCHING.values())}
        return [r for r in rows if r["node"] in loaded]

    def _nodal_loads(
        self,
        model_path: str,
        nodes: list[int] | None,
        cases: list[int] | None,
        kind: str,
        iface: str,
        method: str,
        names: dict[int, str],
        missing: str,
    ) -> list[dict]:
        def fill(req: Any) -> None:
            if nodes:
                req.Nodes.AddFromString(" ".join(map(str, nodes)))

        resp = self._query(model_path, kind, iface, method, fill, missing=missing)
        available = [c for c in _numbers(resp.Forces.AddToString()) if c in names]
        getters = {c: getattr(resp, "GetNode" + names[c]) for c in available}
        shapes = _shapes(resp.LoadCases, cases, summary=True)
        dynamic = _dynamic_cases(resp.LoadCases)
        rows = []
        for node in _items(resp.NodeNumbers):
            for case, modes in shapes:
                # Shapes by mode number, as for node displacements (checked on a live
                # model for both loads on fragment and on punching contours).
                for _, mode in modes:
                    row: dict[str, Any] = {"node": node, "case": case}
                    if len(modes) > 1 or case in dynamic:
                        row["shape"] = _shape_label(mode)
                    for c, get in getters.items():
                        row[names[c]] = float(get(node, case, mode))
                    rows.append(row)
        return rows


def _components(
    resp: Any, el: int, families: list[str] | None, cache: dict[int, Any]
) -> tuple[str, list[tuple[str, Any]]] | None:
    """The family of an element and (component, getter) for each force it has; None for
    elements outside ``families`` or of an unknown family."""
    family = FAMILIES.get(int(resp.GetFamily(el)))
    if family is None or (families and family[0] not in families):
        return None
    components = []
    for code in _numbers(resp.GetForces(el).AddToString()):
        if code in FORCES and FAMILIES[FORCES[code][0]] == family:
            if code not in cache:
                cache[code] = getattr(resp, family[1] + FORCES[code][1])
            components.append((FORCES[code][1], cache[code]))
    return family[0], components


def _combination_infos(infos: Any) -> dict[str, Any]:
    code = int(infos.Code)
    return {
        "code": COMBINATION_CODES.get(code, code),
        "combinations": [
            {
                "number": int(it.Number),
                "name": str(it.Name),
                "formula": int(it.FormulaNumber),
                "special": bool(it.IsSpecial),
                "seismic": bool(it.IsSeismic),
            }
            for it in (infos.Item(i) for i in range(infos.Count))
        ],
    }


def _chosen(infos: dict[str, Any], wanted: list[int] | None) -> list[int]:
    available = [c["number"] for c in infos["combinations"]]
    if not available:
        raise LiraInputError(f"The model has no load combinations (DCL). {NO_DCL}")
    if wanted is None:
        return available
    missing = sorted(set(wanted) - set(available))
    if missing:
        raise LiraInputError(f"No load combinations {missing}; available: {available}")
    return [c for c in available if c in wanted]


def _limit_states(wanted: list[int] | None) -> list[int]:
    if wanted is None:
        return list(LIMIT_STATES)
    bad = sorted(set(wanted) - set(LIMIT_STATES))
    if bad:
        raise LiraInputError(f"Unknown limit states {bad}: use {LIMIT_STATES}")
    return sorted(set(wanted))


def _reinforcement_row(family: str, el: int, values: dict[str, Any]) -> dict | None:
    """One row of reinforcement, or None when LIRA computed nothing for this variant.

    Each getter returns (value, error code): 0 = fine, 200 = not computed.
    """
    row: dict[str, Any] = {"family": family, "element": el}
    errors = {}
    for name, (value, err) in values.items():
        key = REINFORCEMENT_NAMES.get(name, name)
        if err == 0 or err in REINFORCEMENT_WARNINGS:
            row[key] = float(value)
        if err not in (0, REINFORCEMENT_NOT_COMPUTED):
            errors[key] = REINFORCEMENT_ERRORS.get(int(err), f"LIRA design error {-int(err)}")
    if len(row) == 2 and not errors:
        return None
    if errors:
        row["errors"] = errors
    return row


def _doubles(arr: Any) -> list[float]:
    return [float(arr.Item(i)) for i in range(arr.Count)]


def _scode(exc: Exception) -> int | None:
    """The error code of a COM exception: inside its exception info when it has one."""
    info = getattr(exc, "excepinfo", None)
    if info and len(info) > 5 and info[5]:
        return int(info[5])
    return getattr(exc, "hresult", None)


def _numbers(text: str) -> list[int]:
    return parse_numbers(str(text or "")) or []


def _items(numbers: Any) -> list[int]:
    """Node numbers of an answer, one by one. Never through AddToString: it corrupts the
    process's memory and takes the server down once the numbers are scattered enough
    (0xC0000374 at 3000 scattered nodes of a 228 000 node model; 1000 pass)."""
    return [int(numbers.Item(i)) for i in range(numbers.Count)]


def _shapes(
    infos: Any, wanted: list[int] | None, summary: bool = False
) -> list[tuple[int, list[tuple[int, int]]]]:
    """Per load case: its deformed shapes as (position from 1, mode number); with
    ``summary``, a dynamic load case's summary shape follows as (-1, -1)."""
    out = []
    by_number = {int(infos.Item(i).Number): infos.Item(i) for i in range(infos.Count)}
    for case in _cases(infos, wanted):
        info = by_number[case]
        modes = _numbers(info.DeformedShapes.AddToString()) or [1]
        shapes = list(enumerate(modes, start=1))
        if summary and int(info.SummaryShapeCount) > 0:
            shapes.append((SUMMARY_SHAPE, SUMMARY_SHAPE))
        out.append((case, shapes))
    return out


def _dynamic_cases(infos: Any) -> set[int]:
    """Load cases with mode shapes: dynamic even when only one shape was kept, so that
    their rows are labelled with the shape and no tool takes them for static ones."""
    return {
        int(infos.Item(i).Number)
        for i in range(infos.Count)
        if int(getattr(infos.Item(i), "DynamicModeShapeCount", 0) or 0) > 0
    }


def _shape_label(mode: int) -> int | str:
    return SUMMARY if mode == SUMMARY_SHAPE else mode


def _cases(infos: Any, wanted: list[int] | None) -> list[int]:
    available = [int(infos.Item(i).Number) for i in range(infos.Count)]
    if wanted is None:
        return available
    missing = sorted(set(wanted) - set(available))
    if missing:
        raise LiraInputError(f"No results for load cases {missing}; available: {available}")
    return [c for c in available if c in wanted]
