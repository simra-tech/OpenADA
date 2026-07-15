# Contributing to OpenADA

OpenADA grows one verifiable agent–EDA contract at a time. A useful contribution
does more than make a binary run: it defines what the operation means, separates
process status from engineering status, and proves the behavior with bounded
evidence.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

Run the native smoke only when the relevant binary is installed:

```bash
openada doctor --tool ngspice --require ngspice
openada simulate fixtures/smoke/smoke_ngspice.cir \
  --output-dir /tmp/openada-smoke
```

The first cross-driver profile is
`openada.operation/circuit.simulate/v1alpha2`. Its shared ngspice/Xyce alpha
subset is one self-contained OP, DC, AC, or transient analysis with no
includes, measurements, print directives, control-language blocks, FFT, noise,
Monte Carlo, or multiple analyses. ngspice implements OP/DC/AC/TRAN; Xyce
implements DC/AC/TRAN and rejects OP explicitly. Exercise either backend with
the same operation and analysis-specific typed flags:

```bash
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend ngspice --output-dir /tmp/ngspice-run
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend xyce --output-dir /tmp/xyce-run
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/resistor-divider-dc.cir \
  --backend ngspice --analysis dc \
  --source-name VSWEEP --source-unit V --start 0 --stop 1 --step 0.25 \
  --output-dir /tmp/ngspice-dc-run
```

Omitting `--backend` preserves the legacy ngspice interface and default. The
pinned native portability replay exercises the advertised analysis matrix and
independently checks native evidence:

```bash
python3 conformance/circuit-simulate-v0alpha2/run.py \
  --evidence-dir /tmp/openada-circuit-simulate-evidence
```

Keep each advertised analysis capability labeled alpha. The expanded pinned
success replay supports structured OP/DC/AC rows; only the previously complete
transient workflow retains workflow-validated maturity. Neither label covers
every circuit-analysis feature or runtime.

The deterministic evidence operations are intentionally separate from native
simulation:

```bash
openada measure --series normalized-series.json \
  --measurement measurement-request.json
openada evaluate --measurement measurement-result.json \
  --specification specification.json
```

`measure` accepts only a bounded normalized real inline series whose canonical
content digest matches its declared `measurement.source` identity. It does not
parse native waveform files. An optional link to an upstream native artifact
is explicitly `unverified`. `evaluate` performs no implicit unit conversion:
measurement units, bound units, and declared conditions must match exactly.
Contributors must keep the supported scalar algorithm vocabulary and limit
shape closed rather than adding a backend expression escape hatch.

When testing ngspice control decks, use a fresh writable work directory and
declare native outputs rather than searching for whatever changed:

```bash
openada simulate /tmp/task/inverter_tb.spice \
  --execution-mode control \
  --init-file /path/to/pdk/.spiceinit \
  --system-init-file /path/to/ngspice/scripts/spinit \
  --workdir /tmp/task \
  --expect-output raw=test_inverter.raw \
  --output-dir /tmp/task-evidence
```

For KLayout, always choose one exact fresh LYRDB path. Record any PDK files
that the executable Ruby deck loads but that OpenADA cannot infer:

```bash
mkdir -p /tmp/openada-drc-evidence
openada drc /tmp/task/layout.gds \
  --rules /path/to/pdk/rules.drc \
  --workdir /tmp/task \
  --top-cell TOP \
  --provenance-input /path/to/pdk/COMMIT \
  --report /tmp/openada-drc-evidence/layout.lyrdb
```

The report and sibling `.openada.log` transcript must both be absent before
launch. Use `--expect-report relative/path.lyrdb` only when the reviewed deck
owns that exact relative path under `--workdir`. KLayout implicitly reads
`<report>.w`; tests must either prove it is absent or explicitly declare and
hash that exact sidecar with `--waiver-file`.

For Netgen, choose one exact fresh final report path with a filename suffix and
declare known executable-setup dependencies:

```bash
mkdir -p /tmp/openada-lvs-evidence
openada lvs /tmp/task/layout.spice /tmp/task/schematic.spice \
  --cell TOP \
  --setup /path/to/pdk/setup.tcl \
  --provenance-input /path/to/pdk/COMMIT \
  --report /tmp/openada-lvs-evidence/top.lvs.comp
```

