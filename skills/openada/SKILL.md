---
name: openada
description: Discover, invoke, and interpret open-source EDA tools through OpenADA's versioned CLI contract. Use for semiconductor or electronics work involving Xschem schematics (.sch), SPICE simulation, GDS/KLayout DRC, Netgen LVS, Verilog/SystemVerilog lint, Yosys ASIC synthesis, OpenSTA timing, PDK discovery, or diagnosing an open EDA environment in Codex, Claude Code, or another terminal-capable agent.
---

# OpenADA

Use the deterministic OpenADA CLI between agent reasoning and native EDA tools. Keep native design files, PDKs, and rule decks as the source of truth.

This is the plugin's execution and evidence skill. It maps a bounded intent to
OpenADA operations and interprets their versioned results. Higher-level
engineering skills may compose these operations into review or diagnosis
workflows, but they must not duplicate backend commands or redefine contract
status. Keep tool-native safety policy here or in the deterministic driver;
keep reusable engineering judgment in a separate sibling skill.

## Establish the workspace

1. Identify the intended project root before running or modifying a design.
2. Treat PDK trees, shared catalogs, and reference designs as read-only unless the user says otherwise.
3. Put generated evidence under a task-local output directory. Do not overwrite source artifacts unless the user explicitly requests it.

## Resolve the CLI

Prefer `openada` when it is on `PATH`.

Agent plugin managers install this skill bundle, not the OpenADA Python package
or its `jsonschema>=4.18` dependency. The recommended setup therefore installs
the matching OpenADA Python release so `openada` is on `PATH`.

When this skill is loaded from a plugin checkout or cache, resolve the plugin
root from this `SKILL.md` path and use `<plugin-root>/bin/openada` only if the
command is not on `PATH` and that launcher's Python dependencies are available.
Do not guess a different installation path. If a command returns
`provider.validation.unavailable`, stop and tell the user to install the
matching OpenADA Python release; do not present the failed profile/provider
validation as an EDA result.

If neither entry point exists, stop the EDA operation and tell the user that the OpenADA executable is missing. Do not silently fall back to an unstructured raw-tool workflow while claiming OpenADA evidence.

## Inspect before executing

For project work, map the user's immediate engineering intent to exactly one
fixed assertion, then run one scoped preflight:

```bash
openada doctor --project-root /absolute/project \
  --assertion spice-analysis-evidence-valid
```

Use these one-to-one mappings:

| User's immediate assertion | Preflight assertion | Target |
|---|---|---|
| Generate a resolved schematic netlist | `schematic-netlist-generated` | Xschem `netlist` |
| Run a SPICE analysis with valid evidence | `spice-analysis-evidence-valid` | ngspice `simulate` |
| Determine whether the supplied DRC deck is clean | `drc-clean` | KLayout `drc` |
| Determine whether two netlists match under the supplied setup | `lvs-match` | Netgen `lvs` |
| Elaborate RTL and pass structural checks | `rtl-structural-check-passes` | Yosys `rtl-check` |
| Lint SystemVerilog with no warnings or errors | `rtl-lint-clean` | Verilator `rtl-lint` |
| Produce a complete Liberty-mapped ASIC netlist | `asic-netlist-synthesized` | Yosys `synthesize` |
| Satisfy setup and hold constraints for one declared corner | `timing-constraints-satisfied` | OpenSTA `timing-analyze` |

If the request spans a chain, preflight only the smallest next assertion whose
result is needed before later work. Do not run several preflights or recommend
a whole flow at once. `--project-root` and `--assertion` are paired; do not add
`--tool` or `--require` because the assertion selects and requires one tool.

Read `data.preflight.target` as the one recommendation. A preflight `pass`
only establishes point-in-time root and tool readiness:
`data.preflight.assertion_evaluated` remains false. It does not inspect the
project, choose a PDK, or run the engineering assertion. An empty `data.pdks`
means `data.preflight.pdk.catalog_enumerated` is false, not that no PDK exists.
Do not search recursively to compensate.

Inspect portable operation parameters from the installed catalog, independent
of the current working directory:

```bash
openada profile list
openada profile show openada.operation/result.series.extract/v1alpha1
openada profile show openada.operation/result.transfer.measure/v1alpha1
```

