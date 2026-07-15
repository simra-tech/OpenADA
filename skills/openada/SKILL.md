---
name: openada
description: Discover, invoke, and interpret open-source EDA tools through OpenADA's versioned CLI contract. Use for semiconductor or electronics work involving Xschem schematics (.sch), SPICE netlists and ngspice or Xyce simulation, GDS/KLayout DRC, Netgen LVS, Verilog/SystemVerilog RTL and Yosys, PDK discovery, or diagnosing an open EDA environment in Codex, Claude Code, or another terminal-capable agent.
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

When this skill is loaded from a plugin checkout or cache, resolve the plugin root from this `SKILL.md` path and use `<plugin-root>/bin/openada` if the command is not on `PATH`. Do not guess a different installation path.

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
# Xschem schematic -> SPICE; pass the project/PDK rcfile when library resolution depends on it
openada netlist design.sch --rcfile path/to/xschemrc --output evidence/design.spice

# streaming ngspice simulation without .measure or .control
openada simulate testbench.spice --workdir path/to/project \
  --output-dir evidence/simulation

# typed same-intent transient profile through either reviewed built-in mapping
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend ngspice --output-dir evidence/shared-ngspice
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend xyce --output-dir evidence/shared-xyce

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
```

Never substitute a generic DRC deck, LVS setup, PDK, model library, or top cell merely to obtain a passing result. Ask for the missing project-specific input.

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
extension and native artifacts. A simulation-evidence `pass` proves a fresh,
finite transient result, not that any circuit specification was met.

## Report evidence

Return:

1. The engineering conclusion and its status.
2. The exact tool and version selected.
3. The PDK, model, rule deck, setup, and top-cell assumptions that materially affect the conclusion.
4. Diagnostics that explain failures or unknown results.
5. Artifact paths and hashes from the contract.
6. Any limitation that prevents signoff-level interpretation.

Do not describe a preview driver as foundry signoff. Preserve the distinction between tool execution, OpenADA normalization, and engineering review.
