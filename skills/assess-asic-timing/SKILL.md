---
name: assess-asic-timing
description: Assess synthesis-stage ASIC setup and hold timing with backend-independent OpenADA evidence. Use when validating a mapped netlist against a Liberty corner and SDC, reviewing WNS, TNS, unconstrained endpoints and worst setup/hold path summaries, diagnosing whether a timing miss is trustworthy, comparing one controlled timing experiment, or deciding the next closure action without presenting ideal-interconnect static timing as routed or signoff timing.
---

# Assess ASIC Timing

Evaluate one constraint-bound mapped-netlist timing model and recommend one
evidence-backed closure experiment. Use `$openada:openada` for execution and
normalized results. Keep native command construction and report parsing below
the semantic boundary.

## Scope the claim before running

Use exactly:

- `openada.operation/timing.analyze/v1alpha1`
- `openada.assertion/timing.constraints-satisfied/v1alpha1`
- `openada.feature/timing.setup-hold/v1alpha1`

The v1alpha1 operation is synthesis-stage static timing for one mapped netlist,
one Liberty, one SDC, one top, and ideal interconnect. It can establish whether
its complete declared setup/hold timing model has nonnegative slack. It does
not establish routed parasitics, extraction, slew/capacitance integrity,
clock-tree behavior, crosstalk/noise, IR drop, electromigration, OCV/AOCV/POCV,
MCMM coverage, asynchronous correctness, false/multicycle-path intent, or
foundry signoff.

When reviewing routed evidence produced outside this v1alpha1 operation,
distinguish a tool's raw `report_parasitic_annotation -report_unannotated`
driver count from a layout-aware filtered count. Ports, logical pad terminals,
and synthetic loads can be reported as unannotated even when their database
nets have no physical wire and therefore no routed RC to extract. Require the
exact reported identities and the flow's documented wire-presence filter. A
nonzero raw count with zero physically wired unannotated nets is not evidence
of missing routed parasitics; a physically wired unannotated net remains an
`unknown` timing-coverage gap.

## Freeze the timing manifest

Record:

- passing synthesis result-envelope path plus exact mapped-netlist path, byte
  count and hash;
- exact Liberty path/hash, PVT corner, units, operating condition, and library
  provenance;
- exact SDC path/hash and the source/owner of every clock, generated clock, IO
  delay, uncertainty, latency, transition, load, exception, and case analysis;
- top, analysis stage, ideal-interconnect limitation, and intended clock/path
  relationships;
- selected operation/feature, implementation and native product/version,
  evidence directory, result-envelope path, and expected artifact roles;
- explicit project acceptance criteria beyond zero slack, if any.

The current result envelope has no general request or result correlation field;
do not invent one. Bind lineage with the complete upstream envelope, exact
input/artifact hashes, operation profile, tool identity, and evidence paths.

Do not infer constraints from RTL names or substitute a typical corner, clock,
IO delay, exception, or Liberty. An SDC that parses is not proof that it
captures design intent. If ownership or intent is ambiguous, ask one narrow
question and stop.

The current connector accepts only the declarative `openada-sdc-v1` subset and
executes a fresh hash-identical `timing.sdc-snapshot`. It rejects sourced files,
procedures, loops, conditionals, environment access, arbitrary Tcl, and
`read_spef`. If a project generates constraints with those mechanics, preserve
that upstream provenance and obtain a reviewed flattened declarative SDC; do
not bypass the semantic command or silently drop constraints.

## Inspect capabilities and pass prerequisites

Inspect the closed profile and run the smallest scoped preflight:

```bash
openada profile show openada.operation/timing.analyze/v1alpha1
openada doctor --project-root /absolute/project \
  --assertion timing-constraints-satisfied
```

Preflight proves only point-in-time readiness. Require a passing
`openada.operation/logic.synthesize/v1alpha1` result, exact artifact binding,
and the same Liberty identity unless the timing manifest explicitly records a
reviewed compatible library. If synthesis is fail/unknown, the netlist hash is
missing, or any required timing input is unresolved, stop before STA.

If the timing operation, feature, implementation, or tool is unavailable,
report **not evaluated — capability unavailable**. Do not run a raw timing
script and call it equivalent evidence.

