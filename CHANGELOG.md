# Changelog

OpenADA follows semantic versioning for the Python package and agent plugins.
Contract, operation-profile, and conformance identifiers have independent
versions as described in the [compatibility policy](docs/COMPATIBILITY.md).

## 0.4.0 — Unreleased

### Changed

- **One simulation semantic.** `openada.operation/circuit.simulate/v1alpha2` is
  now the only simulation operation and `openada simulate` the only verb. Its
  target is a SPICE deck *or* a published Simra `schematic.artifact.json`,
  detected by reading the file; its model source is nothing, a flattened
  `--models` card file, or an installed PDK bound by `--pdk`; and an artifact
  declaring several analyses is split into one single-analysis deck per
  declaration and every one is run. Every simulation, from every target, with
  every model source, now returns exactly one `circuit.simulate/v1alpha2`
  envelope — a PDK-bound run previously returned a raw native ngspice payload
  with no `analysis` or `evidence` block at all.

  `openada.operation/testbench.simulate/v1alpha1` is **retired**: it was a
  second operation that meant "simulate", and two semantics for one engineering
  act is how a live request took the path that consulted no PDK binding.
  Its profile document is marked `deprecated` and is bound by no driver and no
  surface; the `org.openada.driver.simra.testbench.*` identities are removed, so
  there is one driver identity per backend. `openada testbench-simulate`
  survives as a deprecated alias that emits `simulation.operation.deprecated`
  and delegates — the result it returns is a `circuit.simulate` result.

  **The alias has a stated end.** It is removed in **0.6.0**, and not before
  **2026-11-01**. The date and version are stated in the retired profile's
  `extensions["org.openada"].deprecation`, in
  `openada.operations.testbench_simulate.REMOVAL_VERSION` /
  `REMOVAL_NOT_BEFORE`, and in the warning text every alias call returns; a
  test asserts the three agree. `openada.operations` re-exports only the alias
  entry point the CLI dispatches to — `TESTBENCH_SIMULATE_PROFILE` and
  `resolve_testbench_driver` are no longer a second spelling there, since every
  re-export of a retired name is a fresh way for it to acquire a caller.

  `profiles/circuit.simulate-v1alpha2.json` now declares 25 of the 26
  diagnostics the unified path could emit but had not published.
  `simulation.operation.deprecated` is deliberately not declared: it is a fact
  about the retired alias, not about `circuit.simulate`, and it disappears with
  the alias — declaring a code meant to disappear inside an identifier the
  compatibility policy treats as immutable is how a retired semantic becomes
  permanent.

  The binding facts moved from `data.extensions["org.openada.pdk_binding"]` to
  `data.extensions["org.openada.pdk-binding"]`: the underscore spelling is
  illegal under the `circuit.simulate/v1alpha2` extension-name pattern, which is
  itself evidence the two operations had drifted apart.

- **A netlist carries devices, not technologies.** A deck that binds PDK
  collateral by hand — any `pre_osdi`, or a `.lib`/`.include` reaching into an
  installed PDK's tree — is refused with `pdk.collateral.hand_bound`, even when
  its paths are correct, because such a deck can only ask its question in one
  technology. `pdk.collateral.foreign` names the case where one PDK's
  incantation was applied to another (the observed live failure: IHP's
  `psp103.osdi` preloaded at a sky130 path); `pdk.collateral.missing` the case
  where a deck would bind nothing; `pdk.collateral.conflict` a hand-written
  prelude handed to `--pdk`. `--unmanaged-collateral` is the one way past and
  stamps the result with a permanent provenance limitation.

- `nmos.svt`/`pmos.svt` are accepted as synonyms of `nmos.core`/`pmos.core`.

### Added

