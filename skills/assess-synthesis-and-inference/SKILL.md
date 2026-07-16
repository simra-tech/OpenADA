---
name: assess-synthesis-and-inference
description: Assess ASIC synthesis, technology mapping, and inferred hardware with backend-independent OpenADA evidence. Use when reviewing whether RTL produced a complete Liberty-mapped netlist, investigating unexpected register, latch, memory, arithmetic, or mux structures, comparing controlled synthesis experiments, or deciding whether a netlist is trustworthy enough for static timing without confusing successful mapping with functional equivalence or specification closure.
---

# Assess Synthesis and Inference

Turn one frozen RTL configuration into reviewable mapped-netlist evidence. Use
`$openada:openada` for semantic operations and normalized results. Keep native
invocation mechanics below the contract and keep the RTL, Liberty, technology
maps, constraints, and retained netlist authoritative.

## Separate the claims

Use these exact layers:

- Review strict lint through `openada.operation/rtl.lint/v1alpha1` and
  `openada.assertion/rtl.lint.clean/v1alpha1` when cleanliness is required.
- Produce mapping evidence through
  `openada.operation/logic.synthesize/v1alpha1` and
  `openada.assertion/synthesized-netlist.valid/v1alpha1`.
- Treat timing as a later, separate
  `openada.operation/timing.analyze/v1alpha1` result.

A synthesis pass establishes a fresh, structurally validated, fully
Liberty-mapped netlist under the recorded policy. It does not establish RTL-to-
netlist equivalence, simulation behavior, CDC/RDC safety, DFT readiness,
clock-tree feasibility, routed timing, power, area-target satisfaction, or
silicon correctness. Keep each absent primitive **not evaluated**.

## Freeze a reproducible synthesis manifest

Record:

- repository revision, ordered RTL sources, conservatively captured include
  dependencies, unresolved-literal-include disclosure, and hashes;
- top, frontend, declared language dialect/revision, include directories,
  defines, generated
  sources, and black-box assumptions;
- exact Liberty and technology-map identities and hashes, selected PVT corner,
  cell exclusions, and library units;
- mapping policy, flattening policy, undefined-value policy, delay target and
  native constraint file when supplied;
- upstream lint/structural envelope paths, artifact hashes, and statuses;
- selected operation/feature, implementation and native product/version,
  evidence directory, result-envelope path, and expected output roles.

Do not substitute a convenient Liberty, corner, macro model, technology map,
or top. The current result envelope has no general request or result correlation
field, and the v1alpha1 synthesis surface has no module-parameter override. Do
not invent an identifier or silently use default parameters when the intended
configuration requires an override. Do not compare runs with different ordered
sources, captured dependencies, defines, or policies as if they were the same
experiment. If a required file or design-intent choice is ambiguous, ask one
focused question and stop.

## Inspect and preflight the semantic capability

Require:

- `openada.operation/logic.synthesize/v1alpha1`
- `openada.assertion/synthesized-netlist.valid/v1alpha1`
- `openada.feature/synthesis.asic-liberty/v1alpha1`

Inspect the installed closed schema before constructing the request:

```bash
openada profile show openada.operation/logic.synthesize/v1alpha1
openada doctor --project-root /absolute/project \
  --assertion asic-netlist-synthesized
```

Preflight does not inspect RTL or prove mapping. If the operation, feature,
implementation, frontend, or tool is unavailable, report **not evaluated —
capability unavailable**. Do not replace it with a raw synthesis script.

## Pass the RTL gate

Require an explicitly reviewed source configuration, stable captured source and
dependency hashes, and a successful structural result before using a mapped
netlist for downstream decisions. Also require strict lint pass when the
project's readiness definition requires diagnostic cleanliness.

Include capture is conservative rather than exact conditional-preprocessor
closure. Review `data.unresolved_literal_includes` against the declared defines;
label that determination **source inspection**. If a possibly active dependency
cannot be bound, or the intended configuration needs an unsupported parameter
override, keep synthesis readiness not evaluated.

A warning-only strict lint failure may motivate a diagnostic synthesis only
when the user explicitly accepts that limited purpose. It cannot be waived
into RTL readiness or synthesis closure. Stop on lint errors, unknown lint,
unknown structural evidence, unresolved top/configuration, or an unexplained
black box.

## Invoke synthesis through the intent

Use only the semantic command and declared options. For example:

```bash
openada synthesize rtl/pkg.sv rtl/top.sv \
  --top top \
  --liberty pdk/cells.lib \
  --frontend slang \
  --language 1800-2017 \
  --include-dir rtl/include \
  --define ASIC \
  --techmap pdk/cells_latch.v \
  --dont-use 'CLKGATE_*' \
  --abc-delay-target-ns 2.2 \
  --output-dir evidence/synthesis
```

Include only options present in the frozen manifest. Supply an ABC constraint
file only when its role, units, and provenance are understood. Never tune a
target repeatedly and report only the favorable run.

