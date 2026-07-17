# Current preview drivers

This document records the native-tool and deterministic evidence policies in
the OpenADA `0.4.0` development release. Unreleased status is identified in the
changelog. This is an operational reference, not a claim of universal EDA
support or foundry signoff.

All engineering commands return one `openada.result/v0alpha1` JSON object.
Read `execution.status` separately from `engineering.status`: a native process
can complete while the engineering assertion fails or remains unknown. The
result also records bounded diagnostics, input and output hashes, artifact
paths, the exact argv vector and working directory, and provenance.

## Scoped preflight

For a project-scoped first run, state the project root and one intended
engineering assertion instead of inventorying every tool or project file:

```bash
./bin/openada doctor --project-root . \
  --assertion spice-analysis-evidence-valid
```

Scoped preflight accepts one of eight fixed assertion IDs and selects exactly
one smallest semantic operation:

| Assertion | Tool inspected | Next operation |
|---|---|---|
| `schematic-netlist-generated` | Xschem | `netlist` |
| `spice-analysis-evidence-valid` | ngspice | `simulate` |
| `drc-clean` | KLayout | `drc` |
| `lvs-match` | Netgen | `lvs` |
| `rtl-structural-check-passes` | Yosys | `rtl-check` |
| `rtl-lint-clean` | Verilator | `rtl-lint` |
| `asic-netlist-synthesized` | Yosys | `synthesize` |
| `timing-constraints-satisfied` | OpenSTA | `timing-analyze` |

The result records the canonical root, exact binary/version observation,
runtime profile, configured PDK roots, connector startup policy, and one
singular target. It does not walk the project or PDK catalogs, evaluate the
design assertion, or guess a PDK, rule deck, setup, model library, startup
file, or top cell. Those remain explicit inputs to the recommended operation.
An empty scoped-preflight `data.pdks` means the catalog was not enumerated; it
does not mean that no PDK is installed.

## Xschem netlisting

Generate one SPICE netlist from a native Xschem schematic. Pass the real
project or PDK rcfile when symbol and library resolution depends on it:

```bash
./bin/openada netlist project/design.sch \
  --rcfile /path/to/project/xschemrc \
  --output /tmp/openada-netlist/design.spice
```

Engineering pass requires the requested artifact and no recognized unresolved
symbol record. OpenADA does not select a PDK, rcfile, or top-level schematic on
the caller's behalf.

## Shared circuit-simulation alpha

`simulate` now exposes ngspice and Xyce under the same
`openada.operation/circuit.simulate/v1alpha2` intent. The immutable v1alpha1
profile remains available for historical 0.2.x records. An explicit
`--backend` selects the typed shared-profile path; omitting it preserves the
legacy ngspice interface and default:

```bash
./bin/openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend ngspice \
  --output-dir /tmp/ngspice-evidence
./bin/openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend xyce \
  --output-dir /tmp/xyce-evidence
```

The common profile is intentionally smaller than either simulator: one
self-contained OP, DC, AC, or transient analysis, with no includes,
measurements, print directives, control-language blocks, FFT, noise, Monte
Carlo, or multiple analyses. “Same intent” means the same operation, assertion,
status rules, normalized fact names, and artifact roles; it does not promise
that both simulators accept byte-identical native decks. The caller remains
responsible for the native deck and models.

| Backend | OP | DC | AC | TRAN |
|---|---:|---:|---:|---:|
| ngspice | structured | structured | structured | workflow-validated |
| Xyce | unsupported | structured | structured | workflow-validated |

Xyce OP is rejected rather than emulated. OpenADA can inspect the deck's one
top-level analysis, or the caller can supply matching typed flags. For example:

```bash
./bin/openada simulate conformance/circuit-simulate-v0alpha2/fixtures/resistor-divider-dc.cir \
  --backend xyce --analysis dc \
  --source-name VSWEEP --source-unit V --start 0 --stop 1 --step 0.25 \
  --output-dir /tmp/xyce-dc-evidence
./bin/openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-ac.cir \
  --backend ngspice --analysis ac \
  --sweep dec --points 5 --start-hz 10 --stop-hz 10000 \
  --output-dir /tmp/ngspice-ac-evidence
```

Transient flags are `--step-s`, `--stop-s`, and optional `--start-s` and
`--max-step-s`; OP takes no analysis parameters. Typed flags require an
explicit backend and must agree with the deck.

The declared top-level deck is capped at 16 MiB and is rejected before native
launch or hashing beyond that bound. This ceiling is part of the active
operation profile rather than a backend-specific implementation detail.