- One canonical netlist, four PDKs. A published Simra deck now names a
  technology-independent *device role* (`nmos.core`, `pmos.core`, and
  `.lvt`/`.hvt`/`.io` variants) and SI geometry, and the binding profile
  translates it — so the same bytes bind to `ihp-sg13g2`, `sky130A`,
  `gf180mcuD` and `freepdk45` with nothing but `--pdk` changing. A deck naming
  one PDK's own model is translated through the same role index, so every
  artifact published so far keeps working; each substitution is reported under
  `data.extensions["org.openada.pdk-binding"].model_translations`.

  `PdkBinding` now carries the four properties that previously had no
  representation and made every non-IHP PDK fail:

  - **`geometry_scale`** — the `.option scale` a PDK's own collateral installs.
    sky130A's `libs.tech/ngspice/all.spice:2` sets `scale=1.0u`, reached from
    every `corners/<corner>.spice`, so instance geometry must be a plain micron
    number. An SI-valued card is scaled a second time, lands outside every
    model bin, and ngspice reports only `could not find a valid modelname`.
    Geometry is now converted at bind time and the bound deck states its own
    convention.
  - **`library_entries`** — an ordered prelude of `.include` and
    `.lib <file> <section>` cards rather than one path. gf180mcuD's corner
    library evaluates global switches (`fnoicor`, `sw_stat_global`) defined
    only in `design.ngspice`, which must be included first; without it every
    model card fails to evaluate. Both a path and a section may carry
    `{corner}`, so a corner can be a library section or a directory —
    FreePDK45 has no library sections at all.
  - **`device_geometry`** — the binning envelope the simulator enforces, read
    out of the PDK's own model cards. A device outside it is refused with
    `pdk.device.geometry_out_of_range` naming the dimension and the legal
    range, instead of surfacing as a missing model.
  - **`analog` / `unsupported_reason`** — `asap7`, `nangate45` and `gt2n` ship
    LEF, Liberty and GDS and no transistor models for any simulator. They are
    registered so that requesting one fails with that reason rather than
    "unknown PDK", which an agent answers by trying another spelling.

  `sky130A`'s previous entry was declared from the published layout and never
  exercised; it was wrong on two counts (its FETs are subcircuits, not `.model`
  cards, and it needs the micron convention) and is corrected here.
  `--timeout` defaults to 600 s because parsing `sky130.lib.spice tt` alone
  takes ~95 s.

- Per-PDK binding profiles for `testbench-simulate`, selected with `--pdk`,
  `--pdk-root`, and `--corner`. A published Simra testbench is deliberately
  model-free, so every MOS artifact previously required a hand-flattened
  model-card file; a binding profile now supplies the collateral from an
  installed PDK instead. The profile owns the parts that genuinely differ
  between PDKs and fail silently when guessed: the device prefix (IHP SG13G2
  ships its MOS devices as subcircuits, so the emitted `M` card is rewritten to
  `X`), the parameter spelling (IHP's finger count is `ng`, while Simra emits
  `NF`, and an unmapped parameter is a hard ngspice error), the two-argument
  `.lib <file> <section>` corner entry point, and the Verilog-A modules ngspice
  must preload before a PSP103 device will bind. Every referenced PDK file is
  content-bound and reported under `data.configuration`; the binding itself is
  reported under `data.extensions["org.openada.pdk_binding"]`. `--pdk` and
  `--models` are mutually exclusive.

- An experimental `drc-compare` operation with explicit `revision` and `deck`
  modes. Revision mode requires different GDS content and reports persistent,
  resolved, and introduced bounded native markers. Deck mode requires the same
  GDS content and different generator scripts, then adds proximity-based
  cross-category correlations. Both modes recheck input stability and preserve
  explicit non-signoff limitations. Revision mode can additionally bind paired
  passing OpenADA LVS results that use the same reference netlist and setup,
  without claiming an unproven extraction-to-GDS relationship.

- A diagnostic `drc-review` CLI operation that consumes an existing validated
  KLayout LYRDB plus its exact GDS, deduplicates equivalent native cell
  variants, expands retained leaf-cell markers through the physical hierarchy,
  and emits hashed full-layout and ranked occurrence-level PNG views. Fresh
  output, input stability, bounded geometry, native PNG structure, dimensions,
  and renderer summaries are checked before the operation passes. The images
  remain representative diagnostic evidence, not a replacement for the native
  report, rule deck, or foundry signoff.
  Review results now also classify common rule families from native rule text,
  retain declared length constraints, measure marker bounds, and calculate
  coordinate-to-grid offsets for off-grid findings while explicitly avoiding
  automated-fix or reconstructed-rule claims.

- An experimental `bootstrap-asic-project` skill for blank open-ASIC
  workspaces. It defines core/full-chip/submission finish lines, selects one
  coherent PDK/flow/runtime stack, starts full chips from maintainer-owned
  padframe templates, and stage-gates RTL, function, synthesis, physical
  implementation, routed timing, DRC, LVS, and handoff. Its bounded standard
  library helper maintains a draft/frozen identity ledger for canonical
  project/collateral/tool paths and SHA-256 values, with deliverable-dependent
  requirements, explicit replacement/thaw, retained gap resolution, and
  machine-readable freeze-readiness/missing-requirement diagnostics. Pre-run
  assembly roles bind immutable generators such as `seal-ring.config`, not
  generated signoff outputs. Its
  successful freeze is structural/hash consistency only, not compatibility or
  engineering evidence. Missing OpenADA operations default to not evaluated;
  explicitly authorized native gap runs stay outside OpenADA result envelopes
  and foundry signoff is never inferred.

