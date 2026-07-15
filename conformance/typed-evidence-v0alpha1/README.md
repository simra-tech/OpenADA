# Typed-evidence kernel conformance

This model-free v0alpha1 bundle exercises the deterministic Python evidence
kernel without invoking an EDA tool or using the network. Its immutable
conformance identity is:

`typed-evidence-measurement-specification-v0alpha1`

That one record binds the exact v1alpha1 operation, assertion, and feature IDs
for `result.measure` and `specification.evaluate` to
`org.openada.kernel.typed-evidence` implementation version `1.0.0`.

## Coverage

The request fixture covers every closed measurement algorithm:

- sample-at, minimum, maximum, mean, and RMS;
- directed crossing, rise time, and fall time;
- settling time with an explicit tolerance and hold duration.

It also retains a valid not-found crossing to prove the distinction between a
measurement failure and unknown evidence. Specification cases cover an
inclusive pass, an upper-limit failure, exclusive-boundary failure,
not-found-to-unknown propagation, exact-unit rejection, exact-condition
rejection, and rejection of a condition value changed without updating its
source binding digest.

The fixture is hash-bound by `manifest.json`. The runner constructs exact
normalized-series digests through the public kernel helper and writes a new
evidence file. The verifier does not import or invoke the typed-evidence
implementation. It independently validates:

1. manifest, fixture, result-schema, and operation-profile hashes;
2. exact operation, assertion, feature, implementation, and algorithm IDs;
3. every public result envelope and operation-specific data schema;
4. canonical request, series, condition, measurement, and specification
   digests;
5. reviewed values, locations, units, sample counts, margins, diagnostics, and
   pass/fail/unknown boundaries.

## Run and verify

Install the repository's `conformance` extra. From the repository root, choose
a path that does not already exist:

```bash
python3 conformance/typed-evidence-v0alpha1/run.py \
  --evidence-file /tmp/openada-typed-evidence.json

python3 conformance/typed-evidence-v0alpha1/verify.py \
  /tmp/openada-typed-evidence.json
```

`run.py` refuses to replace an existing evidence file. Both commands read the
implementation and contract files from this checkout, so a passing record is
bound to their reviewed hashes rather than an unrelated installed package.
The digests expose changed content and stale bindings; they are not signatures,
publisher identity, or authentication.

## Scope

This bundle establishes deterministic behavior for the declared finite real
series and scalar limits only. It does not validate a native waveform parser,
EDA model fidelity, unit conversion, complex-series transforms, statistical
coverage, PDK conditions, or signoff suitability. Native-artifact lineage in
the fixture is deliberately marked `unverified`.