The OP/DC/AC rows are structured and have pinned, network-disabled native
success evidence in the
[ngspice/Xyce portability replay](../conformance/circuit-simulate-v0alpha2/README.md).
The transient rows retain workflow-validated maturity from the historical
complete workflow. The expanded verifier independently parses native raw
evidence and checks analysis-specific structure and values while tolerating
backend-native point-count differences; its success-only additions do not by
themselves justify workflow-validated maturity. Scoped preflight continues to
select ngspice for `spice-analysis-evidence-valid`; choosing Xyce is explicit
in this alpha.

## Typed evidence and transfer kernels

`extract` is the reviewed native-evidence bridge. It reopens and verifies the
one `simulation.result` artifact recorded by a complete passing typed ngspice or
Xyce simulation, parses one request-bound ngspice binary/ASCII or Xyce ASCII
Spice3 plot, and projects explicitly selected real or imaginary Cartesian
components into a canonical normalized series:

```bash
./bin/openada extract --simulation simulation-result.json \
  --artifact /exact/path/to/simulation.raw \
  --selection series-selection.json
```

The `measure`, `spectral`, `transfer`, and `evaluate` operations are
backend-independent OpenADA kernels rather than EDA drivers:

```bash
./bin/openada measure --series series-or-extraction-result.json \
  --measurement measurement-request.json
./bin/openada spectral --series series-or-extraction-result.json \
  --measurement spectral-request.json
./bin/openada transfer --series series-or-extraction-result.json \
  --measurement transfer-request.json
./bin/openada evaluate --measurement measurement-result.json \
  --specification specification.json
```

`measure`, `spectral`, and `transfer` accept either a bounded normalized real
inline series or one complete passing extraction envelope. The CLI validates
the latter against the packaged extraction profile and unwraps only a verified
embedded series. Each downstream kernel then validates the canonical
axis/signal/condition digest. Optional native-artifact lineage inside that
downstream assertion remains explicitly unverified; retaining the extraction
envelope preserves the upstream native binding.

`measure` has the closed kinds sample-at, min/max, mean, RMS, crossing,
rise/fall time, and settling time. Every coordinate or threshold uses an exact
declared unit. Its structured maturity, together with `evaluate`, is backed by
the
[network-free typed-evidence conformance bundle](../conformance/typed-evidence-v0alpha1/README.md).
The newer extraction, spectral, and transfer kernels have focused profile and
algorithm tests but are not added retroactively to that immutable conformance
record.

Python callers can compute the exact declared digest without reproducing
private serialization details:

```python
from openada.operations import normalized_series_sha256

digest = normalized_series_sha256(
    axis=axis,
    signals=signals,
    conditions=conditions,
)
```

A successful or conclusive not-found measurement retains `request_sha256` for
the complete normalized measurement definition. Specification evaluation
retains the measurement evidence plus the complete normalized specification
and its `specification_sha256`. These digests bind content and expose accidental
or adversarial tampering; they are not signatures, publisher identity, or an
authentication mechanism.

`evaluate` compares one typed finite measurement with explicit lower and/or
upper bounds, inclusive flags, and exact condition bindings. It performs no
unit conversion. Missing/unknown measurements or incompatible units and
conditions remain `unknown`; only valid matched evidence outside a bound is
specification `fail`.

`spectral` implements one fixed coherent rectangular-window partition for SNR,
SINAD, signed-dB THD, and SFDR. It requires a uniformly spaced power-of-two time
record and exact coherent fundamental bin; it does not silently substitute
SNDR, ENOB, fitting, averaging, PSD, jitter, or phase-noise methods. Its
standards contexts are candidate mappings rather than IEEE conformity claims.

`transfer` constructs output/input from four distinct same-unit Cartesian AC
series on a positive-Hz axis. It reports the first simulated-frequency gain,
the unique falling first-point-minus-3 dB crossing, the unique falling 0 dB
crossing, or—only for explicitly declared negative-feedback loop gain—phase
margin. It does not call the first point DC, infer gain margin, choose among
multiple crossings, or make a general stability claim.

Inspect the complete packaged ontology without guessing IDs:

```bash
./bin/openada profile list
./bin/openada profile show openada.operation/result.transfer.measure/v1alpha1
```

The catalog contains nine active profiles plus the immutable historical
`circuit.simulate/v1alpha1` profile. Catalog presence is not an
external-provider capability.

## Explicit external-provider runtime

`provider validate` and `provider list` inspect one explicitly supplied
manifest. `provider invoke` resolves one unambiguous local JSON-stdio `wait`
capability, currently only for `circuit.simulate/v1alpha2`. It requires an exact
driver selector; it does not discover, install, rank, approve, or connect to
MCP/session/remote providers.

