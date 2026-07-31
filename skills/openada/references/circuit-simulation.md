# Circuit simulation quick reference

Use this bounded reference when the task is only to run and measure a SPICE
circuit. The full OpenADA execution skill also covers layout, LVS, RTL,
synthesis, timing, PDK binding, and provider development; none of that belongs
in a model-free circuit-simulation turn.

## Resolve and preflight

Prefer `openada` on `PATH`. If it is absent and this reference was loaded from
an installed plugin, resolve the plugin root from the parent `SKILL.md` and use
`<plugin-root>/bin/openada` only when its Python dependencies are available.
If neither entry point works, report OpenADA as unavailable. Do not fall back
to a direct simulator command while claiming OpenADA evidence.

For a concrete project, preflight only the simulation assertion:

```bash
openada doctor --project-root /absolute/project \
  --assertion spice-analysis-evidence-valid
```

A preflight pass proves tool readiness at that moment; it does not run the
deck or establish any circuit claim.

## Run one portable analysis

For a model-free run, use a self-contained deck with exactly one top-level
`.op`, `.dc`, `.ac`, or `.tran`. Do not use includes, native `.measure` or
print directives, control blocks, or multiple analyses. Select the backend
explicitly and write into a fresh task-local directory:

```bash
openada simulate /absolute/project/tb_case.cir \
  --backend ngspice \
  --output-dir /absolute/project/evidence/case
```

Read `simulate.result.json` first. Keep these conclusions separate:

- `execution.status=completed`: the invocation completed;
- `engineering.status=pass`: fresh, finite evidence for the requested analysis
  satisfies `openada.operation/circuit.simulate/v1alpha2`;
- neither status proves that the modeled circuit matches its specification.

An engineering failure is evidence about that run. An `unknown`, invalid,
unavailable, timeout, or invocation failure supports no circuit conclusion;
repair the reported gap and rerun the same intent rather than switching
backends for a more favorable answer.

## Extract, measure, and evaluate

Do not read a scalar from a native log or waveform file. Inspect the installed
profiles before constructing requests:

```bash
openada profile show openada.operation/result.series.extract/v1alpha1
openada profile show openada.operation/result.measure/v1alpha1
openada profile show openada.operation/specification.evaluate/v1alpha1
```

Run the typed chain from the retained simulation and native artifact:

```bash
openada --compact extract \
  --simulation evidence/case/simulate.result.json \
  --artifact evidence/case/tb_case/tb_case.raw \
  --selection evidence/case/selection.json \
  > evidence/case/series.result.json

openada --compact measure \
  --series evidence/case/series.result.json \
  --measurement evidence/case/measurement.request.json \
  > evidence/case/measurement.result.json

openada --compact evaluate \
  --measurement evidence/case/measurement.result.json \
  --specification evidence/case/specification.request.json \
  > evidence/case/evaluate.result.json
```

Do not make a nonzero CLI exit synonymous with missing evidence. OpenADA exits
with code 1 when a valid result envelope has `engineering.status=fail`, including
a conclusive specification miss. A wrapper must retain and parse stdout first,
then classify the envelope; it must not discard that JSON or abort the remaining
campaign merely because the engineering result failed. Exit code 2, malformed
JSON, or an inconclusive envelope remains a command/evidence problem.

Require extraction status `extracted`, measurement status `measured`, and a
retained `specification.evaluate` result before reporting that a numeric limit
passed. Requests and selections are supporting inputs, not result evidence.
Use typed minimum and maximum results over the same series for peak-to-peak
ripple; an absolute maximum alone is not ripple.

Preserve exact deck, backend, tool version, raw artifact, selectors, units,
conditions, measurement ID, specification ID, bounds, result envelopes,
hashes, and diagnostics. Report one smallest justified next action and label
transistor, PDK, silicon, signoff, or unmeasured claims as not evaluated.
