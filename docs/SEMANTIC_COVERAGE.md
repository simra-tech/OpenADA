# Semantic coverage and release gating

OpenADA keeps implementation maturity and end-to-end evidence coverage as two
different facts. A feature may have a structured profile and deterministic unit
tests while still lacking the real-design chain needed for an agent to rely on
it. The repository coverage ledger makes that gap explicit and machine
enforceable.

The source inventory is
[`catalog/semantic-surfaces-v0alpha1.json`](../catalog/semantic-surfaces-v0alpha1.json).
It classifies every CLI leaf, gives packaged profiles an explicit lifecycle,
records every preflight route, and snapshots the semantic provider records
emitted by `capabilities`. It also inventories every shipped
`providers/*/driver-manifest.json`, its capability slices, and each provider
conformance claim. The verifier expands every profile feature and native
mapping, so adding an enum-backed analysis, implementation mapping, provider
feature, or packaged provider creates a new uncovered row automatically.

Run the normal offline audit with:

```bash
python tools/verify_semantic_coverage.py --mode audit
```

The output is deterministic JSON. It contains the complete row inventory, the
current implementation-maturity claim, the independently computed coverage
level, missing evidence, and a sorted gap list. It contains no wall-clock field,
so identical repository inputs produce identical output.

Exit behavior is deliberate:

- `0`: the catalog and repository inventory are valid; audit mode may still
  report coverage gaps;
- `1`: the inventory is valid but `--fail-on-gaps`, `--mode agent-ready`, or
  `--mode release` found an incomplete active row;
- `2`: a catalog, schema, profile, CLI, provider, preflight, chain record, hash,
  or other inventory invariant is invalid.

CI runs release mode as a required offline gate. A release checkout must first
prove that every byte-bound static manifest and the seven-record index are
mechanically current:

```bash
python tools/semantic_refresh_manifests.py
python tools/semantic_publish_index.py
python tools/verify_semantic_coverage.py --mode release
```

The release index at
[`conformance/semantic-chains/index.json`](../conformance/semantic-chains/index.json)
is generated only from the seven declared source-frozen receipts. Release mode
requires 147 active rows, zero gaps, zero issues, a passing offline verifier for
every chain, and a resolved provider receipt digest. The ledger never infers
coverage from maturity prose, unit-test counts, or self-declared provider
conformance metadata.

A provider manifest's `maturity`, conformance `level`, and `status` remain
implementation claims, not coverage. For a provider conformance claim to
resolve, a fully validated chain-index record must list its `record_id` in
`conformance_record_ids`, and the manifest's `evidence.sha256` must exactly
equal that index record's run-file SHA-256. An absent registration or digest
mismatch is emitted as the `registered-conformance-digest` gap. It does not
make audit mode an inventory failure, but it blocks agent-ready and release
modes. Resolution alone never promotes coverage: the same chain manifest must
explicitly cover the provider row and supply all evidence required by its
coverage level.

An all-zero `evidence.sha256` is explicitly reported as
`placeholder-digest` with a `non-placeholder-conformance-digest` gap. It can
never resolve a claim or pass a release gate.

The public-IHP ngspice-provider path is fail-closed at its final digest edge.
Its exact identities are:

- chain: `openada.chain/ihp-ngspice-provider-analyses/v1`;
- provider conformance record:
  `org.openada.conformance/ihp-analog-analyses-ngspice-provider/v1`;
- repository receipt:
  `conformance/ihp-ngspice-provider-analyses/semantic-chain-run.json`;
- offline registration point:
  `conformance/semantic-chains/index.json`.

The chain retains real OP, DC, AC, and TRAN artifacts and passes its independent
verifier, negative replays, and tamper replays. The index content-addresses the
source-frozen receipt and registers the conformance record ID; the provider
manifest carries that exact run SHA-256. Any missing registration, all-zero
placeholder, stale subject, or digest mismatch makes release mode fail.

## Coverage levels

Coverage levels are ordered but do not replace driver maturity:

- `unverified`: no accepted content-addressed chain record covers the row;
- `contract-tested`: a passing bounded contract-test receipt exists;
- `native-replayed`: contract evidence, a fresh native run, and an independently
  checked native artifact exist;
- `workflow-validated`: the replay additionally uses a pinned public design and
  carries normalized evidence into a downstream engineering decision;
- `agent-ready`: the row additionally has a trustworthy negative replay, a
  fail-closed tamper replay, and agent-visible decision evidence.

Every active semantic- or transport-execution row requires `agent-ready`.
Discovery, routing, and administrative leaves require `contract-tested`.
Historical profile rows remain visible in the report but do not become active
release obligations. An incomplete implementation should be
`experimental-hidden`; adding a waiver array is intentionally rejected.

## What a passing chain must contain

Chain manifests use
[`openada.semantic-chain/v0alpha1`](../schemas/semantic-chain-manifest-v0alpha1.schema.json).
A manifest pins:

1. a public repository, full commit, design subtree, exact inputs, and license;
2. the container digest, platform, PDK revision, and native tool identities;
3. every normative contract by repository path and SHA-256;
4. the exact semantic rows covered;
5. native semantic execution, an independent artifact oracle, normalization,
   and decision steps;
