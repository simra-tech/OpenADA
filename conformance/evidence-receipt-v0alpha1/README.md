# Scheduler-sealed typed-evidence receipts

`openada.conformance_receipt` closes the authenticity gap left by ordinary
result-envelope digests. SHA-256 proves content linkage; it does not stop an
agent from inventing every envelope and recomputing every digest. A receipt
adds an Ed25519 seal made by a scheduler key that the job cannot access.

The production module is verify-only. It contains no signer, private-key
loader, environment-key convention, or CLI that lets a job select its own
trust inputs.

## Trust boundary

The scheduler, outside the agent's authority, must:

1. freeze a per-attempt job ID and claim/subject manifest;
2. freeze the expected simulation context, exact series-selection bytes, and
   exact measurement-request bytes;
3. launch or independently observe the native tool in an isolated execution
   boundary, rather than sign arbitrary paths supplied by the job;
4. capture the exact result envelopes, raw artifact, simulation inputs,
   configuration collateral, launcher, and log;
5. seal those captured bytes with a non-exportable Ed25519 private key.

The verifier receives the public key and expected digests out of band. Values
inside the receipt never select their own authority.

A signature authenticates what the scheduler attested. It cannot make a
dishonest or incorrectly implemented scheduler truthful. In particular, a
collector that merely signs caller-named files has not proved that ngspice
produced them. Native process observation and executable identity belong in
the privileged collector.

## Closed chain

The signed sidecar binds:

- one `simulate` envelope;
- its unique `simulation.result` raw artifact;
- the public CLI series selection shape
  `{selectors, conditions, extensions: {}}`;
- one passing `result.series.extract` envelope;
- the public CLI closed measurement-request object;
- one passing `result.measure` envelope;
- every other existing top-level simulation input and artifact, including the
  deck, model/PDK collateral, managed configuration, launcher, and log.

`data.extensions.org.openada.configuration` must be an exact projection of
captured configuration inputs. Extra, missing, duplicated, or digest-mismatched
references are refused.

The scheduler-pinned simulation-context digest covers the tool record,
protocol, requested analysis parameters, ordered content-bound inputs,
configuration projection, simulation target, and PDK-binding facts. Together
with the independently pinned selection and measurement-request digests, this
prevents a valid same-job chain from being reassigned to a different deck,
corner, stimulus, vector, condition, or scalar claim.

## Stable capture and replay

All paths must be normalized absolute paths with no symlink ancestor or final
component and exactly one hard link. Directory components are opened with
`O_DIRECTORY|O_NOFOLLOW`, retained, and required to resolve to one consistent
inode across the entire verification. Each file is read once from a retained
descriptor and checked again before descriptors close.

Extraction and measurement are replayed from the captured bytes. Equality is
canonical JSON-byte equality after removing only the nondeterministic
`provenance.created_at` field; Python's `True == 1.0` coercion cannot satisfy
replay. The return value is an immutable `VerifiedTypedEvidence` snapshot
containing the authenticated scalar and exact measurement-envelope bytes, so
a consumer never reopens a path after verification.

The receipt verifier is intentionally versioned `v0alpha1`. A change to
deterministic replay behavior requires a new receipt version or an explicit
compatibility implementation; silently replaying old receipts under changed
semantics is not acceptable.

## Deterministic attacks

`tests/test_conformance_receipt.py` covers:

- self-authored, self-consistent envelopes sealed by an attacker key;
- cross-job and cross-claim laundering;
- wrong selection conditions and vectors;
- unbound configuration and forged tool context;
- stale raw bytes and file replacement during capture;
- ancestor-symlink races and hard path-identity checks;
- replayed scalar type confusion and recomputed false values;
- missing simulation inputs/logs;
- malformed operation data, control-character paths, deep JSON, and descriptor
  cleanup on refusal.

These tests exercise protocol verification. Their ephemeral test signer stands
in for the privileged collector; it is not production signing code.
