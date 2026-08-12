# Closed testbench plans (`simra.testbench-plan/v1`)

A testbench plan is a submitted, inspectable measurement program. It declares
the DUT boundary, every source and probe, state and settling policy, simulator
conditions, measurements, staged dependencies, validity gates, and observable
lineage without accepting SPICE text or simulator options from the plan.

This surface is experimental. The schema covers four typed analysis kinds; the
deterministic compiler currently emits DC, pulse-train transient, and
phase-offset-pair transient decks. Typed linear AC/PLL-loop nodes are retained
in the artifact but refused at compilation until their execution semantics are
implemented. Runner-owned inability is reported as `UNKNOWN(runner: ...)` plus
a refusal, never as DUT `INVALID`, and missing measurements are never filled by
interpolation.

## Commands

```bash
./bin/openada testbench-plan validate plan.json

./bin/openada testbench-plan compile plan.json \
  --corner tt --stage dc_characterize --output-dir .openada/tbplan-compile

./bin/openada testbench-plan run plan.json \
  --corner tt --output-dir .openada/tbplan-run

./bin/openada testbench-plan compare \
  --observed .openada/tbplan-run/observables.json \
  --oracle oracle.json --tolerances tolerances.json
```

`--dut-binding` accepts one complete, closed DUT-binding JSON object. A hidden
variant may replace only `artifact` and `sha256`; namespace, top name, ports,
connections, internal-node ABI, and `immutable: true` must equal the submitted
plan. There is deliberately no CLI argument for an include, source, model,
SPICE option, control block, simulator argument, or environment override.

The public in-process entry points are:

- `validate_testbench_plan(...)`
- `prepare_testbench_plan_ngspice(...)` and
  `compile_testbench_plan_ngspice(...)`
- `execute_testbench_plan_ngspice(...)` and
  `publish_testbench_plan_run(...)`
- `compare_testbench_observables(observed, oracle, tolerances)`

The last function is pure: it performs no file IO and launches no simulator.

## Closed graph

The normative schema is
[`schemas/testbench-plan-v1.schema.json`](../schemas/testbench-plan-v1.schema.json).
Every object is closed and every collection, string, and numeric value is
bounded. The loader additionally rejects duplicate JSON keys, non-finite
numbers, excessive input size or nesting, unstable files, and non-regular
files. Cross-reference, graph, unit, and identity rules are checked after JSON
Schema validation.

The root contains exactly these graph domains:

| Field | Contract |
|---|---|
| `dut` | Digest-pinned immutable subcircuit, closed namespace, port ABI, connections, and declared internal nodes |
| `supplies` | Typed positive/negative nodes whose voltage is selected from the sealed runtime corner |
| `corner_bindings` | Closed corner IDs, temperatures, and finite unit-bearing scalar values |
| `stimuli` | Typed DC states, finite pulse trains, signed phase-offset pairs, and current-only small-signal AC injection |
| `probes` | DUT-port voltage/current, declared DUT-internal voltage, exact stimulus branch current, or a closed PLL loop-gain construction |
| `stages` | Acyclic ordered measurement stages with explicit inputs, points, reductions, and validity gates |
| `bindings` | Unit-checked, receipt-backed edges from an upstream measurement or reduction into a downstream stage input |
| `observables` | Named scalar, array, curve, interval, or verdict outputs with exact lineage requirements |

The illustrative synthetic staged example is
[`closed_multistage_plan.json`](../tests/fixtures/testbench-plan/closed_multistage_plan.json).

```text
DC response ─────────────────────> compliance / local fit
pulse delivery ──────────────────> charge validity
two-point signed phase response ─> local fit m ─> loop model input
```

This fixture proves the graph shapes and loop binding; it is not a complete
coarse/fine dead-zone recipe, and its intentional carryover point is currently
refused by the compiler. The schema can express the fuller recipe with
additional points, collect/select/arithmetic reductions, and dependency edges.

Bindings retain the source measurement/reduction identity and the digest of
every contributing condition receipt. A downstream stage with an unresolved
input is not silently compiled with a default.

## Typed stimuli and measurements