The report, derived `top.lvs.json`, and sibling
`top.lvs.comp.openada.log` must all be absent before launch. Treat setup Tcl as
caller-supplied executable code, run it from an appropriate task-local current
working directory, and do not claim that the connector sandboxes its side
effects or recursively discovers every sourced/ambient dependency.

## Proposing a driver

Open an issue or focused pull request with:

1. The semantic operation an agent needs, not merely the EDA executable name.
2. A native command and input/output description from primary tool documentation.
3. At least one small, redistributable fixture with pinned source and license.
4. Expected process and engineering outcomes, including one failure case.
5. A bounded result mapping into `openada.result/v0alpha1`.

For the target external-driver protocol, also read the
[semantic model](docs/SEMANTIC_MODEL.md) and
[request/driver protocol](docs/DRIVER_PROTOCOL.md). Start from the
[request template](conformance/driver-kit/request.template.json),
[driver-manifest template](conformance/driver-kit/driver-manifest.template.json),
and
[operation-profile RFC template](conformance/driver-kit/operation-profile.template.md).
The request and manifest schemas are reviewable protocol scaffolding; the
current runtime does not discover a manifest or invoke a JSON-stdio driver.
MCP and remote jobs may become transports for the same semantic request/result,
not a second operation ontology. The current
`openada.driver-manifest/v0alpha1` schema defines no normative MCP binding and
cannot express per-feature capability identity or maturity; do not overload its
generic transport strings to imply those contracts. A future additive manifest
revision and marketplace may catalog conforming capability providers rather
than raw binaries; see
[Providers, marketplaces, and MCP](docs/PROVIDERS_AND_MCP.md).

An operation-profile proposal must define more than a command name. Include:

1. purpose, target types, and explicit non-goals;
2. one versioned primary assertion and its `pass`/`fail`/`unknown` truth table;
3. a closed operation-parameter schema layered on the base request envelope;
4. normalized facts, bounds, diagnostic categories, and required artifact roles;
5. evidence freshness, integrity, and incomplete-provenance rules;
6. success, engineering-failure, invalid, unavailable, timeout, and malformed
   evidence fixtures;
7. at least two plausible native mappings before proposing the profile as a
   shared core operation, or a clear reason the operation is a deterministic
   backend-independent semantic kernel.

Use
[`openada.operation-profile/v0alpha1`](schemas/operation-profile-v0alpha1.schema.json)
for the immutable circuit-simulation profile shape. New deterministic semantic
profiles may use the additive
[`openada.operation-profile/v0alpha2`](schemas/operation-profile-v0alpha2.schema.json),
which permits feature-to-algorithm bindings without pretending they are SPICE
analysis commands. Never revise v0alpha1 in place.

The driver must:

- execute an argv vector without `shell=True`;
- validate paths and fragile identifiers before launch;
- bound every input before scanning or hashing it;
- use a finite timeout;
- distinguish invocation failure from an engineering fail;
- bound stdout, stderr, samples, and violation lists;
- retain native reports as hashed artifacts;
- avoid changing PDKs or source designs implicitly;
- work from native `PATH` even when a container profile also exists;
- avoid importing a harness such as Codex, Claude Code, or Simra into core code.

## Maturity labels

- **Discovered** requires a deterministic binary resolver and bounded version probe.
- **Structured** requires a semantic operation, contract output, and unit tests.
- **Workflow-validated** additionally requires a pinned public design, PDK/runtime
  identity, expected assertions, and a real conformance run.

Do not promote a driver based only on `--help`, a zero exit code, or a mocked unit
test.

## Contributing engineering skills

Engineering skills live above the semantic contract. Use them for reusable
review, diagnosis, planning, or decision workflows that can consume OpenADA
operations without teaching the agent a native EDA command surface. Read
[Engineering skills above OpenADA](docs/ENGINEERING_SKILLS.md) before proposing
one.

The current experimental examples include general analog characterization,
feedback stability, spectral linearity, and PVT/yield assessment. They are
capability-gated: a skill may use application and topology judgment to choose a
workflow, but it must inspect exact operation/feature support and mark missing
metrics not evaluated. Fresh-agent forward tests exercise routing discipline;
they do not establish native conformance, engineering signoff, or stable skill
maturity.

Put each skill in:

```text
skills/<lowercase-hyphen-name>/
├── SKILL.md
└── agents/openai.yaml
```

Add `references/` only for material loaded on demand and `scripts/` only for a
deterministic reusable helper. Do not add a README or copy driver behavior into
the skill.