- `result.series.extract/v1alpha1` and the `extract` CLI bridge. It consumes a
  complete passing `circuit.simulate/v1alpha2` envelope plus that result's exact
  retained raw artifact, rechecks canonical path/bytes/SHA-256 and file
  stability, selects one request-bound padded plot within fixed bounds, and emits
  a canonical `result.measure/v1alpha1`-compatible real series. ngspice
  binary/ASCII and
  Xyce ASCII Spice3 evidence support explicit real/imaginary Cartesian voltage
  or current projections across each backend's advertised OP/DC/AC/TRAN rows.
- `result.spectral.measure/v1alpha1` and the `spectral` CLI operation for one
  closed coherent single-tone SNR, SINAD, signed-dB THD, or SFDR measurement.
  The method freezes uniform power-of-two sampling, rectangular window, mean
  removal, one-sided mean-square bin power, harmonic folding/collision rules,
  band membership, tie breaking, and a hashed component partition.
- `result.transfer.measure/v1alpha1` and the `transfer` CLI operation for an
  explicit same-unit Cartesian AC output-over-input trace plus one
  first-positive-frequency gain, unique falling −3 dB bandwidth,
  unity-gain-frequency, or negative-feedback phase-margin scalar. The profile
  freezes phase unwrapping and log-frequency crossing interpolation, rejects
  ambiguous crossings, and explicitly excludes gain margin.
- A standards-scope map for IEEE 1241-2023 (ADC), IEEE 1658-2023 (DAC), IEEE
  1057-2017 (waveform recorders), IEEE 2414-2020 (jitter/phase-noise
  terminology), and IEEE 181-2025 (transitions/pulses). Implemented spectral
  methods remain OpenADA definitions; converter/recorder references are
  explicitly `candidate`, not IEEE conformance claims.
- An explicit external-provider runtime over immutable
  `openada.driver-manifest/v0alpha1` and `openada.request/v0alpha1`, exposed as
  `provider validate`, `provider list`, and `provider invoke`. It validates
  manifests and cross-references, resolves one active circuit-simulation local
  JSON-stdio wait capability, invokes without a shell under bounded I/O and
  timeout policy, and validates typed result data, provider/request correlation,
  truth-table execution status, requested artifact roles and limits, local file
  hashes, zero transport exit, and wait-process cleanup. Before launch it
  snapshots canonical regular target and configuration files, verifies declared
  SHA-256 identities, and enforces 16 MiB target, 256 MiB-per-configuration,
  and 512 MiB aggregate bounds; post-run mutation or replacement invalidates
  the evidence. It does not discover, install, rank, or trust providers;
  v0alpha1 does not digest-bind the complete request.
- A hash-bound ngspice PDK-control reference provider with a closed ordered
  `save all` → OP/DC/AC/TRAN → optional TRAN-only `linearize` → safe `write`
  grammar, exact analysis-feature matching, and sanitized native execution.
  Its pinned public-IHP chain retains real ngspice 46 evidence for all four
  analyses, independently reconstructs engineering facts and scoped agent
  decisions, and exercises request, native-error, and tamper boundaries. Its
  provider conformance claim is bound to the exact source-frozen chain receipt.
- Three complete digital semantic operations and their engineering workflows:
  strict `rtl.lint/v1alpha1` through Verilator, flattened Liberty-mapped
  `logic.synthesize/v1alpha1` through Yosys/ABC, and constraint-complete
  one-corner `timing.analyze/v1alpha1` through OpenSTA. Results bind ordered
  RTL, conservative literal-include closure, stable input hashes, mapping
  policy, a version- and digest-bound external ABC executable, closed
  non-inheriting tool environments, fresh native artifacts, normalized
  inference/cell/area evidence, and
  setup/hold WNS/TNS in seconds without claiming equivalence, physical timing,
  or signoff. The plugin adds senior RTL-architecture, synthesis/inference, and
  ASIC-timing skills that stay inside those evidence boundaries.
- A closed semantic-surface catalog and non-waivable release ledger covering
  all 147 active rows through seven pinned public-design chains. Each accepted
  row now carries contract tests, a real native EDA run, independent artifact
  verification, normalized evidence, a downstream engineering decision,
  negative and tamper replays, agent-visible evidence, and clean-source
  attestation. CI mechanically checks manifest hashes, the seven-record index,
  provider receipt registration, offline verifiers, and zero release gaps.
