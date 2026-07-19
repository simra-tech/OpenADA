# Provider ecosystem profile contracts

OpenADA ships four additive `v1alpha1` operation-profile contracts for provider
ecosystem experimentation:

- `openada.operation/digital.hdl.simulate/v1alpha1` defines an ordered,
  dependency-aware HDL preparation, compilation, elaboration, execution, and
  artifact-collection workflow. A zero exit status without the declared
  self-check evidence remains inconclusive.
- `openada.operation/network.parameters.extract/v1alpha1` defines read-only
  extraction of selected two-port S-parameter series from a strict bounded
  ASCII interchange artifact. It normalizes frequency to hertz and complex
  values to Cartesian form while retaining port direction, impedance, source
  encoding, source digest, normalized digest, and independent-parser facts.
- `openada.operation/electromagnetic.analyze/v1alpha1` binds vendor-neutral
  geometry, material, port, boundary, mesh, sweep, solver, and convergence
  identities. Planar and three-dimensional models, field and network outputs,
  convergence, and reference comparison are separately negotiable features.
- `openada.operation/artifact.transform/v1alpha1` records exact compilation or
  format-conversion lineage into a fresh contained output. Engineering and
  equivalence conclusions are always `not-evaluated`.

These profiles use the existing additive
`openada.operation-profile/v0alpha2` schema. Existing schemas and profiles are
unchanged. Their embedded request and normalized-result schemas are closed and
Draft 2020-12 valid.

## Availability and trust

The semantic catalog marks all four profiles `experimental-hidden` and
non-dispatchable. Their `org.example.*` mappings are deterministic public test
fixtures, not installed provider claims or evidence of current native
availability. A host must not route work to these profiles until an explicit
trusted provider mapping, capability declaration, validator, and required
conformance evidence have been installed and negotiated.

Across all four contracts, dependency readiness, execution state, artifact
readiness, engineering conclusion, workflow review, and signoff approval stay
independent. Process completion and artifact readability never imply an
engineering pass, review, or signoff.
