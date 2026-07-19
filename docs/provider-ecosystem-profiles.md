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

## Additive provider ecosystem contracts

The `openada.ecosystem` package supplies an experimental, vendor-neutral host
SDK around these hidden profiles. It does not change the existing provider
runtime or advertise a new native capability. Its additive schemas define:

- trusted external provider bundles, exact driver mappings, feature-level
  capability manifests, and executable operation-validator dispatch;
- canonical `openada.request/v0alpha2` bindings, host-authorized invocation
  contexts, readiness reports, multi-step `openada.result/v0alpha2` evidence,
  and independent session/job receipts;
- closed revisioned locators for regular files, directory snapshots,
  workspaces, artifacts, sessions, native objects, and allowlisted URIs; and
- generic conformance receipts that keep protocol, artifact, semantic,
  transport, and human-review readiness separate.

An external bundle is loaded only from an explicit host-approved root. The
loader reads bounded regular files without following the final symlink, checks
stable filesystem identity and exact SHA-256 bytes, rejects duplicate JSON
members and identity conflicts, requires an already registered host-trusted
validator, and can detect post-registration mutation. A bundle manifest cannot
name executable imports, commands, environments, credentials, or setup text.
Discovery never scans directories or fetches content.

`AgentSessionTransport` and `DeterministicFakeScheduler` are executable offline
transport models, not live external-tool adapters. They support direct typed
callables only. The session model binds an owner, host-generated nonce and
ownership-token hashes, monotonic heartbeat and invocation sequences, replay
identity, cancellation, and owner-only cleanup. The scheduler model binds
payload and artifact identities, deterministic idempotency keys, lifecycle
polling, cancellation acknowledgment, orphan detection, reconnect, collection,
and cleanup. Neither accepts shell or command templates.

## Canonical JSON v1

`openada.canonical-json/v1` is a bounded cross-language subset: JSON null,
booleans, Unicode strings without lone surrogates, IEEE-754-safe integers,
arrays, and objects with lexicographically sorted Unicode keys. Binary floating
point is rejected; engineering decimals carry explicit units in strings. The
limits are 64 levels, 100,000 members/items per container, and 4 MiB encoded.
For request identity, `canonical.sha256` is replaced by 64 zeroes before
encoding and hashing. Reusing a request UUID with different canonical bytes is
an error.

Fixed vector:

```text
value:  {"z":[true,null,"µ"],"a":-2}
bytes:  {"a":-2,"z":[true,null,"µ"]}
sha256: 6304c162b51538389dca506d39fc0cd405b5a2928c1e8dabde40a13d5e41a536
```

The request contract permits only one opaque context name. Recursive runtime
checks reject request parameter keys that attempt to carry argv, commands,
shell text, environment values, credentials, passwords, secret handles or
stores, native actions, import paths, or setup text. The host resolves an
approved name to a provider-specific filtered context; callers never receive
the secret values behind any handle.

## Discovery and offline conformance

The separate JSON-first command keeps these experimental contracts away from
the established CLI surface:

```bash
openada-ecosystem schema list
openada-ecosystem schema validate capability.json
openada-ecosystem request bind request.json
openada-ecosystem transport
openada-ecosystem fake-conformance
```

`fake-conformance` runs deterministic `org.example.*` positive, engineering
negative, semantic rejection, bounds, and isolation fixtures. Its receipt is
self-attestation: it proves the public SDK paths execute, but it does not prove
external backend availability, workflow review, or signoff approval.