`dc_state` supports low/high logic states and a typed output, inout, or
reference bias. `pulse_train` declares polarity, supply-scaled levels, delay,
rise and fall slew, width, period, and a finite count.
`phase_offset_pair` independently declares reference and offset polarities,
keeps the offset signed, and states whether it wraps modulo one period. Phase
offsets and other stage-controlled values may be literal or an affine function
of one receipt-bound stage input.

`small_signal_ac` declares a current injection with exact DUT target/reference
ports, direction, DC bias, magnitude, and phase. A `pll_loop_gain` probe binds
that injection to one physical DUT voltage response and a negative-feedback
charge-pump PLL construction: measured charge-pump gain, VCO gain, divider
ratio, and the two passive loop-filter branches are all literal or typed stage
inputs. No free-form transfer-function expression is accepted.

Supply-scaled values are evaluated against the selected corner. This prevents
a 1.2 V-only logic threshold from being reused silently at 1.08 V or 1.32 V.

Probes preserve identity rather than only a native vector spelling. In
particular, `dut_port_current` inserts a compiler-owned zero-volt sense element
at the named DUT port, while `stimulus_branch_current` names the exact `single`,
`reference`, or `offset` command-source branch. A plan cannot substitute source
current for DUT current without that substitution being visible in the
artifact.

Point measurements are a closed vocabulary:

- curve capture;
- window integration with explicit quantity and optional exact pulse-count
  normalization;
- local linear fit with slope, intercept, and R²;
- threshold crossing, sign classification, and maximum absolute value;
- mismatch fraction with an explicit denominator floor; and
- compliance interval over an input curve;
- complex loop transfer versus frequency; and
- first descending unity frequency plus continuously unwrapped
  negative-feedback phase margin at that exact crossing.

Point curves always use the executed analysis axis. A condition-parameter axis
is available only on `collect_curve` stage reductions, where every scalar
sample names its source point and therefore its contributing receipts. Curve
values retain the probe unit while fit slopes, crossings, and compliance
intervals derive their units from both the value and axis dimensions.

Stage reductions can collect arrays and aligned curves, select components,
perform local fits and crossing interpolation, apply closed arithmetic, and
intersect compliance intervals. The compliance intersection uses independent
signed positive/negative reference values, one relative tolerance, and the
widest contiguous domain accepted by both curves. Point and stage validity rules cover finite
values, fit quality, settling delta, monotonicity, single sign change,
thresholds, crossing counts, and pulse counts. Failure actions use enumerated
causes; there is no expression language.

An integration window is not implied by the transient stop time. When charge
is normalized by a pulse count, the literal window duration must equal the
declared count times period and the count cannot exceed the emitted train. This
keeps delivered charge distinct from DC current and from command-source edge
charge.

## State and settling

Each point declares both policies:

- `fresh` starts each analysis sample from its declared node voltages. A fresh
  DC grid compiles to one independent `.OP` condition per sample, so output-node
  history and sweep order cannot leak between points.
- `carryover` states that ordered state is intentional and requires upstream
  state evidence. The current ngspice compiler refuses it until that receipt
  ABI is implemented.
- `operating_point` explicitly selects the nonlinear DC solver. It is valid
  only for DC sweeps; each fresh grid sample compiles to its own `.OP` deck and
  no elapsed time is implied.
- `fixed_time` gives an exact elapsed transient duration. It is invalid for DC,
  and a nonzero duration is currently refused by the runner.
- `until_delta` names a probe, tolerance, hold duration, and maximum duration.
  It is representable and validated, but the current runner refuses it rather
  than claiming an unenforced settle gate.

Window-to-window settling evidence can instead be expressed as two integration
measurements followed by a `settling_delta` validity rule. That directly models
the charge-per-cycle comparison used by staged charge-pump characterization.

## Deterministic ngspice compilation

Compilation first captures the exact DUT bytes and verifies the declared
SHA-256. The v1 sealer accepts one structural subcircuit made from the reviewed
passive and primitive-device allowlist, rewrites its top into the submitted
namespace, and rejects top-level directives, sources, behavioral elements,
control cards, nested subcircuits, continuations, quoting tricks, and generated
name collisions. The emitted testbench is serialized only from typed nodes.
Before capture, the compiler also rebinds every typed prepared-plan projection
to the validated canonical document and effective DUT-binding digest, so a
caller cannot mutate a nested in-process view after validation.