## Invoke one semantic timing analysis

Use only the frozen inputs:

```bash
openada timing-analyze evidence/synthesis/mapped.v \
  --top top \
  --liberty pdk/cells.lib \
  --sdc constraints/top.sdc \
  --output-dir evidence/timing
```

Do not add hidden startup files or repair missing constraints inside the
generated run. Create a new identified experiment when any input changes.

Read `execution.status` separately from `engineering.status`. Verify operation
and assertion profile IDs in `data.protocol`; the fixed `analysis_model` that
selects the advertised feature; implementation and native version; input
hashes; units; top; `sdc_policy`, safe-subset validation, snapshot/input hash
equality; `environment_policy=closed-opensta-runtime-v1` and stable tool
identity; constraint-check status; unconstrained-endpoint evidence;
setup and hold WNS/TNS; the one normalized worst-path summary for each analysis;
retained script, transcript, setup report, hold report and constraint report;
freshness; and artifact hashes.

## Gate evidence before interpreting slack

Apply this order:

1. Require successful input binding, design linking, complete capture, and
   structurally valid bounded reports.
2. Require the operation's constraint-completeness checks, including no
   unresolved setup diagnostics or unconstrained endpoints.
3. Only then interpret normalized setup and hold WNS/TNS and the two worst-path
   summaries.
4. Compare against a project specification only when its numeric limits,
   units, corner, mode, and stage are explicit.

An engineering timing `fail` can be conclusive evidence of negative slack; an
engineering `unknown` is not a timing miss and not a pass. A zero-slack pass is
only the built-in constraint assertion. Extra guard band, frequency, latency,
path-group, or stage requirements remain **not evaluated** unless an installed
semantic contract evaluates them.

Route status as follows:

| Result | Meaning | Action |
|---|---|---|
| engineering `pass` | Complete declared ideal-interconnect setup/hold evidence has nonnegative worst slack | Report this stage/corner only; identify unmodeled closure work |
| engineering `fail` | Complete evidence proves negative setup or hold slack under the frozen model | Use the applicable worst-path summary to form one bounded hypothesis and run one causal experiment |
| engineering `unknown` | Linking, constraints, capture, reports, units, paths, or provenance are incomplete | Stop every timing conclusion; repair the cited evidence gap |
| invalid request | A required input or identifier is malformed, missing, or unsafe | Correct the manifest without weakening intent |
| unavailable/timeout/invocation failure | No timing assertion was evaluated | Restore the same capability and rerun |

## Diagnose like a closure engineer

For valid pass/fail evidence, separate setup and hold and preserve the authority
of each observation.

Treat these fields as **normalized evidence**:

- global setup and hold WNS/TNS in SI seconds;
- `setup.path_count` and `hold.path_count`, which count records in each bounded
  retained JSON report only;
- one `critical_path` summary per analysis, containing only startpoint,
  endpoint, path group, and slack;
- constraint-completeness, input-stability, report-completeness, unit, model,
  and cross-format agreement fields.

The bounded `path_count` is not a violating-endpoint count, a complete path
inventory, or a ranking depth. Global TNS is independent of that retained
subset and cannot be reconstructed from it. The normalized envelope does not
expose multiple ranked paths, a path-type field, per-arc delay, fanout/load
breakdown, or failure clustering. Do not manufacture those claims by reparsing
the native reports into a second public result.

Read the authoritative SDC, RTL, mapped netlist, and synthesis statistics only
as **source/artifact inspection**. Use the normalized worst-path endpoints and
path group to focus that inspection: check the applicable clock and exception
intent, IO budget, surrounding logic structure, and mapping choices. Label any
proposed cause—mux depth, arithmetic structure, control fanout, macro choice,
or constraint error—**inferred** until one controlled experiment evaluates it.

Do not assume the reported worst path identifies the root cause. Do not relax a
clock, add a false/multicycle path, or change uncertainty merely to obtain a
pass. Constraint changes require design-owner justification. Do not propose a
hold fix from a setup-only report or extrapolate ideal-interconnect slack to
routed closure.

### Preserve routed electrical-check authority

