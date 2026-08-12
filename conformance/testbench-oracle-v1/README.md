# Testbench-oracle comparator conformance

This model-free v1 bundle exercises the pure testbench-plan oracle comparator.
It performs no simulation, reads no network resource, and has the immutable
conformance identity `testbench-oracle-comparator-v1`. The semantic operation
identity used by the bundle is
`openada.operation/testbench.oracle.compare/v1alpha1`.

## Coverage

The retained synthetic case covers all 12 ratified `pll2-cpchar-sg13g2`
meta-rows in their published order:

1. signed response coverage;
2. absolute zero-crossing offset error;
3. guarded relative local-gain error;
4. guarded-relative source-current curve error;
5. guarded-relative sink-current curve error;
6. absolute mismatch-fraction curve error derived from source/sink charge;
7. absolute compliance-endpoint error;
8. absolute leakage error;
9. invalid-detection recall over oracle-invalid declarations;
10. false-valid rate over submitted VALID declarations;
11. condition-by-observable completeness; and
12. grading runtime.

Every reported observable has one condition receipt with both compiled-deck and
waveform digests. The comparison therefore also exercises mandatory numeric
lineage. The fixture includes one oracle-invalid verdict so both validity rates
have nonempty, independently defined denominators.

The manifest hash-binds the comparator source, all three Draft 2020-12
contracts, the input documents, the exact reviewed comparison, and four tamper
cases. The standalone verifier does not import or invoke OpenADA. It validates
the contracts, exact 12-row order, limits, values, reasons, status/count
summaries, validity confusion counts, canonical comparison digest, and fixture
bindings. It also applies the retained value, status, summary, and fixture-hash
tamper cases and proves that each is rejected at its declared boundary.

## Run and verify

Install the repository's `conformance` extra. From the repository root, choose
an evidence path that does not already exist:

```bash
mkdir -p .conformance-evidence
python3 conformance/testbench-oracle-v1/run.py \
  --evidence-file .conformance-evidence/testbench-oracle.json

python3 conformance/testbench-oracle-v1/verify.py \
  .conformance-evidence/testbench-oracle.json
```

`run.py` refuses to overwrite an existing evidence file. It imports the public
comparator from this checkout and then invokes the independent verifier.
`verify.py` uses only the retained files and general JSON/JSON-Schema support;
it never imports OpenADA. Hashes expose stale or altered content but are not
signatures, publisher authentication, or proof of trusted execution.

## Fixture policy and scope

The observed and oracle documents are MIT synthetic data shaped after the
licensed reference output; they contain no DUT or third-party netlist text.
The tolerance fixture is the 12-row ratified policy fixture. Its 0.5 nA
guarded-relative current floor is a concrete conformance-policy choice derived
from the ratified absolute leakage tolerance; it is not claimed as a physical
signoff limit.

This bundle establishes deterministic comparator semantics, schema closure,
lineage presence, and tamper detection for one bounded case. It does not
validate simulation accuracy, receipt authenticity, PDK/model identity,
runtime measurement trust, hidden-fixture coverage, manufacturing fitness, or
signoff suitability.