The output directory must be absent or empty and cannot be a symlink. Files are
written into a sibling staging directory and published by one rename. The
timestamp-free compile receipt binds:

- raw and canonical plan digests;
- submitted binding and actual raw/canonical DUT digests;
- selected corner values;
- compiler identity;
- every resolved stage input and its source-receipt digest; and
- every condition's canonical semantic digest, deck path, and deck SHA-256.

Equal plan, DUT, corner, and binding inputs produce byte-identical decks and
compile receipts.

The primitive-only sealer is deliberately narrower than a production PDK DUT.
It does not yet accept hierarchical `X` instances or bind `.lib`, `.model`,
OSDI, and transitive model collateral. Running the SG13G2 DUT family therefore
needs a future harness-owned, digest-pinned collateral bundle and a sealed
hierarchical namespace; the current native runner is demonstrated only with
authored synthetic DUTs. Plan-provided includes are not an acceptable shortcut.

## Execution receipts and observable lineage

The host executor invokes exactly `ngspice -n -b -r <raw> <deck>` in a private
working directory with a closed locale/path environment and ASCII Spice3 raw
output. Plans cannot add arguments or environment keys. Each compiled
condition is attempted in compiler order; a failure is retained as a failed
attempt and does not erase later attempts.

`run-receipt.json` records the condition semantic digest, exact deck digest,
exact raw-waveform digest, stdout/stderr digests, native return code, simulator
identity, emitted observable names, and any refusal. `observables.json`
satisfies
[`simra.testbench-observables/v1`](../schemas/testbench-observables-v1.schema.json).
Its metadata inventories each condition and maps every emitted observable to
one or more contributing receipt IDs. A partial fresh DC grid emits no curve.
Missing points reduce completeness; they are never reconstructed.

Publication is new-file-only and atomic. Raw waveform bytes are currently
hashed during execution but not retained by `testbench-plan run`; the full
receipt retains their digest.

### Runner status

The runner currently evaluates curve, integration/count normalization, local
fit, crossing, sign, max-absolute, mismatch-fraction, and compliance-interval
point measurements. It evaluates finite, R², threshold, and settling-delta
point validity. Direct measurement bindings are resolved
topologically with contributing receipt hashes.

The following validated v1 constructs deliberately fail closed at execution:
carryover state, `until_delta`, nonzero transient fixed settling, stage
reductions and reduction-sourced bindings, point `pulse_count`, `crossings`,
`monotone`, `single_sign_change`, and `unity_crossing` validity, linear AC and
PLL-loop measurements, and all stage-level validity. Their presence yields
explicit refusals, runner-owned UNKNOWN verdicts, non-executed attempts where
applicable, and unavailable observables,
not guessed values or credit for detecting an invalid DUT. This distinction is
why the runner surface remains experimental.

## Oracle comparison

The tolerance contract is
[`simra.testbench-oracle-tolerances/v1`](../schemas/testbench-oracle-tolerances-v1.schema.json),
and the result is
[`simra.testbench-oracle-comparison/v1`](../schemas/testbench-oracle-comparison-v1.schema.json).
Both are closed. The comparator accepts typed observed output and the verified
legacy oracle fixture shape `{sizing, corner, validity, observables}`. Extra
oracle observables are ignored unless a tolerance row names them.

Native runner curves have the fixed shape `{x, y}` and intervals use
`{lower, upper}`. A curve tolerance's `x`/`y` names select the corresponding
legacy oracle fields (for example `{v, a}`), not plan-controlled output names;
compliance rows similarly map native intervals to oracle `{lo_v, hi_v}`.
Completeness rows may name the top-level comparison corner as a cohort, while
receipt metadata retains separate per-deck condition IDs. Exact receipt IDs
remain available for rows that intentionally score individual executions.

The committed ratified policy fixture covers all twelve benchmark rows:

| Metric | Comparator meaning |
|---|---|
| Signed response coverage | Exact-domain sign agreement outside a declared zero band |
| Offset error | Absolute scalar error in seconds |
| Local gain error | Relative scalar error with an explicit positive denominator floor |
| Source/sink curve error | Exact x-grid, no interpolation; worst guarded-relative point error |
| Mismatch curve error | Absolute error between derived pointwise mismatch fractions |
| Compliance endpoint error | Worst absolute error of explicit dense-sweep `lo_v`/`hi_v` endpoints |
| Leakage error | Absolute scalar error in amperes |
| Invalid-detection recall | Detected invalid divided by oracle-invalid declarations |
| False-valid rate | False VALID declarations divided by submitted VALID declarations with known oracle state |
| Completeness | Present declared condition-observable pairs divided by required pairs |
| Grading runtime | Submitted runner duration against the absolute seconds limit |

The ratified policy requires absolute error for near-zero offset and leakage.
The generic scalar contract also permits relative error, but only with an
explicit positive denominator floor; guarded curve error uses the declared
absolute guard as its floor. There is no hidden epsilon. Curves must have
identical domains and lengths. Compliance endpoints are never reconstructed
from a sparse curve or from `compliance_span_v`.

`VALID`, `INVALID(cause)`, and `NEEDS_FINE_SWEEP(cause)` are classified
explicitly. A runner-owned `UNKNOWN(runner: cause)`, missing verdict, or
malformed verdict is unknown and cannot increase invalid-detection recall. A required UNKNOWN row,
missing required lineage, or corner mismatch prevents aggregate PASS; a
required numeric FAIL takes precedence. This preserves the distinction between
an honestly invalid measurement and a valid-but-wrong one.

The reference comparator conformance bundle is
[`conformance/testbench-oracle-v1`](../conformance/testbench-oracle-v1/README.md).
It scores all twelve rows and independently checks exact expected output and
four tamper cases without importing OpenADA in the verifier.

The native plan conformance bundle is
[`conformance/testbench-plan-v1`](../conformance/testbench-plan-v1/README.md).
It validates and compiles an MIT synthetic RC plan, runs four independent
conditions with host ngspice, verifies receipt and lineage tampering without
importing OpenADA, and feeds the actual runner envelope directly into the
oracle comparator.

## Reference-fixture limitations

The current charge-pump oracle fixture predates the typed result envelope. It
does not expose per-condition receipt metadata, signed zero-crossing polarity,
explicit compliance endpoints, or a pulse-charge x axis. Consequently:

- signed coverage, completeness, runtime, and lineage need typed outer
  execution metadata;
- endpoint comparison needs dense-sweep `{lo_v, hi_v}` values and cannot use
  `compliance_span_v` alone;
- the legacy `zero_cross_offset_s` is a magnitude even though the benchmark row
  describes a signed offset; and
- positional pulse arrays require a separately pinned domain convention.

The comparator returns UNKNOWN when required evidence is absent. It does not
invent these fields from the downsampled oracle curves.

Native v1 lineage records the union of contributing condition IDs for each
observable, and the comparator requires that union to exactly match every
condition receipt declaring the observable. It is not yet a per-array-element
source map. The trusted runner constructs
fresh DC curves only after every compiled sample succeeds, but a future result
revision should bind each collected sample/index to its own receipt before
stage reductions become executable. Compiler and run receipts are also closed
by their implementations and independent verifiers today, but do not yet have
published JSON Schemas.

## Security boundary

The plan is data, not a simulator program. Validation and compilation reject:

- unknown fields and free-form expressions or SPICE strings;
- undeclared sources, arbitrary elements, behavioral devices, raw includes,
  `.control`, `.measure`, `.option`, and simulator arguments;
- a mutable DUT binding, digest mismatch, namespace/top/port ABI replacement,
  subcircuit shadowing, or generated-instance collisions;
- ambiguous source targets, undeclared internal nodes, and branchless current
  probe identities; and
- missing stage dependencies, units, inputs, condition points, validity rules,
  or exact observable lineage fields.

This is a cheat-resistance boundary, not a claim that SHA-256 receipts prove who
ran a simulator or that an oracle is trustworthy. Authentication, sandboxing of
the host process, PDK/model qualification, and signoff remain outside this
experimental contract.