A contribution should:

1. name a concrete engineering decision and its trigger cases;
2. use OpenADA operation/assertion semantics rather than raw tool commands;
3. make execution, evidence validity, measurement, specification, and signoff
   boundaries explicit where relevant;
4. route `pass`, `fail`, `unknown`, invalid requests, and unavailable backends;
5. preserve explicit PDK, model, corner, deck, setup, and top-cell choices;
6. include UI metadata whose default prompt explicitly names the skill;
7. forward-test at least one realistic success and one failure or uncertainty
   case.

When two conforming drivers implement the operation, the skill instructions
must remain unchanged across them. Backend selection may be an explicit
parameter; backend-specific flags, parsing rules, and safety policy belong in
the driver or the `openada` execution skill.

Run the repository checks before opening a pull request:

```bash
pytest -q tests/test_plugin_skills.py
pytest -q
```

Skill maturity is separate from **Discovered**, **Structured**, and
**Workflow-validated** driver maturity. A skill cannot change contract meaning
or substitute for native conformance evidence.

## Mutation contributions

Write-capable operations use the separately gated
[mutation and design-versioning model](docs/MUTATION_AND_VERSIONING.md). A
mutation proposal must preview by default, bind an exact base revision and plan
digest, require explicit scoped apply authorization, record transaction
disposition separately from engineering status, prove fresh postconditions,
and retain an append-only receipt. Tests operate on disposable copies and must
cover stale-base, partial-write, failed-postcondition, and failed-rollback
states before a mutation profile can be structured.

## Tests

Use fake executables to test argv construction and result normalization without
requiring a large EDA installation. Add a separate marked conformance test for
real tool/PDK behavior. Tests should assert relationships such as:

- process completed while DRC engineering status failed;
- a missing report produces `unknown`, never `pass`;
- artifact hashes match the files written by the driver;
- logs and normalized lists remain bounded.
- a pre-existing, missing, symlinked, constants-only, or corrupt deck-owned
  output never produces simulation `pass`;
- streaming batch mode never claims that suppressed `.measure` values were
  evaluated;
- undeclared native side effects never become OpenADA artifacts;
- replaced output parents, symbolic links, and hard-linked stale files never
  become deck-owned evidence;
- a minimal XML lookalike, wrong LYRDB generator/top cell, or empty category
  catalog never produces DRC `pass`;
- KLayout marker totals honor native item multiplicity, normalized examples are
  globally bounded, and a malformed stable regular report remains hashed
  evidence while its engineering result stays `unknown`;
- nested KLayout categories retain full paths across repeated leaf names, while
  declarations under non-native containers are rejected;
- cell variants and the native empty global/dummy cell are accepted without
  allowing an empty report top cell;
- an ambient or newly appearing KLayout `<report>.w` sidecar prevents a
  trustworthy DRC result unless it was explicitly declared and remained stable;
- an explicit waiver's held inode identity is checked in addition to its hash,
  and KLayout input paths remain unique;
- a Netgen setup error followed by zero exit and match-shaped native outputs
  remains `unknown`, because the bounded transcript is not clean;
- exact reviewed `Unable to permute model <token> pins <token>, <token>.`
  stderr lines remain warning diagnostics without weakening the rejection of
  every other stderr line;
- Netgen `pass` and `fail` require valid fresh report/JSON outcomes that agree,
  while missing, stale, linked, malformed, conflicting, or truncated evidence
  remains `unknown`;
- Netgen declared provenance inputs remain hashed and inode-stable through the
  run, and its incomplete transitive/ambient provenance warning is preserved;
- solver warnings that ngspice demonstrably recovers from do not become a false
  engineering failure;
- ngspice and Xyce fixtures apply the same profile assertion and normalized
  fact names to every mutually supported analysis, while retaining different
  native commands and artifacts;
- ngspice OP/DC/AC and Xyce DC/AC structured capability claims have matching
  pinned native success cases, transient retains its complete
  workflow-validated replay, and Xyce OP is rejected as unsupported;
- includes, measurements, print directives, control-language blocks, FFT,
  noise, Monte Carlo, and mixed analyses are rejected rather than silently
  widened into the common simulation alpha;
- normalized-series digest mismatches, undeclared measurement kinds, unit
  mismatches, absent events, and condition mismatches preserve the documented
  `pass`/`fail`/`unknown` boundaries for typed evidence operations;