That binding accepts only canonical absolute regular non-symlink filesystem
files for the target and each configuration. The target is limited to 16 MiB,
each configuration to 256 MiB, and their aggregate to 512 MiB. Before launch
the host snapshots identity, size, and SHA-256 and verifies every digest the
request declares. The evidence destination must be canonical and absolute, have
an existing canonical non-linked parent, be absent before launch, and use
`fail-if-present`; every returned artifact path must remain inside it without
symbolic-link escape.

Invocation requires a zero transport-process exit and empty stderr, bounds the
request, result, diagnostics, and timeout, and cleans up the fresh process group
even after the parent returns. Only descendants that remain in that group are
killed; a deliberately detached process is outside this containment, which is
not a sandbox. The executable and standalone argv values that already name
regular files are canonicalized and identity-checked before and after launch.
Returned local input/artifact files are reopened and verified against their
declared size and SHA-256. A conclusive circuit result must match the requested
analysis and retain native tool identity, a nonempty command, and a native exit
code; pass requires native exit zero. The echoed `request_id` is correlation,
not a whole-request digest. Manifest conformance evidence is self-declared
metadata: its schema and internal references are validated, but its URI is not
fetched and its declared digest is not independently rehashed. Before accepting
the result, the host reopens every request input and requires identity, size,
SHA-256, and the provider-retained input record to match the pre-launch
snapshot. Mutation, replacement, or disappearance invalidates the evidence.

## ngspice simulation

Run the included fixture when ngspice is installed:

```bash
./bin/openada simulate fixtures/smoke/smoke_ngspice.cir \
  --output-dir /tmp/openada-smoke
```

That command uses the default streaming `batch` mode. For a deck with
`.measure`, `.control`, or unenumerated model includes, select `control`
explicitly. Work from a task-local copy and declare every required `write` or
`wrdata` evidence file; paths are relative to the process working directory:

```bash
mkdir -p /tmp/inverter-task
cp project/inverter_tb.spice /tmp/inverter-task/inverter_tb.spice
./bin/openada simulate /tmp/inverter-task/inverter_tb.spice \
  --execution-mode control \
  --init-file /path/to/pdk/.spiceinit \
  --system-init-file /path/to/ngspice/scripts/spinit \
  --workdir /tmp/inverter-task \
  --expect-output raw=test_inverter.raw \
  --output-dir /tmp/inverter-evidence
```

The ngspice control path is retained for compatibility but is outside the
ngspice/Xyce common alpha subset. The project/PDK init and optional system
`spinit` are hashed. Explicit startup
disables local and user `.spiceinit`; supplying `--system-init-file` also pins
the otherwise native, runtime-dependent system initialization. Deck-owned
outputs must not already exist. OpenADA captures only declared paths and
structurally validates raw/`wrdata` evidence; it does not scan the project
directory or sandbox arbitrary `.control` side effects. Preview validation is
bounded to 16 MiB per deck or explicit init input, 16 MiB logs, and 256 MiB per
raw/`wrdata` artifact.

Transitive model and include inputs are not yet completely enumerated in
control mode and are rejected in batch mode. Use a reviewed flattened deck for
batch mode, or report the provenance limitation with control-mode results.

## KLayout DRC

KLayout reports are explicit deck-owned evidence. For a parameterized deck
that calls `report(..., $report)`, give OpenADA one fresh final path:

```bash
mkdir -p /tmp/drc-evidence
./bin/openada drc project/layout.gds \
  --rules /path/to/pdk/rules.drc \
  --top-cell TOP \
  --provenance-input /path/to/pdk/COMMIT \
  --report /tmp/drc-evidence/layout.lyrdb
```

The report and its `.openada.log` transcript must not already exist. OpenADA
passes the exact report path to KLayout, anchors its real parent directory,
and accepts only a stable regular single-link LYRDB whose native generator and
top-cell identity match the invocation. A self-contained deck with a literal
report path can instead use `--workdir DIR --expect-report RELATIVE_PATH`;
OpenADA then does not inject a report variable. KLayout automatically consults
`<report>.w` for waivers, so an ambient sidecar is rejected unless that exact
file is declared with `--waiver-file` and hashed. Additional `%include`, JSON,
or PDK files can be recorded with repeatable `--provenance-input`; arbitrary
Ruby file access cannot be inferred completely and is reported as a provenance
limitation.

OpenADA reserves KLayout's `input`, `topcell`, and `report` bindings plus any
custom report-variable name, validates the report and both derived sidecar
names against the anchored filesystem, and keeps an explicitly declared waiver
database open by descriptor so even an identical-content inode replacement
invalidates the result.

