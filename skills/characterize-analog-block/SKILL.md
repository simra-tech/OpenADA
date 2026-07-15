---
name: characterize-analog-block
description: Plan and review backend-independent analog-block characterization through OpenADA semantic intents. Use when defining a characterization matrix for an amplifier, regulator, comparator, reference, oscillator, data converter, or other analog block; choosing the next analysis from application, topology, technology, specifications, and available evidence; coordinating stability, spectral-linearity, or PVT workflows; or separating simulation evidence from measurements, specification results, and signoff.
---

# Characterize an Analog Block

Act as the characterization coordinator. Turn the application and specifications
into the smallest justified sequence of OpenADA intents, then delegate focused
reviews to the other engineering skills. Use `$openada:openada` for capability discovery,
invocation, and result interpretation. Keep native design data, testbenches, PDKs,
models, and artifacts authoritative.

## Preserve the contract ladder

Keep these conclusions separate at every step:

| Layer | Exact semantic boundary | Strongest allowed conclusion |
|---|---|---|
| Analysis evidence | `openada.operation/circuit.simulate/v1alpha2` with `openada.assertion/simulation.evidence.valid/v1alpha1` | The requested OP, DC, AC, or transient analysis produced valid evidence, or conclusively did not converge |
| Measurement | `openada.operation/result.measure/v1alpha1` with `openada.assertion/measurement.valid/v1alpha1` | The declared metric was validly extracted from identified source evidence |
| Specification | `openada.operation/specification.evaluate/v1alpha1` with `openada.assertion/specification.satisfied/v1alpha1` | The declared measurement satisfies or violates the explicit limit under its recorded conditions |
| Signoff | Outside these three assertions | Only a separately qualified flow and accountable reviewer may make a signoff claim |

Never promote a lower layer into a higher one. In particular, simulation
`engineering.status: pass` is not a performance pass, and simulation
`engineering.status: fail` is the profile's defined solver failure rather than
a failed design specification.

## Freeze the design context

Create or update a compact context ledger before selecting analyses:

- **Decision:** application class, block role, design phase, topology, loop or
  sampled-data structure, and the immediate engineering question.
- **Technology:** exact PDK/model identity, process or device family, supply and
  body domains, temperature range, and pre-layout or extracted representation.
- **Conditions:** stimulus, source impedance, common mode, load, clocking,
  startup state, corner, and any enabled modes or paths.
- **Specifications:** metric, limit or interval, units, condition, priority,
  tolerance, and source of authority. Keep goals distinct from approved limits.
- **Evidence:** target and configuration identities, prior request/result IDs,
  artifact hashes, measurement definitions, and known provenance gaps.
- **Unknowns:** assumptions whose resolution could change the testbench,
  analysis, measurement, or conclusion.

Do not fill missing values with conventional numbers or substitute collateral.
Ask one narrow blocking question when multiple authoritative choices remain.

## Inspect capabilities before planning execution

Inspect the selected driver's complete operation, assertion, and feature IDs.
For circuit simulation, require the exact advertised analysis feature:

- `openada.feature/simulation.analysis.op/v1alpha1`
- `openada.feature/simulation.analysis.dc/v1alpha1`
- `openada.feature/simulation.analysis.ac/v1alpha1`
- `openada.feature/simulation.analysis.tran/v1alpha1`

Inspect the installed parameter schema before constructing either measurement
or specification requests. Use semantic metric names in the characterization
plan, but do not invent measurement-kind names, backend expressions, or feature
IDs that the installed profile does not define.

If an operation, feature, locator, or transport is unavailable, mark that row
**not evaluated — capability unavailable**. Do not bypass OpenADA with a native
command and then describe the result as contract evidence. A native executable
being installed does not itself make a semantic capability available.

## Build the characterization matrix

Read [references/application-recipes.md](references/application-recipes.md)
after identifying the application class. Adapt a recipe to the actual circuit;
do not force an unfamiliar block into the nearest label.

For every proposed metric, record:

| Field | Required content |
|---|---|
| Question | The design decision this metric informs |
| Testbench | Exact authoritative target and mode |
| Conditions | Supply, stimulus, load, clock, corner, and temperature |
| Analysis intent | One supported `circuit.simulate` analysis feature |
| Measurement | Semantic metric and source signals, units, interval, and method |
| Specification | Explicit bound and condition, or `not supplied` |
| Dependencies | Earlier gate or specialist skill that must complete first |
| State | planned, pass, fail, unknown, unavailable, or not evaluated at the appropriate layer |

Order the matrix by dependency, not by convenience:

1. Prove target, configuration, and baseline identity.
2. Run nominal OP evidence first and validly measure the bias/headroom facts
   needed by the topology.
3. Run DC sweeps needed to establish range, transfer, or regulation behavior.
4. Run application-specific AC or transient analyses only after their gates are
   meaningful.
5. Expand into specialist reviews and then PVT/statistical coverage only after
   nominal behavior is plausible.

Invoke one primary assertion per operation. Use fresh evidence destinations and
preserve lineage from analysis artifacts to measurements and specifications.

## Compose focused skills

- Invoke `$openada:analyze-feedback-stability` for differential, common-mode, nested,
  feed-forward, or regulator-loop questions after the DC gate.
- Invoke `$openada:analyze-spectral-linearity` for FFT, harmonic, SFDR, sampled-data,
  or waveform-linearity questions after transient evidence is valid.
- Invoke `$openada:assess-pvt-and-yield` only after nominal analyses, measurement
  definitions, and specification limits are frozen.

Do not duplicate their metric rules inside this coordinating workflow. Accept
their blocked or unknown result as a characterization state, not as an excuse to
skip the metric.

## Route each result honestly

| Observed state | Next action |
|---|---|
| Analysis evidence pass | Request only supported measurements bound to that exact evidence |
| Analysis evidence fail | Retain the conclusive solver evidence and diagnose that failure before interpreting performance |
| Analysis evidence unknown | Resolve the cited request, provenance, freshness, or structural gap and rerun the same intent |
| Measurement pass | Evaluate an explicit specification if one exists |
| Measurement fail or unknown | Repair the measurement definition or evidence; do not classify circuit performance |
| Specification pass or fail | Record the result only for its exact condition and limit |
| Any required capability unavailable | Preserve the gap as not evaluated and propose the smallest missing semantic primitive or capable driver |

## Report the next engineering decision

Return:

1. the frozen context and unresolved assumptions;
2. the ordered characterization matrix and coverage by contract layer;
3. per-run backend identity, exact profile/feature, result status, diagnostics,
   and artifact lineage;
4. measurements and specification results without collapsing their statuses;
5. application-specific open risks and unsupported primitives;
6. one smallest next experiment that can change the decision.

End with `signoff: not claimed` unless an external, explicitly qualified
signoff process has been supplied and reviewed. OpenADA evidence can support
that review; these skills do not confer signoff authority.
