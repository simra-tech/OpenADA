# The Agent–EDA Contract

OpenADA defines a narrow control and evidence boundary between an agent harness
and native EDA tools. It does not replace native formats or require every EDA to
share one internal data model.

This document is the source of truth for the current CLI and
`openada.result/v0alpha1` behavior in the OpenADA `0.4.0` development release.
Its unreleased status is recorded in the changelog. The broader
[semantic model](SEMANTIC_MODEL.md) and
[request/driver protocol](DRIVER_PROTOCOL.md) distinguish implemented behavior
from explicit-provider runtime behavior and future protocol proposals.

## Contract responsibilities

An OpenADA driver must:

1. Resolve an explicit or discovered native executable.
2. Validate the operation's required native inputs before launch.
3. Construct a deterministic argv vector without a shell.
4. Bound runtime and captured text.
5. Keep process status separate from engineering status.
6. Normalize the smallest useful engineering facts.
7. Record input and output artifacts with paths, sizes, and hashes.
8. Preserve enough provenance to reproduce or audit the invocation.

The agent remains responsible for choosing the correct task, workspace, PDK,
model, rule deck, and interpretation. The native EDA remains responsible for
executing the underlying analysis.

## Result envelope

Every command emits one JSON object with schema identifier
`openada.result/v0alpha1`. The machine-readable JSON Schema is
[result-v0alpha1.schema.json](../schemas/result-v0alpha1.schema.json) and is also
included under `share/openada/schemas` in the Python wheel. CLI validation
failures use the same envelope with execution `invalid_request` and engineering
`unknown`; `--help` and `--version` remain human-readable informational exits.
The [compatibility policy](COMPATIBILITY.md) defines which changes may retain
this identifier and which require a new immutable schema file.
OpenADA-generated summaries, execution errors, diagnostic messages, and hints
are bounded to 4,000 characters while retaining head and tail context.

```json
{
  "schema": "openada.result/v0alpha1",
  "operation": "drc",
  "tool": {
    "name": "klayout",
    "path": "/usr/bin/klayout",
    "version": "KLayout 0.x"
  },
  "execution": {
    "status": "completed",
    "exit_code": 0,
    "duration_ms": 123,
    "command": ["/usr/bin/klayout", "-b", "..."],
    "cwd": "/tmp/openada-run"
  },
  "engineering": {
    "status": "fail",
    "summary": "KLayout reported 3 DRC violation(s)."
  },
  "inputs": [],
  "artifacts": [],
  "diagnostics": [],
  "data": {},
  "provenance": {
    "openada_version": "0.4.0",
    "created_at": "2026-07-13T00:00:00Z",
    "host": {
      "system": "Linux",
      "machine": "x86_64",
      "python": "3.12.3"
    }
  }
}
```

The example is intentionally a completed process and a failed engineering
check. Agents and automations must preserve that distinction.

## Status semantics

Execution statuses describe whether OpenADA could invoke and observe the
process:

- `completed`
- `timed_out`
- `not_available`
- `invalid_request`
- `failed`

Engineering statuses describe what the evidence supports:

- `pass`
- `fail`
- `unknown`
- `not_applicable`

An unavailable tool or malformed report produces `unknown`, not `fail`, because
no trustworthy engineering conclusion was reached.

A completed execution records an integer exit code but may support engineering
`pass`, `fail`, `unknown`, or `not_applicable`, depending on the native evidence
and operation. An incomplete execution normally leaves engineering `unknown`;
it may report `fail` only when bounded native evidence already makes that
conclusion trustworthy. Execution failure alone is never engineering failure.

When a native process uses a material working directory, `execution.cwd`
records its resolved path. This is execution provenance, not a sandbox claim.
If the requested or inherited working directory cannot be resolved, execution
is `failed`, engineering remains `unknown`, and no child process is launched.

## Operation semantics

### `doctor`

Inspect tool availability, bounded version output, runtime profile, and PDK
roots. Discovery is read-only. A resolved binary is `unusable` when every
bounded version probe fails, and it does not satisfy `--require`. A tool can be
`discovered` without having a structured operation.
The per-tool `maturity` value is `discovered`, `structured`, or
`workflow-validated` using the public levels in the roadmap.

