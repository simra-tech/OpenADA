# Changelog

OpenADA follows semantic versioning for the Python package and agent plugins.
Contract, operation-profile, and conformance identifiers have independent
versions as described in the [compatibility policy](docs/COMPATIBILITY.md).

## 0.3.0 — Unreleased

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
