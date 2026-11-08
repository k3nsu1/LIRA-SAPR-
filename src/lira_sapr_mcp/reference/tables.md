# LIRA-SAPR input tables, FE types and text format

For LIRA-SAPR 2024 R2. The columns come from LIRA's help (window "Таблицы ввода", which
these tables mirror) and were checked against LIRA's example models: "verified" means
the server's tests wrote or read it, "help" means the help states it and the examples
match. The help does not give units per column or how every cell is encoded: before
writing a table you have not written before, read it from a model that has such data
(`lira_read_table`) and copy its form.

## How tables work

- A row is a list of cells; the first cell is the node or element number (or object
  number: stiffness, load case). Empty cells are null; trailing nulls may be left out.
- Values use LIRA's current input units: see `units` in `lira_model_summary`. Angles
  are in degrees.
- Some tables hold numbers as text (`Nodes_Restraints`, `DCL`, `CollectionOfMasses`):
  write them as text too, as read.
- `lira_write_table` without `numbers` replaces the whole table (new numbers add
  objects); with `numbers` it replaces only those rows. LIRA checks the rows and
  reports errors with 0-based row and column; on any error nothing changes.
- Rows cannot delete elements: LIRA ignores a whole-table write without them.
- There is no table for loads: use `lira_read_loads` / `lira_set_loads`, or build a
  model in the text format (below).
- Some tables carry a parameter LIRA's window sets (the subtask C or D set of
  foundation coefficients and kE, the DCL table number, the design variant of
  materials). The server creates its tables without parameters, so LIRA's defaults
  apply (presumably the main subtask, the first table, the first variant).

## Model geometry and stiffness