Preflight intentionally leaves `pdk.selected` and startup `selected_files`
unresolved. Identify exact project-specific source, PDK, model, rcfile, init,
rule deck, setup, top cell, and output paths from explicit user/project context.
If more than one candidate is plausible, ask the user which is authoritative.
Never guess a conventional filename, inspect `$HOME` startup files, crawl a PDK
tree, or substitute generic collateral.

For environment diagnosis outside a concrete project assertion, a focused
legacy probe remains available:

```bash
openada doctor --tool xschem --require xschem
openada doctor --tool ngspice --require ngspice
```

Put global runtime options before the command:

```bash
openada --profile native doctor
openada --profile iic-osic-tools doctor
openada --tool-path ngspice=/absolute/path/to/ngspice doctor --tool ngspice
```

Use the detected runtime and PDK roots; do not assume `/foss` exists. The IIC-OSIC-TOOLS layout is a reference profile, not a requirement.

## Choose a semantic operation

Use the smallest operation that proves the requested engineering assertion:

```bash
mkdir -p evidence

# Xschem schematic -> SPICE; pass the project/PDK rcfile when library resolution depends on it
openada netlist design.sch --rcfile path/to/xschemrc --output evidence/design.spice

# streaming ngspice simulation without .measure or .control
openada simulate testbench.spice --workdir path/to/project \
  --output-dir evidence/simulation

# typed same-intent transient profile through either reviewed built-in mapping
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend ngspice --output-dir evidence/shared-ngspice \
  > evidence/shared-ngspice-result.json
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend xyce --output-dir evidence/shared-xyce \
  > evidence/shared-xyce-result.json

# native control deck; declare every write/wrdata file relative to --workdir
openada simulate testbench.spice --execution-mode control \
  --init-file path/to/pdk/.spiceinit \
  --system-init-file path/to/ngspice/scripts/spinit \
  --workdir path/to/writable-project-copy \
  --expect-output raw=testbench.raw \
  --output-dir evidence/simulation

# KLayout DRC with the project's real deck and one fresh exact report
mkdir -p /tmp/openada-drc-evidence
openada drc layout.gds \
  --rules path/to/rules.drc \
  --workdir path/to/project \
  --top-cell TOP \
  --provenance-input path/to/pdk/COMMIT \
  --report /tmp/openada-drc-evidence/drc.lyrdb

# Netgen LVS with the project's real setup and fresh evidence basename
mkdir -p /tmp/openada-lvs-evidence
openada lvs layout.spice schematic.spice --cell top \
  --setup path/to/setup.tcl \
  --provenance-input path/to/pdk/COMMIT \
  --report /tmp/openada-lvs-evidence/top.lvs.comp

# Yosys elaboration and structural checks
openada rtl-check rtl/top.sv rtl/block.sv --top top --output-dir evidence/rtl

# Strict SystemVerilog lint; ordered sources, includes, and defines are part of the evidence
openada rtl-lint rtl/package.sv rtl/top.sv --top top \
  --include-dir rtl/include --define SYNTHESIS=1 \
  --output-dir evidence/rtl-lint

# Flatten and map to one exact Liberty; retain generic inference and mapped statistics
openada synthesize rtl/package.sv rtl/top.sv --top top \
  --frontend slang --include-dir rtl/include \
  --liberty platform/typical.lib --techmap platform/cells_latch.v \
  --abc-constraint platform/abc.constr --abc-delay-target-ns 2.0 \
  --output-dir evidence/synthesis

# Single-corner synthesis-stage timing with exact mapped netlist, Liberty, and SDC
openada timing-analyze evidence/synthesis/mapped.v --top top \
  --liberty platform/typical.lib --sdc constraints/top.sdc \
  --output-dir evidence/timing
```

Never substitute a generic DRC deck, LVS setup, PDK, model library, or top cell merely to obtain a passing result. Ask for the missing project-specific input.