Discovery executes each selected binary's bounded version command from an
isolated temporary working directory. That protects the project from legacy
CLIs that treat unknown flags as filenames; it is not a sandbox for the
binary. For the five scoped-assertion tools, an exact version observation
requires a completed probe with a tool-identified version line, complete valid
UTF-8 output, at most 500 characters on the selected line, and stable binary
identity across the probe. The accepted exit policy is zero for every tool
except Xschem: reviewed Xschem versions may return 1 after printing the exact
version/copyright text, so that one probe accepts 0 or 1 only when stderr is
empty and the Xschem version grammar matches. A resolved scoped-assertion
binary without a trustworthy observation is `unusable` rather than versioned
by inference.
Scoped-preflight version timeouts are bounded to 30 seconds per native probe;
legacy focused or broad discovery retains its positive finite timeout option.

Scoped first-run preflight is additive:

```text
openada doctor --project-root ROOT --assertion ASSERTION
```

Both options are required together and cannot be combined with `--tool` or
`--require`. `ASSERTION` is one of:

- `schematic-netlist-generated` → Xschem `netlist`;
- `spice-analysis-evidence-valid` → ngspice `simulate`;
- `drc-clean` → KLayout `drc`;
- `lvs-match` → Netgen `lvs`;
- `rtl-structural-check-passes` → Yosys `rtl-check`.

This mode resolves one canonical existing project directory, probes exactly
the assertion's one mapped binary, reports configured PDK roots without
enumerating their children, and returns exactly one target under
`data.preflight.target`. It never enumerates project entries or collateral.
`data.pdks` is deliberately empty and
`data.preflight.pdk.catalog_enumerated` is `false`; this is not evidence that
no PDK is installed. `data.preflight.pdk.selected` and startup
`selected_files` remain empty until the operation receives explicit
project-specific inputs.

The single scoped tool record adds `version_probe.status`,
`binary_identity_stable`, and `accepted_exit_code`. `accepted` is the only
ready probe status; failures
such as `probe_timed_out`, `output_truncated`, `output_invalid_utf8`,
`output_unparseable`, `output_identity_mismatch`, `output_malformed`,
`nonzero_probe_stderr`, `probe_failed`, and
`binary_identity_changed` remain bounded classifications and never include the
native output. `accepted_exit_code` is otherwise null. The preflight
project-root record similarly exposes `identity_stable` after the probe.

A preflight `pass` means only that the root remained stable and the mapped tool
had a trustworthy path/version observation. `assertion_evaluated` is always
`false`, and the summary states that no design assertion ran. A missing or
unusable mapped tool fails this environment-readiness check. An invalid or
changed project root produces `unknown`. Preflight is a point-in-time routing
observation, not a sandbox, project attestation, PDK selection, or signoff
result; every subsequent operation independently validates and hashes its real
inputs.

### `netlist`

Ask Xschem to compile a native schematic to a SPICE artifact. A project/PDK
rcfile can be supplied as an explicit hashed configuration input. Engineering
pass requires both the requested artifact and no recognized Xschem
`IS MISSING` symbol records. An incomplete native artifact is retained and
hashed as failure evidence.

### `simulate`

Without `--backend`, `simulate` retains the legacy ngspice interface described
below. With `--backend ngspice|xyce`, it evaluates the typed
`openada.operation/circuit.simulate/v1alpha2` profile. The immutable v1alpha1
profile remains historical. The active common alpha path
accepts exactly one parseable, self-contained top-level OP, DC, AC, or transient
analysis; rejects includes, `.measure`, `.print`, control blocks, FFT, noise,
Monte Carlo, and multiple analyses; and emits the profile's closed
`data.protocol`, `data.analysis`, and `data.evidence` facts. Both mappings use
the same assertion truth table and canonical artifact roles while retaining
different native commands and files.

The supported feature matrix is closed:

| Backend | OP | DC | AC | TRAN |
|---|---:|---:|---:|---:|
| ngspice | structured | structured | structured | workflow-validated |
| Xyce | unsupported | structured | structured | workflow-validated |

Xyce OP is rejected as unsupported; it is not emulated with a zero-span DC
sweep or inferred from log text. A caller may omit `--analysis` and let OpenADA
inspect the deck's one supported top-level analysis, or provide an explicit
typed request. Typed parameters require `--backend` and must match the deck:

- OP: `--analysis op` with no analysis parameters;
- DC: `--analysis dc --source-name NAME --source-unit V|A --start VALUE
  --stop VALUE --step VALUE`;
- AC: `--analysis ac --sweep lin|dec|oct --points COUNT --start-hz HZ
  --stop-hz HZ`;
- transient: `--analysis tran --step-s SECONDS --stop-s SECONDS`, with
  optional `--start-s` and `--max-step-s`.

The shared path does not accept legacy ngspice `--raw-file`,
`--execution-mode`, `--expect-output`, `--init-file`, or
`--system-init-file` options. Those continue to belong only to the legacy path
below.