- Public real-design chains for IHP DRC/LVS, IHP analog measurement and full
  agent workflows, IHP SAR RTL/lint, ORFS Ibex Nangate45 synthesis/timing, and
  ngspice/Xyce analysis portability. Native
  artifacts and public-design provenance are retained in the source
  distribution so downstream reviewers can rerun every offline oracle.
- `profile list` and `profile show` for cwd-independent inspection of all
  packaged operation, assertion, feature, parameter, and normalized-result
  schemas.
- Intent-ledger and implemented-routing references for the analog
  characterization coordinator, plus standards-aware spectral workflow
  guidance and concrete extraction/measurement/evaluation/provider commands in
  the execution skill.

### Changed

- Updated the ASIC bootstrap coordinator to use the shipped
  `rtl.test/v1alpha1` operation for compatible self-checking HDL tests while
  preserving its assertion-adequacy boundary and explicitly recording the
  current lack of a scoped RTL-test doctor mapping.
- Promoted `jsonschema>=4.18` to a base dependency because operation-specific
  validation is part of the external-provider execution boundary.
- Extended `evaluate` to accept complete ordinary or spectral measurement
  envelopes, plus the new transfer measurement envelope, while preserving the unchanged
  `specification.evaluate/v1alpha1` typed measurement input.
- Let `measure`, `spectral`, and `transfer` consume a complete passing
  `result.series.extract` envelope directly, removing an undocumented manual
  JSON handoff.
- Advanced the Python package, Codex plugin, Claude plugin, and built-in driver
  identity to 0.4.0; packaged wheels now include every current analog,
  measurement, digital, and specification operation profile.
- Made the source/plugin launcher import optional schema validation lazily, so
  dependency-free discovery commands still run and schema-backed commands emit
  a structured missing-dependency diagnostic. Plugin setup now states clearly
  that agent marketplaces install skills but not the Python runtime dependency.

### Fixed

- **`result.series.extract` accepts the envelope `circuit.simulate` writes.**
  `simulate` records every file that bound the devices under
  `data.extensions["org.openada"].configuration`; `extract` refused that field
  as undeclared, so the only way to chain the two operations was to delete the
  record naming where the devices came from. A round trip broken inside one
  contract, paid for in provenance. The field is now declared *and* validated -
  role, path, digest and byte count - so accepting it opens no hole. The
  regression test round-trips a real `simulate` envelope with nothing stripped.

- **`openada simulate` consults `PDK_ROOT`.** `--pdk` refused with
  `pdk.root.required` even when the environment variable already named a valid
  root that `openada doctor` reports. `--pdk-root` still wins when given; the
  refusal now names both ways the root can be supplied.

- **A named analysis takes its numbers from the deck.** `--analysis tran`
  demanded `--step-s` and `--stop-s` from a deck that already states
  `.TRAN 10p 8n`. Since the request is checked against the deck's own directive
  anyway, those were the only values that could ever run. Any required field
  the caller omits is now taken from the deck's single top-level directive of
  the named type; an explicit flag always wins, and a deck stating no such
  directive is still refused, saying so.

- **`series.selector.missing` names what was asked for and what is available.**
  One sentence covered both a misspelled vector and naming the sweep axis
  alongside the signals, and gave a caller nothing to act on. The refusal now
  names the offending selector, which of the two mistakes it is, the plot's
  selectable signals and its axis vector.

- **`pdk.collateral.foreign` is reachable for `pre_osdi` cards.** The
  `pre_osdi` branch returned before the foreign check, so the module
  docstring's own headline example - IHP's `psp103.osdi` preloaded from the
  sky130A tree - classified as `pdk.collateral.hand_bound`. Both refuse before
  ngspice runs, so nothing unsafe ran; the diagnostic simply named the wrong
  mistake, and the two have different corrections. Foreign is now decided
  first, for every card kind; a preload naming its own tree's module is still
  `hand_bound`.

- Accepted Netgen 1.5.321 hierarchical JSON's exact, equivalent pin-only
  auxiliary records without obscuring the unique requested top-cell LVS
  comparison. Unequal pin lists, partial known-key records, duplicate requested
  tops, and other ambiguous shapes remain invalid and produce engineering
  `unknown`.

### Limitations

- Spectral v1alpha1 intentionally rejects nonuniform or noncoherent records,
  non-rectangular windows, main-lobe integration, PSD/averaging, SNDR aliases,
  ENOB derivation, jitter, and phase noise. True zero-frequency DC gain,
  gain margin, phase-crossing search, poles/zeros, integrated noise, corners,
  and statistical campaigns remain future semantic operations.
