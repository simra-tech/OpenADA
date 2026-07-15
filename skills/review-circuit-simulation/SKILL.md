---
name: review-circuit-simulation
description: Review circuit-simulation evidence and recommend the next engineering action through OpenADA's backend-independent circuit.simulate contract. Use when assessing a SPICE operating-point, DC, AC, or transient run; deciding whether simulation evidence is trustworthy; comparing normalized evidence across supported open-source simulators; separating simulator completion from circuit-specification satisfaction; or routing an inconclusive simulation without relying on ngspice- or Xyce-specific commands.
---

# Review Circuit Simulation

Use OpenADA's shared circuit-simulation operation as the execution boundary.
Reason about the engineering question above that boundary, without teaching the
workflow a simulator-specific CLI or log grammar.

## Frame the decision

State the exact question before running anything. Keep these claims separate:

1. **Execution:** Did the selected simulator run to completion?
2. **Evidence validity:** Did the run produce fresh, structurally valid,
   finite evidence for the requested analysis under the shared operation profile?
3. **Measurement:** What values can be extracted from that evidence?
4. **Specification:** Do those measurements meet explicit limits?

The simulation profile can establish the first two claims. Compose the separate
typed measurement and specification operations only when their exact features
are available. Require explicit measurement definitions, limits, units,
corners, and model context before judging a design specification. Never upgrade
valid simulation evidence into “the circuit works.”

## Check applicability

Use this skill directly only when the request fits
`openada.operation/circuit.simulate/v1alpha2`: one self-contained top-level OP,
DC, AC, or transient analysis with parseable closed parameters and no includes,
measurements, print directives, control blocks, or additional analyses. Require
`openada.feature/simulation.analysis.<type>/v1alpha1` from the selected driver;
Xyce does not advertise OP.

If the design falls outside that subset, report which construct is unsupported
and route the task to a suitable OpenADA operation when one exists. Do not
silently widen the shared profile or fall back to a raw native command while
claiming comparable OpenADA evidence.

Identify the exact netlist and a fresh task-local evidence directory. Treat
design files, models, and PDK collateral as read-only. Do not substitute a
model, corner, library, or testbench to obtain a passing run.

## Select the backend explicitly

Honor a backend named by the user. Otherwise inspect available capabilities,
select a supported open-source backend deterministically, and state the choice.
Do not retry through a second backend merely because the first produced an
engineering failure. A comparison across backends is a separate, explicit
request.

Run the same semantic operation through the selected backend:

```bash
openada simulate /absolute/path/to/testbench.cir \
  --backend ngspice \
  --output-dir /absolute/path/to/evidence
```

Use `--backend xyce` with the same shape when Xyce is the selected capability.
Resolve the `openada` executable through the plugin's OpenADA execution skill.
If OpenADA or the requested backend is unavailable, report it as unavailable;
do not manufacture an engineering conclusion from an alternate tool.

## Interpret normalized evidence first

Read these result fields before opening native logs:

- `execution.status` for invocation and process completion;
- `engineering.status` for the fixed simulation-evidence assertion;
- `data.protocol`, `data.analysis`, and `data.evidence` for normalized facts;
- `diagnostics` for bounded failure or uncertainty routing;
- `inputs`, `artifacts`, and `provenance` for source and evidence identity.

Route the result as follows:

| Result | Engineering interpretation | Next action |
|---|---|---|
| execution completed, engineering pass | Fresh finite requested-analysis evidence is valid | Extract only explicitly requested measurements, or define the next assertion |
| execution completed, engineering fail | Valid evidence supports the profile's engineering failure | Diagnose the reported convergence or waveform condition; do not switch tools automatically |
| engineering unknown | Evidence is absent, stale, malformed, incomplete, or uninterpretable | Resolve the cited evidence or provenance gap and rerun into a fresh directory |
| invalid request | The target is outside the shared profile or malformed | Correct the request without weakening the intended assertion |
| backend unavailable, timeout, or invocation failure | No engineering conclusion is supported | Repair capability or execution state, then rerun the same intent |

Treat native logs as drill-down evidence, not as a second source of contract
semantics. Preserve a normalized `unknown` even if a log line appears
optimistic.

## Compare backends only when asked

For an explicit backend comparison:

1. Hold the exact input bytes and operation profile fixed.
2. Use separate fresh evidence directories.
3. Record each backend identity and version.
4. Compare normalized assertion status, analysis kind, vector names, finite
   sample validity, diagnostics, and provenance limitations.
5. Report native artifact differences separately.

Do not require byte-identical waveform files, point counts, or logs. Those are
backend representations, not the shared semantic assertion. A disagreement in
normalized status is a conformance investigation, not evidence that the more
favorable result is correct.

## Report the engineering decision

Return a compact review containing:

1. the question evaluated and the strongest supported conclusion;
2. execution status and engineering status as separate facts;
3. selected backend, native tool version, operation profile, and target;
4. material diagnostics plus artifact paths and hashes;
5. assumptions and provenance limitations;
6. one smallest justified next engineering action.

Label measurement or specification satisfaction as **not evaluated** unless a
separate explicit contract and its required evidence were actually applied.