Every top-level SPICE deck is limited to 16 MiB. OpenADA rejects a larger deck
before native launch and never hashes or scans beyond that ceiling. The shared
profile records the same limit as a semantic constraint, so a connector cannot
silently widen it while claiming `circuit.simulate/v1alpha2` conformance. Input
capture opens a nonblocking regular-file descriptor and verifies its file and
path identity after reading; FIFOs, non-regular paths, and changed inputs fail
closed rather than blocking inspection.

The legacy ngspice path has one of two explicit execution policies:

- `batch` is the default, wrapper-owned streaming mode. OpenADA invokes
  `ngspice -b -r ...`, retains the selected raw file, and does not keep the
  complete waveform in ngspice memory. ngspice disables dot-card `.measure`
  processing when batch mode and `-r` are combined, and its manual warns that
  `.control` sections should not be run with `-b`. OpenADA therefore rejects
  directly declared `.measure`, `.control`, and pure `*ng_script` constructs in
  this mode. Because transitive files are not yet recursively attested, a batch
  deck containing `.include`, `.inc`, or `.lib` is also rejected before launch;
  use control mode or a reviewed flattened deck. The native batch
  measurement-suppression warning remains a fail-closed defense.
- `control` uses a retained OpenADA control launcher and ngspice's in-memory
  control interpreter, forced explicitly with `-i`. It supports top-level `.measure` statements and native
  `.control` decks. When no deck output is declared, OpenADA runs an analysis
  when needed and writes a wrapper-owned raw artifact. When one or more
  `--expect-output KIND=RELATIVE_PATH` options are present, the caller's deck
  owns analysis and output commands; OpenADA only sources the deck, then quits.
  Control mode may use substantially more memory than streaming batch mode.

`KIND` is currently `raw` for Spice3/ngspice raw files or `wrdata` for
numeric tables produced by `wrdata`. Every declaration is required, is
resolved against the recorded working directory, and must name an exact
control-safe ASCII relative path whose real parent directory already exists.
Whitespace, glob syntax, traversal, and ngspice control metacharacters are
rejected in this preview. A declared path must be absent before launch; OpenADA
does not delete or accept stale files. OpenADA retains real parent-directory
descriptors across execution, revalidates their identities, and captures through
those descriptors; replaced parents, symlinks, non-regular files, and files with
multiple hard links are rejected. It then captures a stable SHA-256 snapshot
and structurally validates the native format. Raw validation requires a complete
binary or ASCII payload and at least one non-constants analysis plot. `wrdata`
validation requires finite numeric rows and separate evidence that an analysis
completed. Preview ceilings are 32 declared outputs, 16 MiB logs, 256 MiB per
native output, and 33,554,432 numeric scalars per validated file.

OpenADA never scans the working directory for undeclared files. A native
control deck can run `shell`, change directory, append files, or cause other
side effects, so this containment rule is evidence selection—not a sandbox.
Use a fresh task-local writable project copy for untrusted decks and for decks
that write via `$inputdir`.

`--init-file` is available in control mode for a project or PDK initialization
script. Explicit init and system-init files use the same 16 MiB per-input
ceiling. OpenADA hashes each accepted file as a configuration input and
explicitly sources it
before the deck. The `-n` flag disables only local and user `.spiceinit`; it
does not disable ngspice's system `spinit`. Callers that need complete startup
identity can also pass a readable file named `spinit` with
`--system-init-file`. OpenADA hashes that file and sets `SPICE_SCRIPTS` to its
parent for the child process. Without the system option, native system startup
remains enabled and unenumerated. The effective policy, startup paths, and
bounded `SPICE_SCRIPTS`/`SPICE_LIB_DIR` environment are recorded in result
data.

Engineering pass requires a completed zero-exit process, a fresh nonempty log,
all required outputs to be fresh and structurally valid, evidence that a real
analysis completed, no terminal convergence/native fatal diagnostic, and one
unambiguous finite value inside ngspice's native measurement section for every
uniquely declared top-level `.measure`. Reviewed terminal convergence evidence
has `fail` precedence only when no generic native error or other conflicting
evidence undermines that classification. Intermediate singular-matrix or
stepping warnings do not
become engineering failure when ngspice recovers and produces valid analysis
evidence. Missing, corrupt, constants-only,
unstable, or otherwise insufficient output has status `unknown`; regular
partial files are still hashed and retained as evidence.

The netlist directory is the default process working directory. Callers can
select an existing project directory for relative model, include, and native
output paths. Transitive model/include inputs remain unenumerated in control
mode and are explicitly unsupported in batch mode. ngspice control commands
cannot safely represent every filesystem name; OpenADA embeds validated paths
in its launcher and rejects source/init paths that cannot be represented without
whitespace, non-ASCII characters, or control metacharacters instead of silently
truncating them.