### Diagnostic DRC views

After retaining a native KLayout report, generate AI- and human-reviewable
views without re-running the rule deck:

```bash
./bin/openada drc-review project/layout.gds \
  --report /tmp/drc-evidence/layout.lyrdb \
  --layer-properties /path/to/pdk/layers.lyp \
  --output-dir /tmp/drc-review
```

The output directory must be fresh and empty. `drc-review` validates the native
LYRDB, retains only its bounded geometry examples, deduplicates equivalent
KLayout cell variants, and asks KLayout to expand leaf-cell markers through the
GDS hierarchy. It writes one full-layout PNG and ranked occurrence-level
cluster PNGs, then checks their native PNG headers, exact requested dimensions,
paths, sizes, hashes, renderer summary, and unchanged inputs.

The result's `data.diagnosis` section gives future agents deterministic,
bounded facts alongside the images: lexical rule-family classification,
declared length values parsed from native rule descriptions, marker dimensions
and centers, and—for off-grid rules—the distance from each retained coordinate
to the nearest declared grid line. These are triage observations rather than
repair instructions; marker geometry is not treated as a reconstructed deck
measurement.

This operation proves that the declared review artifacts were generated and
validated. It does not establish DRC cleanliness, execute a rule deck, infer
unreported markers, or turn representative bounded examples into exhaustive
signoff evidence. The original LYRDB and its deck remain authoritative.

## Netgen LVS

Netgen LVS also uses explicit, fresh evidence. Give it the project's executable
setup Tcl and one final report path with a filename suffix:

```bash
mkdir -p /tmp/lvs-evidence
./bin/openada lvs project/layout.spice project/schematic.spice \
  --cell TOP \
  --setup /path/to/pdk/setup.tcl \
  --provenance-input /path/to/pdk/COMMIT \
  --report /tmp/lvs-evidence/top.lvs.comp
```

For that report name, Netgen owns `top.lvs.comp` and its native `-json` output
`top.lvs.json`; OpenADA owns the bounded transcript
`top.lvs.comp.openada.log`. All three paths must be absent before launch.
Engineering `pass` or `fail` requires stable hashed inputs, completed zero-exit
execution, a complete clean setup/completion transcript, and structurally valid
native report and JSON outcomes whose device/net totals also agree. Stderr must
be empty or consist only
of exact reviewed `Unable to permute model <token> pins <token>, <token>.`
warning lines; accepted lines remain visible as
`netgen.stderr_reviewed_warning`. A unique match is `pass`, a trustworthy
mismatch is `fail`, and stale, linked, missing, malformed, conflicting,
unrecognized-stderr, or truncated evidence is `unknown` rather than an inferred
result.

The setup is caller-supplied executable Tcl; OpenADA hashes it but does not
sandbox it or infer every transitive file and ambient dependency it may read.
Add known setup, PDK, layer-map, or revision files with repeatable
`--provenance-input`. Executed LVS results retain the explicit
`netgen.provenance_incomplete` warning. The preview stability checker accepts
declared inputs up to 512 MiB each; a larger netlist or rules file is rejected
as `unknown` before launch rather than hashed without a bound.

## Yosys RTL checks

Elaborate Verilog or SystemVerilog and run the preview structural checks:

```bash
./bin/openada rtl-check rtl/top.sv rtl/block.sv \
  --top top \
  --output-dir /tmp/openada-rtl-evidence
```

The structural Yosys operation has a pinned public SAR replay. It remains a
front-end structural assertion, not functional or implementation proof.

## Digital lint, ASIC synthesis, and timing

Use exact ordered sources and frontend context for strict lint, then preserve
one technology/mapping context for synthesis and timing:

```bash
./bin/openada rtl-lint rtl/package.sv rtl/top.sv --top top \
  --include-dir rtl/include --output-dir /tmp/openada-lint

./bin/openada synthesize rtl/package.sv rtl/top.sv --top top \
  --frontend slang --include-dir rtl/include \
  --liberty platform/typical.lib --techmap platform/cells_latch.v \
  --abc-constraint platform/abc.constr --abc-delay-target-ns 2.0 \
  --output-dir /tmp/openada-synthesis

./bin/openada timing-analyze /tmp/openada-synthesis/mapped.v --top top \
  --liberty platform/typical.lib --sdc constraints/top.sdc \
  --output-dir /tmp/openada-timing
```

