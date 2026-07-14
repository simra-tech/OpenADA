# Current preview drivers

This document records the native-tool policies implemented by the OpenADA
`0.1.0` preview. It is an operational reference for the drivers that exist
today, not a claim of universal EDA support or foundry signoff.

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

Scoped preflight accepts one of five fixed assertion IDs and selects exactly
one smallest semantic operation:

| Assertion | Tool inspected | Next operation |
|---|---|---|
| `schematic-netlist-generated` | Xschem | `netlist` |
| `spice-analysis-evidence-valid` | ngspice | `simulate` |
| `drc-clean` | KLayout | `drc` |
| `lvs-match` | Netgen | `lvs` |
| `rtl-structural-check-passes` | Yosys | `rtl-check` |

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
`openada.operation/circuit.simulate/v1alpha1` intent. An explicit `--backend`
selects the typed shared-profile path; omitting it preserves the legacy
ngspice interface and default:

```bash
./bin/openada simulate conformance/circuit-simulate/fixtures/rc-transient.cir \
  --backend ngspice \
  --output-dir /tmp/ngspice-evidence
./bin/openada simulate conformance/circuit-simulate/fixtures/rc-transient.cir \
  --backend xyce \
  --output-dir /tmp/xyce-evidence
```

The common profile is intentionally smaller than either simulator: one
self-contained `.TRAN` analysis, with no includes, measurements, or
control-language blocks. “Same intent” means the same operation, assertion,
status rules, normalized fact names, and artifact roles; it does not promise
that both simulators accept byte-identical native decks. The caller remains
responsible for the native deck and models.

ngspice is workflow-validated. The Xyce driver has deterministic synthetic
contract tests, but this development server has no native Xyce binary, so its
native command and artifact mapping still require a pinned public replay before
it can be called workflow-validated. Scoped preflight continues to select
ngspice for `spice-analysis-evidence-valid`; choosing Xyce is explicit in this
alpha.

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
bounded to 16 MiB logs and 256 MiB per raw/`wrdata` artifact.

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

The Yosys operation is structured alpha. It has pinned native-design evidence,
but its own clean public workflow recipe is still pending.

## Maturity and discoverable tools

| Operation | Native tool | Maturity | Preview behavior |
|---|---|---|---|
| `doctor` | runtime | preview | Discover capabilities, or preflight one project assertion without catalog inventory |
| `netlist` | Xschem | workflow-validated | Produce a SPICE netlist and fail on recognized unresolved symbols |
| `simulate` (legacy default) | ngspice | workflow-validated | Stream wrapper raw files in batch mode, or validate declared deck-owned raw/`wrdata` outputs in control mode |
| `simulate --backend ngspice` | ngspice | structured shared profile | Run the shared self-contained transient subset and emit typed normalized facts |
| `simulate --backend xyce` | Xyce | structured alpha | Run the shared self-contained transient subset and validate a fresh native raw artifact; synthetic-contract-tested only |
| `drc` | KLayout | workflow-validated | Validate one exact fresh deck-owned `.lyrdb`, weighted violations, and bounded transcript evidence |
| `lvs` | Netgen | workflow-validated | Validate agreeing fresh native report/JSON plus a clean bounded setup transcript |
| `rtl-check` | Yosys | structured alpha | Elaborate SystemVerilog/Verilog and run structural checks |

Magic, OpenROAD, Icarus Verilog, Verilator, Surelog, slang, OpenVAF,
Qucs-S, GTKWave, and LibreLane are currently discoverable but do not yet have a
stable structured operation in the preview contract.

Xschem-to-ngspice simulation, KLayout DRC, and Netgen LVS pass pinned public IHP
inverter conformance cases. The other structured drivers have real native or
pinned-design evidence but do not yet have a public workflow recipe. See the
[driver roadmap](ROADMAP.md) for the exact maturity policy and validation
status.
