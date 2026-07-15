# Changelog

OpenADA follows semantic versioning for the Python package and agent plugins.
Contract, operation-profile, and conformance identifiers have independent
versions as described in the [compatibility policy](docs/COMPATIBILITY.md).

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