`rtl-lint` uses a strict warning policy. `synthesize` retains both generic
inference and mapped statistics and passes only when every mapped cell belongs
to the exact Liberty. It also binds the external ABC executable by exact path,
accepted version, bytes, and SHA-256, supplies that path through `abc -exe`,
and uses one closed non-inheriting environment for both tool probes and the
native run. The built-in synthesis frontend reports its dialect as
`yosys-sv`; only Slang accepts `1800-2017` or `1800-2023`, and those selectors
are not an IEEE-conformance claim. `timing-analyze` accepts the closed
declarative `openada-sdc-v1` subset, executes a fresh hash-identical SDC
snapshot, and validates constraint completeness,
scalar/path-report agreement, and setup/hold WNS/TNS in seconds. Its v1 model
is one corner with ideal interconnect and no SPEF; it is not MCMM or signoff.
The accepted OpenSTA version probe and analysis share
`closed-opensta-runtime-v1`, so ambient loader, interpreter, Tcl, OpenSTA, and
shell-control variables cannot change the declared run.

Standards context is deliberately scoped. The lint `1800-2017` and
`1800-2023` selectors name editions of the
[IEEE SystemVerilog standard](https://standards.ieee.org/ieee/1800/7743/), but
a successful frontend run is not an IEEE-conformance certification. IEEE also
publishes [IEEE 1481-2019 OLA](https://standards.ieee.org/ieee/1481/7651/) for
portable IC library architecture and
[IEEE/IEC 61523-3-2004](https://standards.ieee.org/ieee/61523-3/3644/) for SDF.
This timing profile consumes the declared Liberty and validated SDC snapshot,
reads no SDF, and therefore claims neither OLA nor SDF alignment. Its WNS/TNS values are
OpenSTA evidence normalized to SI seconds, not a separate standards-conformant
measurement method.

## Maturity and discoverable tools

| Operation | Native tool | Maturity | Preview behavior |
|---|---|---|---|
| `doctor` | runtime | preview | Discover capabilities, or preflight one project assertion without catalog inventory |
| `netlist` | Xschem | workflow-validated | Produce a SPICE netlist and fail on recognized unresolved symbols |
| `simulate` (legacy default) | ngspice | workflow-validated | Stream wrapper raw files in batch mode, or validate declared deck-owned raw/`wrdata` outputs in control mode |
| `simulate --backend ngspice` | ngspice | structured OP/DC/AC; workflow-validated TRAN | Run one self-contained OP, DC, AC, or transient analysis and emit typed normalized facts |
| `simulate --backend xyce` | Xyce | structured DC/AC; workflow-validated TRAN | Run one self-contained DC, AC, or transient analysis; OP is unsupported |
| `extract` | deterministic Spice3 evidence kernel | structured alpha | Verify one typed simulation/raw-artifact pair and project selected native Cartesian vectors into a canonical real series |
| `measure` | deterministic OpenADA kernel | structured alpha | Derive one typed scalar from a canonical normalized real series or passing extraction envelope |
| `spectral` | deterministic OpenADA kernel | structured alpha | Derive coherent single-tone SNR, SINAD, signed-dB THD, or SFDR under one fixed partition |
| `transfer` | deterministic OpenADA kernel | structured alpha | Derive one closed AC output/input gain, crossing, or explicitly declared negative-feedback phase-margin scalar |
| `evaluate` | deterministic OpenADA kernel | structured alpha | Evaluate exact-unit bounds and explicit conditions over one typed measurement |
| `drc` | KLayout | workflow-validated | Validate one exact fresh deck-owned `.lyrdb`, weighted violations, and bounded transcript evidence |
| `lvs` | Netgen | workflow-validated | Validate agreeing fresh native report/JSON plus a clean bounded setup transcript |
| `rtl-check` | Yosys | structured alpha | Elaborate SystemVerilog/Verilog and run structural checks |
| `rtl-lint` | Verilator | workflow-validated | Strict warning/error lint with hashed RTL/include evidence |
| `synthesize` | Yosys + ABC | workflow-validated | Bind external ABC by version/digest and validate a complete flattened Liberty-mapped ASIC netlist and statistics |
| `timing-analyze` | OpenSTA | workflow-validated | Validate one-corner setup/hold timing evidence with explicit limitations |

Magic, OpenROAD, Icarus Verilog, Surelog, standalone slang, OpenVAF,
Qucs-S, GTKWave, and LibreLane are currently discoverable but do not yet have a
stable structured operation in the preview contract.

Xschem-to-ngspice simulation, KLayout DRC, Netgen LVS, and Verilator lint pass
pinned public IHP cases. Mapped synthesis and timing use pinned public ORFS
Ibex/Nangate45 evidence. See the
[driver roadmap](ROADMAP.md) for the exact maturity policy and validation
status.