| Table | Columns | Status |
|---|---|---|
| `Nodes_Coordinates` | node, X, Y, Z | verified |
| `Elements_TypeAndNodes` | element, FE type, nodes as text `"1, 4"` | verified |
| `Nodes_Restraints` | node, X, Y, Z, UX, UY, UZ, W (warping, model type 6): `"1"` = fixed, null = free | verified (first 7) |
| `Elements_Stiffnesses` | element, stiffness number | verified |
| `Stiffnesses` | number, section type code (below), colour `#rrggbb`, comment, parameters as text `"Ro:2.75 E:3e+006 B:60 H:40 ..."`, then nonlinear law and record numbers and reinforcement parameters (null for linear) | help; write new ones with `lira_copy_stiffness` or the text format |
| `Stiffnesses_Factors` | element, kE: a factor to the element's elastic modulus (1 = none). kE = 2 halves the element's own deformations | verified |
| `Elements_Materials` | element, material code (`RC` reinforced concrete, `M` metal, `B` masonry), component numbers joined by dots (for RC `type.concrete.rebar`, e.g. `1.2.1`); null = none | help |
| `Elements_DesignSectionsCount` | element, number of design sections (bars: default 2, start and end; odd counts recommended; RC beams at least 3; plates 1, or 4 / 5 for forces at nodes) | verified (bars) |
| `Bars_LocalAxes` | element, rotation angle of the section about X1 (counter-clockwise looking from the end of X1) | help |
| `Plates_LocalAxes` | element, angle of the axes that results follow ("согласованные оси") | help |
| `Plates_AxesOfOrthotropy` | element, angle of the orthotropy axes | help |
| `Solids_LocalAxes`, `Solids_AxesOfOrthotropy` | element, then X, Y, Z of the unit vector X1 and of Y1 in global axes (Z1 by the right-hand rule) | help (6 cells seen) |
| `Nodes_LocalAxes` | node, X, Y, Z of a point K (X1 runs from K to the node), angle of Y1 and Z1 about X1 | help |
| `Bars_Offsets` | element, AX1, AY1, AZ1 at the start, AX2, AY2, AZ2 at the end (rigid offsets in the bar's local axes), `+` / `-`: whether the offsets turn with the bar's angle | help |
| `Plates_Offsets` | element, offset along the plate's Z1 (same at all nodes; 0 removes it) | help |
| `Bars_Hinges` | element, then hinge stiffness at the start for X1, Y1, Z1, UX1, UY1, UZ1, W, then the same 7 at the end. Null = no hinge, `0` = a free hinge | help |
| `CoupledDOFs` | group number, block number, flags `1` for X, Y, Z, UX, UY, UZ, W (null = not coupled), the nodes of the group | help (not in the examples) |
| `PRBs` | rigid body number, kind as text (`Все степени свободы` = all degrees of freedom), master node, slave nodes as text `"22 23 24 "` | help + examples |

## Elastic foundation

Values belong to the main subtask's set C.

| Table | Columns | Status |
|---|---|---|
| `Plates_C1C2` | element, C1z, C2z, Pz (load on the soil), Pz group number, [Rz] (limit soil resistance), kind of work | help (7 cells seen) |
| `Bars_C1C2` | element, C1z, C2z, `Bc=B` flag (null = no), Bc (foundation width along Y1), C1y, C2y, Hc (width along Z1), Pz, Pz group, [Rz], [Ry], kind of work | help (13 cells seen) |
| `Special_C1C2` | element of FE type 53 or 54, FE type, C1z, C2z, angle Fi of the soil zone (FE 54) | help (not in the examples) |

Kind of work: `0 - Линейная` (linear), `1 - Односторонняя` (one-sided), `2 -
Односторонняя + [R]`; one-sided work needs a nonlinear analysis. A zero Bc or Hc turns
the foundation off.

## Load cases and combinations

| Table | Columns | Status |
|---|---|---|
| `LoadCases` | number, name (up to 50 characters), subtask as text (`"1. Основная задача: C1. Основная задача; D1. Основная задача"`), kind (`Постоянное(П)`, `Длительное(Д)`, `Кратковременное(К)`, `Мгновенное(М)`, `Особое ...`; other norms have their own list), formula of a combined load case | verified (number, name, kind) |
| `DCL` | combination table (РСН): row 1 holds combination names and parameters (empty without combinations); then per load case, all as text: number, name, kind, null, sign alternation `+` or `+/-`, mutual exclusion group (`0` = none, up to 9), safety factor γf, long-term part, then a coefficient per combination | help (9 cells seen) |
| `CollectionOfMasses` | masses from static load cases, all as text: row 1 `Номера динамических загружений:` and a dynamic load case per column (`"4.Загружение 4"`); then per static load case `"1.Загружение 1"` and its factor to mass in each dynamic column | help |
| `AssemblageStages` | stage number, elements mounted, elements removed (as text `"1-16 32 48-54"`) | help |
| `Bars_InternalForces` | load case, superelement (`1` = the main model), FE type, element, section, N, Mx, My, Qz, Mz, Qy, Ry, Rz, Bw — forces from the analysis, editable as input | help (not in the examples) |

The text format keeps none of DCL, materials and design data: they live only in the
`.lir` file.

## Drawing aids

| Table | Columns | Status |
|---|---|---|
| `StructuralBlocks` | elements as text `"1-1080 1889-1924"`, kind (`Блок`, `КОЛОННА`, `БАЛКА`, `ПЛИТА`, `СТЕНА` or own text), ID (unique), floor, mark, comment, colour; an element belongs to one block | help |
| `ConstructionAxes` | mark as text, X1, Y1, X2, Y2, angle | help |
| `ElevationMarks` | Z level, side `Left` / `Right` | help |
| `NonlinearDiagrams` | law number, record number, parameters as text `"NLL_Comment:B25 NLL_FltParams:-0.0035, -2494.86, ..."` | help (parameter format not described) |

## Section type codes (column 2 of `Stiffnesses`)

- Standard sections: 0 rectangle (`B`, `H`), 1 T-section flange up (`B`, `H`, `B1`,
  `H1`), 2 T-section flange down, 3 I-section, 4 channel, 5 box, 6 ring, 7 beam,
  47 cross, 48 angle, 49 / 50 asymmetric T-sections.
- 36 plate (`H` thickness, `E`, `V` Poisson's ratio, `Ro` specific weight), 37 solid.
- Steel catalogue profiles (`File`, `Shape`): 1001 rolled I-beam, 1002 welded I-beam,
  1003 channel, 1004 box of channels, 1007 angle, 1014 pipe, 1018 rectangular tube and
  others up to 1075.
- 35, 54, 64 sections from КС-САПР, КТС-САПР and the section designer; 65-75
  steel-concrete sections.
- Numeric stiffness of special elements: 17 FE 51, 20 FE 55, 55 FE 56, 40 FE 60,
  38 / 39 FE 261 / 262, 57 / 58 FE 251 / 252, 59 FE 255-256, 60 FE 265-266, 76 dampers
  (FE 62, 65, 66), 77 joints (FE 58, 59, 258, 259). FE 56 (verified): parameters
  `"Rx:200 Ry:200 Rz:0 Rux:0 Ruy:0 Ruz:0 DD56_END"`, in the text format `4 200 200 0 0 0 0/`.
  FE 51 (verified): one direction `n` (3 = Z) and its stiffness, `"EF:1000 n:3 DD51_END"`, in the
  text format `4 3 1000/`.

## Finite element types

| FE type | Meaning | Nodes | Stiffness |
|---|---|---|---|
| 1, 2, 3, 4 | bar of a 2D truss, 2D frame, grillage, 3D truss | 2 | sections |
| 10 | universal space bar (default for bars) | 2 | sections |
| 11, 12 | rectangular, triangular plate (bending) | 4, 3 | plate (36) |
| 15, 16, 17, 19 | thick plates, quadrilateral plate | 4, 3, 4, 4 | plate (36) |
| 21, 22, 30 | plane stress: rectangular, triangular, quadrilateral (wall-beam) | 4, 3, 4 | plate (36) |
| 23, 24, 27 | universal plane stress | 4, 3, 4 | plate (36) |
| 31, 32, 33, 34, 36 | solids: parallelepiped, tetrahedron, prism, 6- and 8-node | 8, 4, 6, 6, 8 | solid (37) |
| 41, 42, 44 | universal shell: rectangular, triangular, quadrilateral | 4, 3, 4 | plate (36) |
| 45, 46, 47 | thick shells | 4, 3, 4 | plate (36) |
| 51 | one-node elastic support | 1 | numeric (17) |
| 53, 54 | elastic foundation outside the slab contour | 2, 1 | `Special_C1C2` |
| 55, 56 | elastic links between two nodes, at one node | 2, 1 | numeric (20, 55) |
| 57 | single pile | 1 | pile stiffness (stiffness number 0 in `Elements_Stiffnesses`) |
| 2xx, 3xx, 4xx | nonlinear analogues (e.g. 210 bar, 241-247 shells, 251-266 one-sided links) | | |
| 15xx | heat conduction (model type 15) | | |

`elements_by_fe_type` in `lira_model_summary` shows which types a model uses. A
stiffness must fit the FE type: LIRA rejects, for example, a plate stiffness for a bar
("Тип жесткости не допустим для типа КЭ").

Model types (`model_type`, degrees of freedom per node): 1 2D truss or wall-beam in XOZ
(X, Z), 2 2D frame in XOZ (X, Z, UY), 3 grillage or plate in XOY (Z, UX, UY), 4 3D
truss (X, Y, Z), 5 general 3D (X, Y, Z, UX, UY, UZ), 6 thin-walled bars (5 plus warping
W), 15 heat conduction (temperature).

## Text format

`lira_import_text_model` takes it; `lira_export_text` shows any model in it. Documents
`( N/ entry/ entry/ ... )`, each starting a line. LIRA's help does not describe this
language; the rules below are the server's verified findings. A verified 6 x 3 m portal
frame:

```text
( 0/ 1; FRAME/ 2; 5/ 33; M 1 CM 100 T 1 C 1/ )
( 1/ 10 1 1 3/ 10 1 2 4/ 10 1 3 4/ )
( 3/ 1 S0 3E6 40 60/ 0 RO 2.5/ )
( 4/ 0 0 0/ 6 0 0/ 0 0 3/ 6 0 3/ )
( 5/ 1 1 2 3 4 5 6/ 2 1 2 3 4 5 6/ )
( 6/ 3 6 3 1 1/ )
( 7/ 1 10/ )
```

- 0: `1; name/` the model name, `2; type/` the model type, `33; .../` the units of the
  text; keep `33; M 1 CM 100 T 1 C 1/` (m, section sizes in cm, t, degrees C) so that
  the values below read as described.
- 1: elements in order, `FE-type stiffness node node .../`.
- 2: hinges; 8: design combinations (DCL); 13: dynamic loads (names from the help;
  formats not verified).
- 3: stiffnesses, `number S0 E B H/` a rectangular section (E in t/m2, B and H in cm);
  `0 RO weight/` the weight per length (t/m) for the stiffness above: `RO 2.5` with a
  40 x 60 cm section becomes `Ro:10.4167` (t/m3) in the `Stiffnesses` table.
  Self weight (`lira_self_weight`) uses it; it is a load of type 16 in document 6.
- 4: node coordinates in order, `X Y Z/`.
- 5: restraints, `node direction direction .../` with directions 1-6 = X Y Z UX UY UZ.
- 6: loads, `target type direction row case/`: type 0 is a nodal load (target is a
  node), type 6 a uniform load on a bar; direction 3 = global Z, a positive value acts
  downward; `row` points to document 7.
- 7: load values, `row value value .../`.
