# The Agent–EDA Contract

OpenADA defines a narrow control and evidence boundary between an agent harness
and native EDA tools. It does not replace native formats or require every EDA to
share one internal data model.

This document is the source of truth for the shipped `0.2.0` CLI and
`openada.result/v0alpha1` behavior. The broader [semantic model](SEMANTIC_MODEL.md)
and [request/driver protocol](DRIVER_PROTOCOL.md) distinguish implemented
behavior from review-only protocol scaffolding.

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
    "openada_version": "0.2.0",
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
`openada.operation/circuit.simulate/v1alpha1` profile. That common alpha path
accepts exactly one parseable, self-contained top-level `.tran`, rejects
includes, measurements, print cards, control blocks, and multiple analyses,
and emits the profile's closed `data.protocol`, `data.analysis`, and
`data.evidence` facts. Both mappings use the same assertion truth table and
canonical artifact roles while retaining different native commands and files.

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
script. OpenADA hashes it as a configuration input and explicitly sources it
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
uniquely declared top-level `.measure`. Terminal convergence evidence has
`fail` precedence. Intermediate singular-matrix or stepping warnings do not
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
Engineering `pass` requires zero native exit, a complete single real transient
plot with finite dependent values, a fresh log, stable input identity, and
complete bounded process capture. Only reviewed terminal transient
non-convergence text with native exit 1 can produce engineering `fail`; parse
errors, missing or malformed raw data, timeouts, and generic nonzero exits are
`unknown`. The mapping is workflow-validated for the bounded shared alpha
fixture through the pinned
[native ngspice/Xyce replay](../conformance/circuit-simulate/README.md); that
does not widen the alpha profile or imply support for arbitrary Xyce decks.

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