- scoped preflight probes exactly one assertion-mapped binary, never enumerates
  project or PDK catalog entries, and never reports that the design assertion
  itself was evaluated;
- empty, malformed, truncated, invalid-UTF-8, or identity-changing version
  output cannot make a selected tool ready.

The first real-design example is the pinned
[IHP inverter DRC/LVS workflow](conformance/ihp-inverter/README.md). Its static
manifest and verifier tests run without Docker or a PDK. The real replay is an
explicit conformance action because it depends on a large external runtime and
design prepared by the separate setup step; never add its generated evidence to
the repository.

The pinned
[IHP Xschem/ngspice workflow](conformance/ihp-inverter-ngspice/README.md) adds a
real schematic-to-waveform chain with an independently parsed native raw
artifact. Its system `spinit`, PDK init, design, PDK, tool versions, and frozen
container image are bound by the manifest/verifier pair.

Use the [driver conformance kit](conformance/driver-kit/README.md) to validate
captured results, expected status pairs, diagnostic codes, artifact roles, and
recorded file hashes. Contract changes must also follow the
[compatibility policy](docs/COMPATIBILITY.md). Passing this kit supports the
**structured** maturity gate; it does not replace a pinned real workflow for
**workflow-validated** maturity.

Agent comparison changes belong in
[`evaluation/paired-agent`](evaluation/paired-agent/README.md), not in the core
driver package. Keep scoring condition-blind, use byte-identical task prompts,
preassign every raw/OpenADA pair, launch every assignment exactly once, and
retain failures, timeouts, adapter rejection, unknown evidence, and treatment
non-adoption as intention-to-treat outcomes. Freeze the exact harness and
adapter binaries, canonical task files, runtime tool binaries, and a treatment
manifest containing each participant-visible file's role, path, size, hash,
mode, and media type. The raw condition may receive the neutral evaluation task
and submission schema, but none of the treatment distribution, CLI, package,
result schema, skill, repository, prior output, or injected context.

Supervisor records must bind the capture files, attempt count, planned dispatch
sequence, a campaign-local monotonic clock domain, both non-overlapping trial
intervals in the pair, isolation attestations, and the exact treatment-manifest
observation. Assemble and Ed25519-seal every planned row, including rejected
adapter traces; never select a rerun because an outcome is inconvenient. Plans
must declare `hmac-sha256-fisher-yates-v1`. Summaries must carry deterministic
plan-ordered commitments to every supplied plan-bound signed row and their own
campaign-key Ed25519 seal. Withhold the seed and mark the schedule unverified
whenever accounting is partial, even if the supplied seed reproduces the plan.
A campaign clock domain must be a fresh random nonce, and the supervisor must
rebase its first dispatch to zero; never publish a host boot/machine identity or
an absolute host-monotonic value. Treat artifact hashes, relative timing/usage,
and pair membership as residual sanitized-row fingerprints.
A reduced trace must discard prompts, reasoning, commands, paths, tool
arguments/results, search text, native identifiers, and errors; regex redaction
is not a safe substitute. Add adversarial tests for missing/selectively omitted
trials, malformed native evidence, filesystem aliasing, telemetry lifecycle
conflicts, signature or semantic forgery, schedule conflicts, and unsupported
metric claims. Historical restricted traces may select metrics or fixtures but
may never populate a public trial row. Keep artifact semantics separate from
process causality: only a trusted executor audit may support a claim that named
native processes generated the scored bytes.

Treat summary-only verification as publisher authentication, not independent
claim verification. A reproducible public comparison must include the exact
campaign bundle, plan, signed summary, and every sealed sanitized row committed
by that summary so another party can run full semantic recomputation. Sanitized
rows still expose opaque pair and condition linkability; assess that tradeoff
before the campaign. Artifact hashes, relative timing, and usage totals remain
additional row fingerprints. Never publish restricted raw streams or
supervisor records.

## Skills and harness adapters

Keep the shared routing workflow in `skills/openada`. Put deterministic behavior
in the Python package. Harness adapters should locate the CLI and translate
installation conventions; they should not fork driver semantics.

Validate changed skills and plugin manifests before opening a pull request.

## Licensing

Contributions are accepted under the repository's MIT license. Do not add PDK
files, foundry collateral, proprietary designs, or third-party examples without
clear redistribution permission and attribution.