These mode constraints follow the
[official ngspice manual](https://ngspice.sourceforge.io/docs/ngspice-manual.pdf);
they are connector semantics, not a new simulation-data format.

The Xyce alpha invokes serial Xyce as literal argv `Xyce -l LOG -r RAW -a
NETLIST`, with every option before the netlist and `XYCE_NO_TRACKING=1`. The
log and ASCII Spice raw file are created in a fresh temporary location, then
captured, bounded, hashed, and structurally validated before publication.
Engineering `pass` requires zero native exit, one complete real plot of the
declared supported analysis with finite dependent values, a fresh log, stable
input identity, and complete bounded process capture. Only reviewed terminal
non-convergence text in the matching analysis, without a conflicting generic
native error, can produce engineering `fail`;
parse errors, missing or malformed raw data, timeouts, generic nonzero exits,
and OP requests are `unknown` or invalid/unsupported as defined by the profile.
The DC and AC mappings have structured native success evidence in the expanded
[ngspice/Xyce replay](../conformance/circuit-simulate-v0alpha2/README.md), while
the transient mapping retains workflow-validated maturity. The expanded
success-only cases do not widen the alpha profile, cover every maturity
outcome, or imply support for arbitrary Xyce decks.

### `extract`

`extract` implements `openada.operation/result.series.extract/v1alpha1` and
`openada.assertion/series.extraction.valid/v1alpha1`:

```text
openada extract --simulation SIMULATION-RESULT.json \
  --artifact /exact/path/to/simulation.raw \
  --selection SERIES-SELECTION.json
```

The simulation input must be the complete passing
`circuit.simulate/v1alpha2` envelope for one reviewed built-in ngspice or Xyce
mapping. The artifact path must equal its one retained `simulation.result`
canonical path. Extraction reopens one regular file, verifies exact bytes and
SHA-256, parses one unambiguous request-bound padded analysis plot under fixed
limits, and rechecks identity. ngspice binary/ASCII and Xyce ASCII Spice3 raw
are implemented; Xyce binary and unpadded plots are rejected.

The selection document contains exactly `selectors`, `conditions`, and empty
`extensions`. Every selector declares one exact native vector, unique output
name, exact unit, and `real` or `imaginary` Cartesian component. Only reviewed
native voltage→V and current→A dependent-variable mappings are accepted. OP
maps to `sample/1`, DC to the typed source/unit, AC to `frequency/Hz`, and
transient to `time/s`. The operation does not infer units, expressions,
magnitude, phase, dB, interpolation, or resampling.

Engineering `pass` records `data.extraction.source.binding: verified`, native
plot facts, and a canonical normalized series. The `measure`, `spectral`, and
`transfer` CLI commands accept that complete passing extraction envelope
directly and unwrap only its verified embedded series. Each downstream
operation's source digest covers the axis, signals, and caller-declared
condition bindings. Embedded native lineage remains `unverified` inside the
separate downstream assertion; the retained extraction envelope is what
preserves the verified native binding. Invalid, stale, tampered, ambiguous,
unsupported, or over-limit evidence is `unknown`, never an engineering fail.

### `measure`

`measure` implements `openada.operation/result.measure/v1alpha1` and
`openada.assertion/measurement.valid/v1alpha1` without invoking an EDA:

```text
openada measure --series SERIES.json --measurement REQUEST.json
```

The series argument may be either a closed normalized-series document or one
complete passing `result.series.extract/v1alpha1` envelope. The normalized
representation has one strictly increasing axis, one or more equal-length
finite real signals, explicit units and conditions, and a producer
operation/request identity. The declared
`series.source.artifact_sha256` must equal OpenADA's canonical SHA-256 over the
normalized axis, signals, and condition bindings. That digest binds the inline
content; it is not a native waveform hash. An optional upstream native artifact
may appear only as lineage with `binding: unverified`. The operation neither
opens nor decodes ngspice/Xyce raw files and makes no built-in native waveform
extraction claim.

Python callers should use
`openada.operations.normalized_series_sha256(axis=..., signals=...,
conditions=...)` to compute this value with the operation's exact validation
and normalization rules. Successful and conclusive not-found records also
retain `data.measurement.request_sha256`, the canonical digest of the complete
normalized measurement request.

V1alpha1 has a closed scalar vocabulary: `sample_at`, `minimum`, `maximum`,
`mean`, `rms`, `crossing`, `rise_time`, `fall_time`, and `settling_time`.
Coordinates must use the exact axis unit; thresholds, targets, and tolerances
must use the exact selected-signal unit. There is no implicit conversion,
complex-series support, simulator expression, FFT, or arbitrary plugin
algorithm. Inputs are bounded to 100,000 axis points, with matching signal
lengths and finite values.

Engineering `pass` means the declared versioned algorithm produced one finite
typed scalar. Engineering `fail` means a valid bounded domain conclusively did
not contain the requested sample or event, with measurement status
`not_found`. Invalid shape, digest, units, domain, signal, algorithm, or numeric
evidence is `unknown`, normally with execution `invalid_request`. Neither pass
nor fail evaluates a design specification.

### `spectral`

`spectral` implements
`openada.operation/result.spectral.measure/v1alpha1` and
`openada.assertion/spectral.measurement.valid/v1alpha1`:

```text
openada spectral --series SERIES.json --measurement SPECTRAL-REQUEST.json
```

It accepts the same canonical normalized real-series representation or complete
passing extraction envelope as `measure`, with an axis unit of exactly seconds.
V1alpha1 requires 8 through
65,536 power-of-two uniformly spaced points, a declared interval tolerance,
an exact coherent fundamental bin, rectangular window, arithmetic-mean
removal, one-sided mean-square per-bin power, no segments or averaging, a
closed first-Nyquist band, explicit harmonic orders, fold-to-first-Nyquist
aliasing, zero-bin integration width, and collision rejection.

The closed scalar kinds are `snr`, `sinad`, `thd`, and `sfdr`. SNR removes DC,
fundamental, and declared in-band harmonics before summing noise. SINAD includes
all non-DC/non-fundamental residual power. THD divides declared in-band harmonic
power by fundamental power and is a signed dB ratio. SFDR compares the
fundamental with the largest residual bin, keeps harmonics as competitors, and
chooses the lowest frequency on a power tie. Results retain component powers,
harmonic membership, compressed bin ranges, winning spur, and a SHA-256 of the
complete semantic partition.

Engineering `pass` requires one finite dB scalar. A valid record with zero
power at the declared fundamental is `not_found`/engineering `fail`. A zero
ratio numerator or denominator that would require infinity is `unknown` with a
null value; OpenADA does not invent a numeric floor. Nonuniform, noncoherent,
windowed, fitted, PSD, averaged, SNDR, ENOB, jitter, and phase-noise methods are
not silently substituted.

The request may identify generic OpenADA definition context or candidate ADC,
DAC-device, or waveform-recorder IEEE scope. `candidate` is not IEEE
conformance. See [Measurement methods and standards](MEASUREMENT_METHODS.md).

### `transfer`

`transfer` implements
`openada.operation/result.transfer.measure/v1alpha1` and
`openada.assertion/transfer.measurement.valid/v1alpha1`:

```text
openada transfer --series SERIES.json --measurement TRANSFER-REQUEST.json
```

The series argument may be a canonical normalized real series or a complete
passing extraction envelope. V1alpha1 requires at least two strictly
increasing positive frequencies with axis unit exactly `Hz`, and four distinct
same-unit real series naming the input and output phasors' real and imaginary
Cartesian components. At every point the kernel computes exactly complex
output divided by complex input. A zero input or output magnitude, a non-finite
ratio, or a dimensional mismatch is `unknown`; no numerical floor is invented.

The closed metrics are `low_frequency_gain_db`, `bandwidth_3db`,
`unity_gain_frequency`, and `phase_margin`. “Low frequency” means the first
positive simulated frequency and is explicitly not DC. The -3 dB reference is
that first-point magnitude minus exactly 3.0 dB. Bandwidth and unity use only
falling adjacent-point crossings, require exactly one candidate, and interpolate
magnitude and unwrapped phase linearly over log10 frequency. Phase margin is
available only when the request explicitly declares
`loop-gain-negative-feedback`, and is 180 degrees plus the unwrapped
output-over-input phase at the unique falling 0 dB crossing.

One finite scalar is `measured`/engineering `pass`; no required falling
crossing is conclusively `not_found`/engineering `fail`; multiple crossings or
an invalid source or interpretation are `unknown`. The profile does not define
gain margin, a phase-crossing search, multi-crossing selection, true DC gain,
smoothing, fitting, extrapolation, de-embedding, or a general stability claim.

### `evaluate`

`evaluate` implements `openada.operation/specification.evaluate/v1alpha1` and
`openada.assertion/specification.satisfied/v1alpha1`:

```text
openada evaluate --measurement RESULT-MEASURE-ENVELOPE.json \
  --specification SPECIFICATION.json
```

The CLI measurement input must be a complete `openada.result/v0alpha1` envelope
whose operation is `result.measure`, `result.spectral.measure`, or
`result.transfer.measure` and whose typed `data.measurement` is present. The
Python operation API can accept that extracted typed measurement record
directly. The specification names the exact measurement ID, at least one
explicit lower and/or upper finite bound, each bound's inclusive flag, and
required operating conditions. Measurement and bound units must be identical
strings. Every required condition must occur exactly once in the measurement
source with the same JSON scalar type, value, and unit. V1alpha1 performs no
conversion, dimensional inference, tolerance matching, or missing condition
inference.

When both inputs normalize successfully, `data.evaluation.source` retains the
canonical measurement digest and measurement source, plus the complete
normalized specification and `specification_sha256`. These measurement,
request, condition, and specification digests detect stale bindings or changed
content; none is a digital signature, proof of authorship, or authentication
mechanism.

Engineering `pass` requires a measured finite value inside every declared
bound at exactly matched conditions. Engineering `fail` requires the same valid
evidence and a conclusive bound violation. A `not_found` or `unknown`
measurement, unit mismatch, condition mismatch, invalid interval, or broken
source binding yields specification `unknown`, never a false specification
failure. The retained source object described above keeps the full measurement
and specification binding available for audit.

### `profile`

`profile list` and `profile show OPERATION-PROFILE-ID` inspect the packaged
operation-profile catalog. `list` returns schema, operation, assertion, and
feature identities for all six active typed profiles plus the immutable
historical `circuit.simulate/v1alpha1` profile. `show` returns one complete
packaged profile or a typed `profile.not_found` failure. These commands validate
the installed profile documents before returning them; they are control-plane
inspection results, not engineering assertions and not provider discovery.

### `provider`

`provider validate`, `provider list`, and `provider invoke` implement an
explicit control-plane boundary for one
`openada.driver-manifest/v0alpha1`. They are not engineering operation profiles
and do not change `simulate --backend` or built-in driver selection.

`invoke` requires one complete `openada.request/v0alpha1` with an exact driver
selector. The implemented external dispatch registry currently contains only
`openada.operation/circuit.simulate/v1alpha2`; the other packaged profiles use
their operation-specific built-in CLI bridges. The runtime validates the
manifest and its internal cross-references, request, installed operation
profile, closed and relational parameters, target, configuration, feature set,
the profile side-effect mode against caller authority and capability
declarations, maturity records and their native-product coverage, assertion
truth table, evidence roles and limits, and one unambiguous local CLI
JSON-stdio `wait` transport. The current simulation binding requires the target
and every configuration locator to name a canonical absolute regular
non-symlink file. The target is limited to 16 MiB, each configuration file to
256 MiB, and all request input files together to 512 MiB. Before launch the host
snapshots their filesystem identity, byte count, and SHA-256 and rejects any
declared digest that does not match. Its evidence destination must also be a
canonical absolute filesystem path whose
canonical non-linked parent already exists; only `fail-if-present` is accepted,
and the destination itself must be absent before launch.

The host invokes an executable argv vector without a shell. It resolves the
executable and any standalone existing regular-file path arguments, substitutes
their canonical paths, and compares their filesystem identities before and
after execution. It bounds stdin/stdout/stderr and the declared timeout,
requires a zero transport-process exit with no stderr, and terminates the fresh
process group on timeout, overflow, and after the parent exits. That kills only
descendants which remain in the group; a process that deliberately detaches can
escape this containment, and the provider runtime is not a sandbox. It then
validates the base and operation-specific result,
correlation/profile/provider echoes, truth-table and evidence consistency, and
every recorded local input and artifact against its declared regular-file size
and SHA-256 under aggregate bounds. Every returned artifact path must be
canonical, absolute, and inside the authorized evidence destination; an
existing artifact must resolve there without symbolic-link traversal.

`request_id` is a correlation identifier, not a digest of the complete request.
For conclusive results, the current simulation validator additionally binds
the canonical absolute filesystem target and every configuration locator to
exactly one result input record and checks caller-supplied digests. The returned
analysis type must equal the requested type, `tool` must identify the native
tool, `execution.command` must be nonempty, and `execution.exit_code` must carry
the native exit; engineering `pass` requires that native exit to be zero. These
checks do not provide a general request content digest in v0alpha1. After the
provider completes, the host reopens every request input and requires its
identity, size, and digest—and the provider's corresponding retained input
record—to match the pre-launch snapshot. Mutation, replacement, disappearance,
or a conflicting provider input record invalidates the returned evidence even
if the provider otherwise reports a conclusive result.
Provider-manifest conformance records are schema- and cross-reference-checked
self-declarations; the runtime does not fetch their URI or independently verify
their declared evidence digest.

This boundary does not discover, install, rank, authenticate, or trust a
provider. It implements no MCP/session/remote transport, marketplace, network
catalog, credential model, or artifact transfer. See
[Providers, marketplaces, and MCP](PROVIDERS_AND_MCP.md).

### `drc`

Run KLayout `-b` with a caller-supplied rule deck and one exact deck-owned
LYRDB report. Batch mode disables KLayout configuration files and implicit
macros (`-b` is the native `-zz -nc -rx` policy), but the rule deck remains
executable Ruby and can read arbitrary files or cause side effects. OpenADA
does not choose, invent, sandbox, or make an untrusted rule deck authoritative.

Variable-bound mode passes the requested fresh final path through `$report` by
default; `--report-variable` supports another bounded identifier such as
`output`. Script-owned mode uses `--workdir DIR --expect-report RELATIVE_PATH`
and does not inject a report variable. The expected path must be exact, its
real parent must already exist, and it cannot contain traversal or glob syntax.
Both modes pass the GDS through `$input`. `--top-cell` additionally passes
`$topcell` and requires the same nonempty top cell in the native report.
Bounded repeatable `--deck-var NAME=VALUE` options expose other reviewed runset
switches without a shell. The dedicated `input`, `topcell`, and `report` names,
plus the selected report-variable name, are always reserved and cannot be
shadowed by `--deck-var`.

The report and its sibling `.openada.log` transcript must be absent before
launch. OpenADA retains the real report-parent descriptor, revalidates every
directory identity, and captures through that descriptor with no symlink
following. A report must be a stable regular file with one hard link and no
more than 256 MiB. Missing, symbolic, hard-linked, replaced, oversized, or
unstable output produces engineering `unknown`. The report, automatic `.w`
waiver sidecar, and `.openada.log` transcript names are checked against the
anchored filesystem's actual name and path limits before launch. A stable
malformed regular report is still retained and hashed as failure evidence.

The bounded streaming validator requires the native direct `generator`,
`top-cell`, `categories`, `cells`, and `items` sections. The generator must
identify the exact executed deck, the top cell must be declared by the report,
and at least one DRC category must exist; zero items without any registered
category does not prove that a check ran. Violation totals sum each item's
mandatory positive native `multiplicity`. Recursive categories retain their
full native paths so repeated leaf names remain distinct. Qualified cell
variants and KLayout's empty global/dummy cell are modeled without weakening
the requirement for a nonempty declared report top cell. Direct native item
tags determine waiver status; waived markers remain violations and are reported
separately. Normalized category summaries, item/tag counts, violation examples,
geometry values, coordinates, text, XML depth, and total report bytes are
bounded globally.

KLayout automatically consults `<report>.w` as a waiver database. OpenADA
requires that sidecar to be absent unless the exact path is supplied with
`--waiver-file`, in which case it becomes a hashed, stability-checked input.
The explicit waiver is opened through the held parent descriptor with no
symlink following, must be a regular single-link file, and must retain the same
inode and contents through the run. It cannot duplicate the GDS, deck, or a
declared provenance input.

The main GDS and deck plus repeatable `--provenance-input` files are hashed
before launch and rehashed after execution. `%include`, Ruby `load`/`require`,
JSON, environment, and other dynamic accesses cannot be inferred completely,
so results explicitly state that transitive rule inputs and ambient environment
are not enumerated. A frozen runtime manifest can bind that wider state for a
specific conformance workflow.

The transcript artifact contains at most 12,000 retained UTF-8 bytes for each
of the shared bounded stdout and stderr tails, their observed byte counts, and
truncation flags; it is not an unbounded native log. Engineering `pass` requires
completed zero-exit execution, stable inputs and waiver policy, both report and
transcript evidence, a structurally
valid native report, and zero multiplicity-weighted markers. For an otherwise
trustworthy run, one or more markers yield engineering `fail`; missing or
uninterpretable evidence yields `unknown` rather than inferring cleanliness
from process exit.

### `lvs`

Run Netgen in batch LVS mode with caller-supplied layout and schematic
netlists, one bounded cell identifier, an executable setup Tcl, and one exact
final report path. Netgen runs in the resolved current working directory, which
is recorded but is not a sandbox. The setup is configuration and executable
code: OpenADA hashes and stability-checks the declared file, but does not make
an untrusted setup safe or prevent it from reading files and causing side
effects. Because Netgen parses each composite netlist-and-cell argument as a Tcl
list, netlist paths containing whitespace, braces, quotes, or backslashes are
rejected before launch rather than ambiguously re-encoded.

`--report` must name a file with a suffix. Netgen owns that exact comparison
report and the native `-json` output formed by replacing its final suffix with
`.json`; OpenADA owns the sibling `<report>.openada.log` transcript. For
example, `top.lvs.comp` implies native `top.lvs.json` and OpenADA
`top.lvs.comp.openada.log`. All three paths must be absent before launch.
OpenADA anchors their real parent directory and captures by anchored filename
without following links. A native output must be a stable regular file with one
hard link. A stale path, symbolic or hard link, replaced parent, missing output,
oversized or unstable file, or transcript collision cannot support an
engineering conclusion. A stable malformed regular report or JSON file can
still be retained and hashed as diagnostic evidence.

The report validator requires a terminal Netgen outcome bound to the requested
top-cell comparison and the native summary/count structure needed to interpret
it. The separately produced JSON must be bounded UTF-8 JSON without duplicate
keys, contain exactly one requested top comparison, and provide interpretable
device, net, pin, and mismatch fields. Preview ceilings are 256 MiB for the
comparison report, 64 MiB for native JSON, 16 MiB for each completely captured
process stream, and 512 MiB for each declared netlist, setup, or provenance
input. An over-limit input is an invalid request with engineering `unknown`;
the process is not launched.

The OpenADA transcript records those bounded stdout and stderr streams plus
their observed byte counts and truncation flags; it is not an unbounded native
log. A clean transcript requires complete valid UTF-8 streams, Netgen's exact
setup-read marker for the declared setup, no setup-error or stdout-error marker,
the `LVS Done.` marker, and accepted stderr. Accepted stderr is either empty or
consists only of exact reviewed
`Unable to permute model <token> pins <token>, <token>.` lines, where each token
is bounded ASCII text from `A-Z a-z 0-9 _ . $ : + / @ -`. Those native pin-permutation
warnings do not invalidate otherwise trustworthy evidence, but are exposed as
`netgen.stderr_reviewed_warning`; any other stderr line makes the result
`unknown`. This guards against Netgen versions that can continue to a zero exit
and emit match-shaped files after ignoring an error in setup Tcl.

Engineering `pass` requires completed zero-exit execution, unchanged declared
inputs, valid transcript/report/JSON captures, a clean transcript, agreeing
`pass` outcomes from both native outputs, and matching report/JSON device and
net totals. The same trust conditions plus two agreeing mismatch outcomes and
matching structural totals produce engineering `fail`; a mismatch is valid
engineering evidence, not an invocation failure. Any conflict, malformed or
truncated evidence, setup error, nonzero exit, changed input, or incomplete
capture produces `unknown`, never an inferred match or mismatch.

The normalized decision is exposed through `data.lvs_match` and
`data.comparison`, including `report_outcome`, `json_outcome`,
`outcomes_agree`, `structural_counts_agree`, and `evidence_agrees`. Capture
details are in `data.report_output.capture`,
`data.json_output.capture`, and `data.transcript`; evidence artifacts use kinds
`netgen-comparison`, `netgen-comparison-json`, and `netgen-transcript`.
`data.inputs_stable` and `data.changed_inputs` record the post-run input check.
The transcript assessment records `stderr_policy`, `stderr_line_count`,
`stderr_reviewed_warning_count`, `stderr_unrecognized_count`, and
`stderr_accepted` so a consumer can distinguish the reviewed native warning
class from empty or rejected stderr.

The two netlists and setup are always declared inputs. Callers should add known
setup dependencies, PDK revision files, layer maps, or other rule collateral
with repeatable `--provenance-input`; each is hashed before launch, held open,
and checked again afterward. Tcl `source` calls, transitive files, process
environment, and other ambient state cannot be inferred completely. Every
executed result therefore carries the `netgen.provenance_incomplete` warning and
records that transitive setup inputs and ambient environment are not enumerated.

### `rtl-check`

Run Yosys elaboration and `check -assert`, then retain a JSON netlist as an
artifact. This is a structural front-end check, not a complete digital flow.

## Runtime profiles

`native` discovers executables on `PATH` and PDKs from explicit roots or
`PDK_ROOT`. `iic-osic-tools` additionally checks the conventional `/foss/tools`
and `/foss/pdks` layout. `auto` selects the reference profile only when that
layout is present.

Profiles contain environment knowledge. They must not leak container-specific
paths into the contract's semantic operation definitions.