For digital commands, preserve source order, declared language dialect/revision, include
directories, defines, top, Liberty, mapping policy, SDC, and tool identity as
one comparison context. `rtl-lint` uses a strict policy: any recognized warning
or error is an engineering `fail`. `synthesize` positively identifies and
content-binds the exact external ABC executable under the same closed runtime
environment as Yosys; operation-level evidence, not primary-tool preflight, is
the authoritative ABC gate. `synthesize` passes only with fresh mapped
netlist/statistics evidence, zero processes or memories after mapping, and no
cell type outside the declared Liberty. Read `data.inference_stats` separately
from `data.stats`; synthesis success does not establish behavioral equivalence,
timing, area-budget, or power success. `timing-analyze` is intentionally one
corner with ideal interconnect and no SPEF. A negative WNS is a trustworthy
constraint failure only when `constraints_complete`, `reports_complete`,
`inputs_stable`, `metric_consistency`, and path-report agreement establish
complete evidence. Even a timing pass is not MCMM or physical signoff.
The timing connector accepts only `openada-sdc-v1` declarative constraints and
executes a fresh snapshot whose hash equals the declared SDC input; arbitrary
Tcl, sourced files, environment access, and `read_spef` are unsupported.
Its version probe and analysis share `closed-opensta-runtime-v1` rather than
inheriting ambient loader, interpreter, Tcl, OpenSTA, or shell-control state.

An explicit `--backend ngspice|xyce` selects the typed
`openada.operation/circuit.simulate/v1alpha2` bridge. Its initial common subset
is exactly one self-contained top-level `.op`, `.dc`, `.ac`, or `.tran` with
parseable closed arguments. Require the selected driver's matching advertised
feature: ngspice supports OP/DC/AC/TRAN; Xyce supports DC/AC/TRAN and rejects
OP. The bridge rejects includes, measurements, print directives, control
blocks, and multiple analyses. Do not combine this path with legacy
ngspice-only options.
Omitting `--backend` preserves the broader legacy ngspice batch/control
interface. Scoped preflight also remains mapped to ngspice in this alpha; Xyce
selection is explicit.

## Continue from native evidence to a specification

A shared-profile simulation pass can feed the implemented typed evidence
chain. Keep every result envelope as JSON; do not copy values from a log.

First create a closed selection document:

```json
{
  "selectors": [
    {
      "native_name": "v(out)",
      "output_name": "v(out)",
      "unit": "V",
      "component": "real"
    }
  ],
  "conditions": [
    {"name": "temperature", "value": 27, "unit": "degC"},
    {"name": "corner", "value": "tt", "unit": "1"}
  ],
  "extensions": {}
}
```

Then bind the exact retained `simulation.result` artifact named by the complete
simulation envelope:

```bash
openada extract \
  --simulation evidence/simulation-result.json \
  --artifact /absolute/evidence/simulation.raw \
  --selection evidence/series-selection.json \
  > evidence/series-extraction.json
```

The later `measure`, `spectral`, and `transfer` commands accept this complete
passing extraction envelope directly; `data.extraction.series` remains the
embedded canonical series for programmatic use. Extraction requires the exact
passing `circuit.simulate/v1alpha2` envelope and matching
canonical path, byte count, and digest. It supports ngspice binary/ASCII and
Xyce ASCII padded Spice3 raw evidence. Select exact real or imaginary Cartesian
components; it never derives magnitude, phase, differential expressions, or
unit conversions. Conditions are caller-declared and digest-bound, not inferred
from simulator state. Retain the extraction envelope beside downstream results:
it carries the verified native binding that immutable measurement lineage still
labels `unverified`.

For an ordinary scalar, write one closed measurement object and run:

```bash
openada measure \
  --series evidence/series-extraction.json \
  --measurement evidence/measurement-request.json \
  > evidence/measurement-result.json
```

Advertised kinds are `sample_at`, `minimum`, `maximum`, `mean`, `rms`,
`crossing`, `rise_time`, `fall_time`, and `settling_time`. Inspect
the installed profile with `openada profile show
openada.operation/result.measure/v1alpha1` for the exact unit-bearing parameter
shape. Do not use native `.measure` or an arbitrary expression as a substitute.

For a uniformly sampled coherent single-tone record, inspect the packaged
spectral profile, write its complete method, and run:

```bash
openada spectral \
  --series evidence/series-extraction.json \
  --measurement evidence/spectral-request.json \
  > evidence/spectral-result.json
```

The alpha supports only SNR, SINAD, signed-dB THD, and SFDR under its fixed
power-of-two rectangular coherent-bin method. A candidate IEEE context records
application scope; it is not a conformity claim.

For an AC analysis with explicit same-unit input/output real and imaginary
series, inspect `openada.operation/result.transfer.measure/v1alpha1` and run:

```bash
openada transfer \
  --series evidence/ac-series-extraction.json \
  --measurement evidence/transfer-request.json \
  > evidence/transfer-result.json
```

The closed profile supports first-positive-frequency gain, unique falling
−3 dB bandwidth, unity-gain frequency, and explicitly declared
negative-feedback phase margin. It rejects ambiguous multiple crossings and
does not implement gain margin.

Finally evaluate a scalar result only against an already supplied explicit
limit and exact conditions:

```bash
openada evaluate \
  --measurement evidence/measurement-result.json \
  --specification evidence/specification.json \
  > evidence/specification-result.json
```

`evaluate` accepts ordinary, spectral, or transfer measurement envelopes. A
measurement pass is not a specification pass, and a missing limit is not
permission to invent one.

## Use one explicit external provider

The first provider boundary is opt-in and deliberately smaller than a
marketplace:

```bash
openada provider validate path/to/driver-manifest.json
openada provider list --manifest path/to/driver-manifest.json
openada provider invoke \
  --manifest path/to/driver-manifest.json \
  path/to/openada-request.json
```

Invocation requires a complete `openada.request/v0alpha1` with an exact
`driver_selector`. OpenADA validates the manifest and cross-references,
resolves one structured or workflow-validated local CLI capability, writes one
bounded JSON request to stdin without a shell, and validates the returned base
and operation-specific result plus request correlation and provider identity.
External dispatch is currently registered only for
`openada.operation/circuit.simulate/v1alpha2`. Its target and configuration
locators must be canonical absolute filesystem paths; its fresh evidence
destination must also be canonical and uses `fail-if-present`, with returned
artifacts confined beneath it.
The immutable v0alpha1 bridge does not carry a digest of the complete request;
do not describe correlation as cryptographic or complete request binding. The
default working directory is the manifest directory; pass `--cwd` only when
the provider contract requires another reviewed directory.

Manifest conformance records are self-declared metadata. The runtime checks
their schema and cross-references but does not fetch their URI or independently
rehash the referenced evidence, so do not present manifest validation as an
independent conformance result.

There is no implicit provider discovery, installation, ranking, marketplace,
MCP binding, credential model, session transport, or remote job transport in
this command. Never describe an explicit manifest list as an EDA marketplace.

`simulate` runs from the netlist directory by default. An explicit `--workdir`
changes native relative-path resolution; it is not a sandbox, and ngspice may
write files there. Prefer a task-local writable project copy when the source
catalog is read-only. Transitive model/include files are not yet enumerated or
hashed by the preview contract, so report that provenance limitation.

For the legacy ngspice interface, choose the mode explicitly when the default
batch policy is not appropriate:

- Keep the default `batch` mode for bounded streaming runs whose reviewed,
  flattened top-level deck has no `.measure`, `.control`, pure control script,
  `.include`, `.inc`, or `.lib` directive.
- Use `control` for `.measure` and `.control` decks. If the deck calls
  `write` or `wrdata`, repeat `--expect-output raw=...` or
  `--expect-output wrdata=...` for every required file.
- Pass a real project/PDK init script with `--init-file` when simulation
  depends on one. This disables local/user `.spiceinit` and records the init
  file as a hashed input; it does not disable system `spinit`.
- For a reproducible startup chain, also pass the runtime's reviewed system
  `spinit` via `--system-init-file`. OpenADA pins it by hash and selects its
  directory through `SPICE_SCRIPTS`.

Never treat an undeclared file found by directory scanning as current-run
evidence. A declared deck output must be absent before launch and `pass`
requires OpenADA's structural validation, not just file presence.
The preview rejects logs over 16 MiB and raw/`wrdata` evidence over 256 MiB;
split or reduce evidence rather than treating a bounded-validation rejection as
an engineering failure.

For KLayout, the exact report and its sibling `.openada.log` transcript must be
absent before launch. OpenADA binds the report path through the deck variable
named `report` by default; use `--report-variable NAME` for a reviewed deck that
expects another variable. If the deck itself owns a fixed relative report path,
use `--expect-report relative/path.lyrdb` with `--workdir` instead of
`--report`; OpenADA then injects no report variable. Pass additional bounded
`NAME=VALUE` bindings with repeatable `--deck-var` and select the native top
cell with `--top-cell` when the deck supports `topcell`. Do not try to shadow
`input`, `topcell`, `report`, or the selected report-variable name with a deck
variable; those bindings are reserved by the connector.