6. row-specific negative and tamper replays; and
7. the facts that must remain visible in the agent-facing result.

Every semantic-command step declares the nonempty set of catalog row IDs it
positively exercises; nonsemantic graph steps declare an empty set. The union
of those step-local sets must equal `manifest.covers` exactly, so adding an
unrelated row to the manifest cannot borrow evidence from the rest of the
workflow. EDA-driver provider/native-mapping rows and transport-execution rows
must be covered by at least one semantic-command step with
`native_execution: true`. Evidence-kernel rows remain nonnative: extraction,
measurement, and specification kernels consume retained EDA artifacts without
pretending to be native EDA execution. Administrative and discovery rows may
likewise be exercised by a nonnative semantic command.

A manifest alone cannot promote coverage. The index must pair it with a passing
[`openada.semantic-chain-run/v0alpha1`](../schemas/semantic-chain-run-v0alpha1.schema.json)
record. The run binds the manifest and current semantic-subject digest, and
every claimed check requires a present content-addressed artifact with the
corresponding evidence role. A stale subject, missing file, altered byte count,
wrong digest, synthetic design advertised as real, unknown coverage row, or
negative/tamper replay that does not cover that exact row invalidates the
record.

Every run artifact also names its exact `source_step` and `source_output` from
the manifest DAG. Native artifacts must originate at native semantic-command
steps, oracle evidence at independent-oracle steps, normalized evidence at
nonnative semantic-command steps, downstream decisions at
their nonnative semantic-command steps, and agent-visible evidence at the
declared independent-decision result step. Negative and tamper artifacts instead name one declared `replay_id`; a
passing replay check requires exactly one distinct artifact for every replay.
Repository paths may not be reused, and trust artifacts may not reuse content
digests across roles or replay cases. This prevents a native result, its oracle,
and the final decision from being aliases of one retained file.

The semantic subject includes the OpenADA implementation, profiles, schemas,
catalog, verifier, packaging/entry-point metadata, every shipped provider
configuration schema, every provider launcher, and provider manifest semantics.
Provider capability, transport, native-product, driver-version, and conformance
metadata changes therefore stale an existing receipt. The one deliberate
exception is `conformance_records[*].evidence.sha256`: that detached field is
normalized before hashing because it points to the run receipt that already
contains the subject digest. Changing only that claim digest does not change
the subject; changing any execution or capability meaning does. This breaks the
attestation cycle without allowing provider behavior to escape the run binding.
A chain that registers a provider conformance record must not also list that
provider's `driver-manifest.json` as a byte-exact manifest contract. Doing so
would recreate the impossible manifest → run → claim-digest cycle; the release
gate rejects it explicitly. The semantic-subject binding remains the authority
for provider semantics, while index resolution binds the detached receipt
digest.

Each chain-index record has exactly `id`, `conformance_record_ids`, `manifest`,
`run`, and `extensions`. `manifest` and `run` are closed file references with a
repository-relative path and SHA-256. Use an empty `conformance_record_ids`
array for a chain that proves semantic rows but backs no shipped-provider
claim. A conformance ID may be registered only once and must name a provider
conformance record already present in the closed repository inventory.

The independent oracle must consume native artifacts directly. It must not use
an OpenADA-normalized measurement as its expected value or import the operation
implementation it is checking. Review still matters: schemas can prove the
declared graph and byte bindings, not intellectual independence of two
algorithms.

## Adding or changing a semantic surface

An implementation change that adds a CLI leaf, profile, feature, native
mapping, provider record, or preflight assertion must update the catalog in the
same change. The offline gate will reject either side of a mismatch.

Before making the row active:

1. add a chain manifest over a pinned real open design;
2. run it in a fresh evidence directory with EDA networking disabled;
3. independently check the native artifact;
4. carry normalized evidence to the decision an agent will make;
5. exercise an engineering-negative variant and a boundary-specific tamper;
6. retain exact diagnostic and artifact evidence; and
7. add the content-addressed manifest/run pair to the chain index.

Release publication is intentionally source-frozen. Finish semantic and chain
code, refresh the static manifest references with
`python tools/semantic_refresh_manifests.py --write`, and commit that source
state before running native EDA. Publish one verified evidence bundle from the
clean checkout, commit that evidence-only change, and repeat for the next
chain. Evidence-only commits may have different Git trees while retaining the
same semantic-subject digest; the gate verifies each attested revision and
tree directly.

After all seven receipts exist, run
`python tools/semantic_publish_index.py --write`. Set any registered provider
conformance digest to the generated index entry's exact run SHA-256, commit the
index/digest update, and rerun the three no-write checks shown above. Do not
hand-maintain index hashes or reuse a provisional receipt.

If the chain backs a shipped provider conformance record, also list that record
ID on the index entry and set the provider record's `evidence.sha256` to the
exact SHA-256 of the final run JSON. Recompute it after any run-record change.

Do not raise coverage because a native process returned zero, a synthetic
fixture passed, or the same implementation recomputed its own output.
