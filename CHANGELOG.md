# Changelog

OpenADA follows semantic versioning for the Python package and agent plugins.
Contract, operation-profile, and conformance identifiers have independent
versions as described in the [compatibility policy](docs/COMPATIBILITY.md).

## 0.4.0 — Unreleased

### Added

- Four additive vendor-neutral provider-ecosystem contracts: bounded multi-step
  digital HDL build/simulation, read-only two-port network-parameter series
  extraction, planar/three-dimensional electromagnetic analysis, and generic
  artifact compilation/conversion. They keep readiness, execution, artifact,
  engineering, review, and signoff dimensions independent and ship as
  `experimental-hidden`, non-dispatchable profiles with deterministic
  `org.example.*` conformance mappings only.

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