Use `--language yosys-sv` with the built-in `verilog` frontend. Treat it as a
tool dialect, not an IEEE edition. The `1800-2017` and `1800-2023` selectors
are valid only with `--frontend slang`; they request that language edition but
do not certify tool or design conformance to IEEE 1800.

Read `execution.status` separately from `engineering.status`. Validate the
operation and assertion profile IDs in `data.protocol`, implementation and tool
version, `data.abc_tool` path/version/bytes/SHA-256,
`data.abc_tool_identity_stable`, the closed `data.environment_policy`, bound
source/dependency records, unresolved-include disclosure,
mapping policy, artifact freshness and hashes, and bounded diagnostics before
using statistics or the mapped netlist.

Scoped preflight inspects the primary synthesis connector. The synthesis result is
the authoritative external-ABC gate: `abc.missing`, `abc.unusable`, or
`abc.changed` means the mapping assertion was not established. When a project
pins non-default tool locations, bind both explicitly with `--tool-path
yosys=/absolute/yosys-bin --tool-path abc=/absolute/yosys-abc`; do not let ambient
`PATH` select the mapper.

Route the contract result:

| Result | Meaning | Action |
|---|---|---|
| engineering `pass` | The retained output is a complete validated Liberty-mapped netlist under the frozen policy | Review inference/mapping evidence; pass the exact artifact to timing if needed |
| engineering `fail` | Complete evidence proves a recognized elaboration, synthesis, or mapping failure | Diagnose the first causal failure; do not use partial output |
| engineering `unknown` | Execution, capture, dependency, statistics, JSON, mapping, or provenance evidence is incomplete | Stop all netlist, area, and timing-readiness claims; repair and rerun |
| invalid request | Inputs or policy are malformed, missing, or unsafe | Correct the manifest without weakening intent |
| unavailable/timeout/invocation failure | No synthesis assertion was established | Restore the same capability and rerun |

## Review inference and mapping like a senior designer

For a passing result, separate contract facts from design review. Treat
normalized `inference_stats`, mapped `stats`, `mapping_complete`,
`unmapped_cell_types`, Yosys/ABC identity and environment policy, mapping
policy, and input/artifact hashes as **normalized
evidence**. Reconcile at least:

- process and memory counts before and after mapping;
- sequential cell count and area;
- cell histogram, total area, sequential area, and cells excluded by policy;
- unexpected internal or Liberty-absent cell types and whether the mapping
  completeness assertion is actually true.

Then read the authoritative RTL and retained mapped netlist/structure only as
**source/artifact inspection**. Use that inspection to compare intended versus
realized latch, register, memory, arithmetic, mux, priority, decode, constant
propagation, state-removal, and interface structure. Do not present those
review findings as normalized facts when they are absent from `data`. Label a
causal explanation **inferred** until a focused semantic check or controlled
experiment supports it.

Treat these as structural observations, not proof of behavioral equivalence.
The v1alpha1 mapping policy explicitly sets undefined values to zero before
technology mapping; record that choice and flag any reset, safety, or
X-propagation argument that depends on four-state behavior.
An absent internal/unmapped cell is necessary for this mapping assertion but
does not prove that an intended SRAM, multiplier, clock gate, or other macro
was inferred correctly. Compare against explicit architectural expectations;
label unexplained differences **inferred risk** and propose a focused check.

Do not turn an area or cell-count observation into a specification pass. This
release has no typed synthesis-statistics specification evaluator. Do not
derive power from area or timing from a synthesis delay target.

## Control experiments and handoff

Change one variable per experiment: RTL structure, inference template,
technology map, exclusion policy, delay target, or constraint file. Preserve
the source/configuration diff and every result. Compare statistics only when
all other manifest fields and library identities match; otherwise report the
pair as non-comparable.

Hand off only the complete passing synthesis result envelope, the exact mapped-
netlist artifact and hash, and the exact Liberty/constraint identities. Use
`$openada:assess-asic-timing` for constraint-bound timing. If the RTL or mapping
policy changes, invalidate the timing result and rerun synthesis first.

## Stop boundaries

Stop before using any netlist when mapping evidence is fail or unknown, any
artifact binding is missing, a possibly active dependency is unresolved, or
unexpected black boxes/unmapped cells remain. Stop before claiming functional
preservation without a separate equivalence capability. Stop before declaring
area, power, timing, DFT, or physical closure without their explicit contracts
and project limits. Do not modify RTL, constraints, libraries, or maps without
separate authorization.

## Report

Return the frozen synthesis manifest; upstream gate statuses; execution and
engineering statuses; operation, assertion, feature, implementation and native
version; input and artifact hashes; inference/mapped statistics; mapping
completeness; findings labeled normalized evidence/source-artifact
inspection/inferred; unsupported claims; comparability limits; and one smallest
next experiment. Finish with `signoff: not claimed`.