When a later physical-design flow supplies native max-fanout, capacitance, or
slew reports, treat them only as **source/artifact inspection** because the
current timing operation does not normalize routed or post-ECO evidence.
Preserve these controls even during explicitly authorized native capability-gap
work:

- keep an optimization target distinct from the authoritative library or
  project acceptance limit, and evaluate the final report against the latter;
- rerun electrical checks after antenna repair, hold repair, buffering, or any
  other stage that can add loads or alter topology;
- retain results by explicit corner and check type; an aggregate zero cannot
  override a nonzero corner report, and conflicting or stale summaries remain
  unknown;
- bind each conclusion to the exact post-change netlist/layout state and report
  hashes rather than carrying forward a pre-change pass.

For a package-facing pad violation, first establish the Liberty precedence
chain: the global default, any cell value, any pin override, the reported pin
direction and function, and the characterized timing or three-state arc at
every evaluated corner. A violation that remains with zero external load while
the applicable characterized arc itself exceeds an inherited global default is
a candidate library-collateral ambiguity requiring owner clarification, not by
itself proof of a defective circuit or an acceptable interface. Preserve the
exact instantiated pad-cell types and reported endpoint identities before
proposing a design change.

An explicitly authorized diagnostic Liberty overlay must start from a
hash-frozen pristine library and modify only the named property on the named
pin of explicitly named instantiated cells. Assert the exact edit cardinality
in every corner file and prove that there is no other diff; never patch an
installed PDK in place. Hold the netlist, routed database, extracted
parasitics, SDC, tool, and report policy fixed, then rerun every affected corner
and retain max-slew, capacitance, fanout, unconstrained-endpoint, setup, and
hold results independently. If the targeted violations clear, that establishes
threshold causality only. It does not make the hypothesized value
authoritative, waive residual checks, pass an engineering gate, or establish
package suitability. Retain the original-library result beside the overlay
result. Acceptance still requires the applicable PDK, pad, package,
submission-program, or foundry authority.

Treat SDF gate simulation as separate corroborating evidence, not a substitute
for complete STA. Require the simulator to support every emitted SDF construct,
the cell models' specify behavior, and the testbench access mechanism. If SDF
interconnect creation makes VPI enumeration abort, timing checks are reported
unsupported, paths cannot be matched, or annotation coverage is unmeasured,
classify the simulation as **unknown — harness capability gap**. Preserve the
exact netlist, SDF, corner, command, transcript, and failure signature; never
translate simulator startup or annotation failure into either a design fail or
a timing pass.

Do not call these reports normalized OpenADA timing evidence or use a tightened
optimization target as a waiver. Route recurring needs to a future bounded
physical-timing/electrical-check operation instead of widening v1alpha1 in
skill prose.

## Choose one controlled experiment

Select the smallest experiment that distinguishes one hypothesis:

- restructure or pipeline one source-inspected logic region associated with the
  normalized endpoint, then rerun lint, structural checks, synthesis, and
  timing;
- change one reviewed mapping target/policy, then rerun synthesis and timing;
- correct one demonstrably wrong constraint with provenance and review; or
  investigate an unsupported domain with the smallest missing semantic
  capability.

Hold all unrelated manifest fields fixed. Preserve before/after input and
artifact hashes. A comparison is invalid when netlist, Liberty, SDC, top,
corner, tool/implementation, or analysis stage changes without being the
declared experimental variable.

## Stop boundaries

Stop before slack interpretation on unknown evidence or incomplete constraints.
Stop before project-specification satisfaction when only the built-in zero-
slack assertion was evaluated. Stop before physical/signoff claims because v1
has ideal interconnect and no MCMM/variation closure. Do not mutate RTL,
libraries, or SDC without separate authorization.

## Report

Return the frozen timing manifest; synthesis lineage; execution and engineering
statuses; operation, assertion, feature, implementation and native version;
constraint-completeness state; setup/hold WNS and TNS with units and critical
path summaries; bounded report-record counts labeled as neither violating-path
counts nor rankings; findings labeled normalized evidence/source-artifact
inspection/inferred; input/artifact hashes; specification coverage and
unmodeled risks; and one smallest controlled next experiment. Finish with
`signoff: not claimed`.