- External provider execution is explicit-manifest, local CLI, JSON
  stdin/stdout, wait-only, and currently registered only for active
  `circuit.simulate/v1alpha2`. v0alpha1 still has no complete request digest,
  independent capability ID, per-feature maturity rows, normative MCP binding,
  catalog trust model, sessions, remote jobs, or artifact-transfer protocol.

## 0.3.0 — 2026-07-15

### Added

- Backend-independent `result.measure/v1alpha1` and
  `specification.evaluate/v1alpha1` operations, exposed as `measure` and
  `evaluate`. They operate on canonical-digest-bound normalized real inline
  series and typed measurement records; supported scalar algorithms, exact
  units, limits, and condition bindings are closed and explicit. A public
  `openada.operations.normalized_series_sha256(...)` helper computes the input
  digest, while results retain measurement-request and complete-specification
  digests and normalized evidence. These bindings detect changed content but
  are not signatures or authentication.
- A deterministic, network-free typed-evidence conformance bundle covering all
  nine measurement algorithms plus specification pass, fail, unknown, and
  tampered-binding cases.
- The additive immutable `openada.operation-profile/v0alpha2` schema for
  deterministic semantic implementations, plus the active
  `circuit.simulate/v1alpha2` profile with explicit OP/DC/AC/TRAN mappings. The
  published v0alpha1 schema and historical `circuit.simulate/v1alpha1` profile
  remain unchanged.
- Experimental `characterize-analog-block`, `analyze-feedback-stability`,
  `analyze-spectral-linearity`, and `assess-pvt-and-yield` engineering skills.
  The skills inspect installed capabilities and leave unsupported metrics not
  evaluated; fresh-agent forward tests do not promote them beyond experimental.
- A provider, marketplace, connector-mining, and MCP boundary proposal. MCP is
  described as a future transport adapter for unchanged OpenADA
  requests/results, and a future marketplace catalogs conforming capability
  providers rather than raw executables.
- Namespaced plugin skill entry points for Codex (`$openada:<skill>`) and Claude
  Code (`/openada:<skill>`), while retaining skill-only installation through
  the standard `~/.agents/skills` user directory.

### Changed

- Moved the active shared simulation bridge from historical immutable
  `circuit.simulate/v1alpha1` to additive `circuit.simulate/v1alpha2`, expanding
  the typed CLI flags to OP, DC, AC, and transient analyses. The ngspice mapping
  is structured for OP/DC/AC and workflow-validated for transient; Xyce is
  structured for DC/AC, workflow-validated for transient, and explicitly rejects
  OP as unsupported.
- Extended the circuit-simulation portability fixtures and independent native
  evidence checks by analysis in the new v0alpha2 conformance bundle. Its
  pinned success replay now binds the exact active operation-profile digest and
  supports structured maturity for the new analysis rows; the historical
  v0alpha1 transient bundle remains byte-stable. The shared
  subset still rejects includes, control blocks, native measurements, print
  directives, FFT, noise, Monte Carlo, and multiple analyses.
- Bounded each top-level simulation deck and explicit ngspice init input to
  16 MiB before native launch or over-limit hashing. Conflicting generic native
  errors now prevent a terminal non-convergence observation from becoming an
  engineering `fail`.

### Limitations

- `result.measure` consumes caller-supplied normalized inline series; OpenADA
  does not yet extract those series from native ngspice or Xyce waveform files.
  Optional upstream native-artifact lineage is recorded only as unverified.
- Runtime external-manifest discovery, generic request dispatch, and MCP
  provider invocation remain unimplemented.
- `openada.driver-manifest/v0alpha1` has no normative MCP transport binding,
  independent capability IDs, or per-feature maturity/conformance rows; those
  require a future additive manifest revision rather than overloaded v0alpha1
  values.

## 0.2.0 — 2026-07-15

### Added

- A tool-independent `review-circuit-simulation` engineering skill above the
  OpenADA execution-and-evidence adapter.
- A shared typed `circuit.simulate/v1alpha1` profile for ngspice and Xyce.
- A pinned, network-disabled native ngspice/Xyce portability replay with an
  independent verifier for both native waveform formats.

### Changed

- Promoted the bounded Xyce transient mapping to workflow-validated shared
  alpha maturity alongside ngspice.
- Expanded plugin metadata so Codex and Claude Code expose both the execution
  skill and backend-independent engineering workflow.
- Kept the emitted result envelope at `openada.result/v0alpha1`; execution and
  engineering status semantics are unchanged.

## 0.1.0 — 2026-07-14

- Initial public preview of the semantic CLI, six open-source EDA drivers,
  normalized evidence contract, plugin packaging, schemas, and conformance
  workflows.
