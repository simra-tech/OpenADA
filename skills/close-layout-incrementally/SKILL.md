---
name: close-layout-incrementally
description: Close analog, mixed-signal, custom-digital, or top-level IC layout through small visually reviewed increments with fresh DRC, LVS, extraction, and performance evidence. Use when editing or debugging GDS, OASIS, Magic, KLayout, or custom-cell layout; placing matched or parasitic-critical devices; routing sensitive nets; repairing DRC or LVS markers; integrating hard macros; or diagnosing geometry that is difficult to understand from text reports alone.
---

# Close Layout Incrementally

Treat visual inspection as a required diagnostic loop and tool-native evidence
as the engineering gate. Use `$openada:openada` for capability discovery, DRC,
LVS, simulation, and normalized result interpretation. Keep the selected PDK,
rule deck, extraction setup, schematic, models, and native layout authoritative.

## Freeze one closure context

Record before editing:

- writable layout and immutable baseline hashes;
- exact cell and hierarchy boundary under review;
- authoritative PDK revision, layer map, DRC deck, LVS setup, extraction setup,
  models, schematic/netlist, and simulator startup chain;
- device intent, net intent, matching or symmetry constraints, current paths,
  sensitive nodes, and supplied DRC/LVS/parasitic/performance limits;
- known-good DRC, LVS, PEX, and simulation evidence for the baseline.

Do not mix collateral revisions to obtain a pass. If the authoritative process
context is ambiguous, stop before changing geometry and request that choice.

## Choose the smallest meaningful increment

Change one independently reviewable unit:

- one device or matched device pair;
- one contact or via array;
- one local route or critical-net segment;
- one guard-ring, well, tap, shield, or power connection;
- one block-boundary pin or one hard-macro integration edge.

Do not place or connect the whole block at once. Preserve the prior passing
checkpoint. State the expected connectivity and parasitic effect before the
edit, including the planned route and return path.

For matched or high-frequency structures, begin with the minimum meaningful
pair. Close that pair before adding bias, load, cascode, common-mode, or routing
structures. Repeat the same rule when joining completed blocks: connect one
interface or critical net, then reclose the hierarchy.

## Render before and after every edit

Produce all views needed to understand the increment:

1. full-cell context with hierarchy visible;
2. marker-centered zoom with coordinates and scale;
3. isolated device, cut, and adjacent conductor layers;
4. connectivity context one level above and below the edited cell;
5. before/after views at the same crop, layer visibility, and resolution.

Use `scripts/render_layout.rb` when KLayout is available. Run it in GUI-capable
headless mode rather than `-b`:

```bash
QT_QPA_PLATFORM=offscreen klayout -z \
  -r skills/close-layout-incrementally/scripts/render_layout.rb \
  -rd layout_path=/absolute/design.gds \
  -rd cell_name=BLOCK \
  -rd output_path=/absolute/evidence/block-via.png \
  -rd layers=68/20,68/44,69/20 \
  -rd box=8.2,0.4,9.9,1.4
```

Inspect the pixels directly. Check cut size and pitch, conductor enclosure,
overlap, gaps, unintended shorts, disconnected islands, pin access, well and
tap continuity, orientation, symmetry, common-centroid structure, dummy
devices, current crowding, route detours, shielding, and hierarchy transforms.

Visual reasoning proposes and localizes a diagnosis; it never proves DRC,
connectivity, extraction correctness, or performance.

## Gate the increment in order

Run only the next unproven gate and stop at its first regression:

1. **Geometry:** fresh cell-level DRC with the authoritative deck.
2. **Connectivity:** fresh cell-level LVS against the exact schematic/setup.
3. **Parasitics:** fresh extraction when the increment touches a critical,
   matched, clock, supply, bias, high-impedance, or high-current path.
4. **Performance:** rerun only the smallest simulation and explicit
   specification affected by the extracted change.
5. **Hierarchy:** repeat DRC/LVS and relevant PEX checks at the first parent
   boundary before adding another connection.

A DRC pass does not prove connectivity. An LVS pass does not prove parasitic or
performance closure. A PEX file does not prove a supplied budget. Keep
execution, engineering, measurement, and specification statuses separate.

If a gate fails, retain its markers and artifacts, render the failing region,
revert or repair only that increment, and repeat the same gate. Do not stack a
second layout change on an unexplained failure.

## Budget routing before it exists

For a parasitic-critical path, declare a provisional budget before placement:

- maximum route length, resistance, capacitance, coupling, or mismatch;
- permitted layers, width, spacing, shielding, via count, and symmetry;
- expected current and any electromigration or IR-drop requirement;
- observable performance metric and explicit condition/limit.

Use early trial routes and extraction after the first device or pair rather
than waiting for a complete block. Update the budget only from an authoritative
requirement or a recorded design decision; do not relax it merely because the
layout missed it.

## Keep an append-only checkpoint ledger

For every increment, record:

| Field | Required evidence |
|---|---|
| Baseline | layout/cell hashes and last passing checkpoint |
| Intent | one geometry/connectivity change and expected parasitic effect |
| Visual review | fixed-crop full, zoomed, and isolated-layer renders |
| DRC | exact deck/top/report identity and normalized status |
| LVS | exact schematic/setup/top/report identity and normalized status |
| PEX | extractor identity, extracted artifact hash, and changed RC metrics |
| Performance | exact test, condition, measurement, limit, and result |
| Decision | accept, repair, revert, or blocked, with one reason |

Never overwrite a passing checkpoint or reuse an evidence destination. A
change in PDK, deck, setup, model, schematic, hierarchy, or specification starts
a new comparison context.

## Report the next physical decision

Return:

1. the exact increment and frozen process context;
2. visual observations with view paths and marker coordinates;
3. separate DRC, LVS, PEX, and performance conclusions;
4. the smallest unexplained geometry or electrical risk;
5. one next increment or repair, not a whole-block rewrite;
6. any missing authority that prevents signoff interpretation.

End with `signoff: not claimed` unless an explicitly qualified signoff flow and
review authority have been supplied.