KLayout automatically reads `<report>.w`. Do not leave that sidecar ambient:
either keep it absent or declare that exact existing file with `--waiver-file`
so it becomes a hashed, stability-checked configuration input. Use repeatable
`--provenance-input` for known `%include`, JSON, layer-map, or PDK revision files
loaded by the executable Ruby deck. OpenADA cannot infer arbitrary Ruby file
access, and `--workdir` controls relative paths but is not a sandbox. A stable
malformed report may be retained by hash for diagnosis, but only a bounded
native-shaped LYRDB with the executed deck generator, matching requested top
cell, a nonempty category catalog, declared cells, a native items section, and
interpretable multiplicity-weighted markers can support `pass` or `fail`.

For Netgen, keep the exact report, its native JSON path formed by replacing the
final suffix with `.json`, and `<report>.openada.log` absent before launch. Use a
fresh basename instead of deleting or reusing prior evidence. Treat setup Tcl as
caller-supplied executable code and run from an appropriate current working
directory; OpenADA records that directory but does not sandbox Tcl side effects.
Declare known sourced setup, layer-map, PDK, and revision files with repeatable
`--provenance-input`. Report the `netgen.provenance_incomplete` warning because
transitive Tcl access and ambient environment state remain unenumerated. The
preview rejects each declared input above 512 MiB before launch; report that
limit instead of attempting to bypass the stability check.

Accept Netgen `pass` or `fail` only from the normalized result. It requires
unchanged declared inputs, completed zero-exit execution, a complete clean
setup/completion transcript, and valid native report and JSON outcomes whose
device/net totals also agree. Accept stderr only when it is empty or every line
exactly matches the reviewed
`Unable to permute model <token> pins <token>, <token>.` grammar. Each token is
bounded ASCII text from `A-Z a-z 0-9 _ . $ : + / @ -`. Report accepted lines
through `netgen.stderr_reviewed_warning`; rely on normalized `stderr_accepted`
for classification. Inspect
`data.report_output.capture`, `data.json_output.capture`,
`data.transcript.assessment` (including `stderr_policy`, counts, and
`stderr_accepted`), and `data.comparison.evidence_agrees` when the result is
`unknown`. Never infer a match or mismatch from stale, linked, missing,
malformed, conflicting, unrecognized-stderr, or truncated evidence.
The JSON parser decodes the exact Verilog escaped-identifier form
`\\body<ASCII-space>` with printable non-space ASCII body bytes, plus Netgen's
unterminated form only for a legal simple-identifier body. It rejects
malformed forms and canonical collisions, and requires ordered equality plus
the independently parsed native-report verdict. Do not add project aliases or
rewrite either netlist to turn a mismatch into a pass.

## Interpret the contract

Read `engineering.status` separately from `execution.status`:

- `execution.status: completed` means the native process ran; it does not mean DRC or LVS passed.
- `engineering.status: pass` means the driver interpreted the requested check as passing.
- `engineering.status: fail` is valid evidence of an engineering failure, such as violations, mismatch, or non-convergence.
- `engineering.status: unknown` means the evidence was insufficient or uninterpretable. Do not upgrade it to pass.

Use `diagnostics`, `inputs`, `artifacts`, and `provenance` before reading native logs. Open only the bounded log tail or a referenced artifact when the normalized result is insufficient. See [references/result-contract.md](references/result-contract.md) for field and exit-code details.

For an explicit shared simulation backend, also read `data.protocol`,
`data.analysis`, and `data.evidence`. Those fields have the same closed meaning
for ngspice and Xyce; backend-native details remain under the namespaced
extension and native artifacts. A simulation-evidence `pass` proves fresh,
finite evidence for the selected OP, DC, AC, or transient analysis, not that
any circuit specification was met.

## Report evidence

Return:

1. The engineering conclusion and its status.
2. The exact tool and version selected.
3. The PDK, model, rule deck, setup, and top-cell assumptions that materially affect the conclusion.
4. Diagnostics that explain failures or unknown results.
5. Artifact paths and hashes from the contract.
6. Any limitation that prevents signoff-level interpretation.

Do not describe a preview driver as foundry signoff. Preserve the distinction between tool execution, OpenADA normalization, and engineering review.
