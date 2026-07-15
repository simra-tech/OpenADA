# Driver conformance kit

This small kit helps a contributor prove that a structured driver returns the
declared OpenADA result contract. It validates the complete JSON Schema, checks
the expected operation and status pair, requires named artifact roles or
diagnostic codes, and can independently rehash every recorded input and
artifact.

Install the optional validation dependency from a checkout:

```bash
python -m pip install -e '.[conformance]'
```

Capture exactly one CLI result, then check it:

```bash
openada simulate fixtures/smoke/smoke_ngspice.cir \
  --output-dir /tmp/openada-smoke > /tmp/openada-result.json

python conformance/driver-kit/check_result.py \
  /tmp/openada-result.json \
  --expect-operation simulate \
  --expect-execution completed \
  --expect-engineering pass \
  --require-artifact-role evidence \
  --verify-files
```

The checker emits one small JSON document. Exit `0` means every check passed,
exit `1` means the result is well-formed but failed a declared expectation, and
exit `2` means the checker could not safely read or validate the input. Result
JSON is bounded at 5 MiB. Recorded-file verification rejects symlinks and
non-regular files, and defaults to 100 MiB per file, 512 files, and 512 MiB in
aggregate. Change those bounds explicitly with `--max-file-mib`, `--max-files`,
or `--max-total-mib` when a fixture genuinely needs more.
At most 100 conformance issues are returned and each issue is bounded to 2,000
characters, so malformed documents cannot expand the checker output without
limit.

## Request and manifest scaffolding

The kit also contains valid starting points for the review-only external-driver
protocol:

- [`request.template.json`](request.template.json) validates against
  [`openada.request/v0alpha1`](../../schemas/request-v0alpha1.schema.json);
- [`driver-manifest.template.json`](driver-manifest.template.json) validates
  against
  [`openada.driver-manifest/v0alpha1`](../../schemas/driver-manifest-v0alpha1.schema.json).
- [`operation-profile.template.md`](operation-profile.template.md) is the RFC
  checklist for one operation, primary assertion, evidence truth table, and
  cross-backend mappings.
- [`circuit.simulate-v1alpha2.json`](../../profiles/circuit.simulate-v1alpha2.json)
  is the active concrete simulation profile and validates against
  [`openada.operation-profile/v0alpha1`](../../schemas/operation-profile-v0alpha1.schema.json).
  Its v1alpha1 predecessor remains packaged unchanged for historical records.

The current CLI records the built-in circuit-simulation profile and selected
ngspice/Xyce driver, but it does not accept the generic request envelope or
discover external manifests. These files make request, capability, transport,
maturity, and conformance assumptions reviewable before runtime driver
discovery is implemented. See the
[driver protocol](../../docs/DRIVER_PROTOCOL.md) for validation and reference
rules that JSON Schema alone cannot enforce.

## Same-intent simulation proof

Use the same operation shape for both built-in mappings:

```bash
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend ngspice --output-dir /tmp/ngspice-run
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend xyce --output-dir /tmp/xyce-run
```

Omitting `--backend` keeps the legacy ngspice interface as the default.
Cross-backend fixtures are limited to one self-contained advertised OP, DC, AC,
or transient analysis with no includes, measurements, print directives,
control-language blocks, or multiple analyses. They must assert the same
primary truth table and normalized fact names while retaining and checking each
driver's native command, log, and waveform evidence.

The pinned native portability replay now exercises both mappings with ngspice
46 and Xyce 7.10-opensource, then independently parses both raw formats and
checks the same model-free RC behavior:

```bash
python3 conformance/circuit-simulate-v0alpha2/run.py \
  --evidence-dir /tmp/openada-circuit-simulate-evidence
```

The expanded success replay supports structured OP/DC/AC capability rows. The
shared transient rows retain their earlier workflow-validated evidence. The
new success-only cases do not independently establish every outcome required
for workflow-validated maturity, and they do not widen the profile to includes,
measurements, control language, or unadvertised analysis types.

The same checks are available to tests:

```python
from openada.conformance import assert_result_conforms

assert_result_conforms(
    payload,
    expected_operation="simulate",
    expected_execution_status="completed",
    expected_engineering_status="pass",
    required_artifact_roles=("evidence",),
    verify_recorded_files=True,
)
```

## Minimum structured-driver cases

A new structured operation should cover at least:

1. one successful native or fake-tool invocation with the expected artifact;
2. one valid engineering failure when the native evidence supports `fail`;
3. invalid input, reported as execution `invalid_request` and engineering
   `unknown`;
4. an unavailable executable, reported as execution `not_available` and
   engineering `unknown`;
5. missing, empty, malformed, or incomplete native output, which must never be
   promoted to an engineering pass;
6. bounded runtime and retained-output behavior.

Use fake executables for deterministic unit cases. Workflow validation is a
separate maturity gate: it needs a pinned public design, tool/runtime and PDK
identity, real native artifacts, and independently checked engineering
assertions like the `ihp-inverter` recipe.

## Extension path

1. Add deterministic binary names and bounded version probes to
   `openada.discovery.TOOL_SPECS`. This earns only **discovered** maturity.
2. Implement a semantic driver under `src/openada/engines`. Validate inputs
   before launch, call `run_process` with a finite timeout and explicit working
   directory, retain native evidence, and build the result with
   `openada.contract.result`.
3. Export the driver from `openada.engines`, add the semantic CLI operation,
   and keep process status independent from engineering status.
4. Add fake-tool unit cases and check their results with this kit. This earns
   **structured** maturity after review.
5. Add a separately marked, opt-in real replay with pinned public inputs and an
   independent verifier before claiming **workflow-validated** maturity.

Do not make the conformance checker the source of engineering truth. The driver
must derive its conclusion from bounded native evidence, and a real workflow
verifier should parse the retained native artifact independently.
