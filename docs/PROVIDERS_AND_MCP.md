# Providers, marketplaces, and MCP

OpenADA separates engineering meaning from connection mechanics:

```text
engineering skill
  -> versioned OpenADA operation and assertion
  -> deterministic capability resolver
  -> conforming driver
  -> local CLI | future MCP/session/remote adapters
  -> native EDA
```

An agent should choose `circuit.simulate`, `result.measure`, or another
semantic operation before it chooses a transport. A transport may change where
or how an operation runs; it must not change what the operation means, what
evidence is required, or when its assertion is `pass`, `fail`, or `unknown`.

## Implemented explicit-provider boundary

OpenADA 0.4 implements the first deliberately small runtime slice of
`openada.driver-manifest/v0alpha1`:

```bash
openada provider validate path/to/driver-manifest.json
openada provider list --manifest path/to/driver-manifest.json
openada provider invoke \
  --manifest path/to/driver-manifest.json \
  path/to/openada-request.json
```

It is an explicit provider boundary, not discovery or a marketplace. The host
supplies one manifest and one complete `openada.request/v0alpha1`. Invocation
requires an exact `driver_selector`, `completion: wait`, one unambiguous
structured or workflow-validated capability, and one matching `local-cli`
transport with JSON stdin/stdout. `simulate --backend` and the reviewed built-in
driver registry remain unchanged. The external dispatch registry currently
contains only `openada.operation/circuit.simulate/v1alpha2`; listing a manifest
capability or finding another packaged profile does not make that profile
externally invocable.

Before launch the runtime validates:

- strict bounded JSON with duplicate-key and non-finite-number rejection;
- the manifest, request, locally installed operation profile, parameter schema,
  registered relational semantics, configuration roles, target/locator,
  required features, result schema, the profile side-effect mode against caller
  authority and capability declarations, maturity, conformance-record
  cross-references, and collective native-product coverage;
- canonical absolute regular non-symlink filesystem target and configuration
  files, with
  ceilings of 16 MiB for the target, 256 MiB for each configuration, and
  512 MiB for all request inputs together;
- host-side pre-launch identity/size/SHA-256 snapshots, including verification
  of every locator digest the request declares, plus a canonical absolute
  evidence destination whose canonical non-linked parent exists, whose final
  path is absent, and whose collision policy is exactly `fail-if-present`;
- an exact driver ID/version and optional transport ID; and
- one executable regular file resolved without a shell, plus canonical identity
  binding for standalone argv elements that already name existing regular
  files.

It then writes one bounded request to stdin, enforces the request timeout and
stdout/stderr ceilings, requires zero transport-process exit and empty stderr,
and kills the fresh process group on timeout, overflow, and after the parent
exits. Only descendants that remain in that group are killed; a process that
deliberately detaches can escape this containment, and the runtime is not a
sandbox. It rechecks executable and bound argv-file identity, then validates
the generic result, operation-specific data, operation name, correlation ID,
profile/assertion and provider identities, assertion truth table, evidence
roles and byte limits, and local recorded input/artifact files against their
declared size and SHA-256 under aggregate bounds. Every artifact path must be
canonical, absolute, and inside the authorized evidence destination; existing
artifacts must resolve there without symbolic-link traversal. A conclusive
circuit result must match the requested analysis and retain native tool
identity, a nonempty native command, and a native exit code; engineering `pass`
requires native exit zero. Before accepting any result, the host reopens every
request input and requires its identity, size, and SHA-256 and the provider's
corresponding retained input record to match the pre-launch snapshot. Mutation,
replacement, disappearance, or a conflicting provider record invalidates the
evidence. It neither grants trust nor contains credentials; installation and
execution approval remain host policy.

`request_id` is a correlation UUID, not a canonical digest of the complete
request. For a conclusive circuit result, each canonical absolute filesystem
target or configuration locator must also bind exactly one retained input
record and any caller-supplied SHA-256, but v0alpha1 does not generalize that
into complete request-content binding. Likewise, a manifest's conformance
records are self-declared metadata. The runtime validates their shape,
relationships, claimed level, URI, and digest fields; it does not fetch the URI
or independently rehash the declared conformance evidence.

The runtime intentionally does not scan directories, environment variables, a
network catalog, or MCP endpoints. It does not install, rank, update, or choose
between providers. Relative provider argv is resolved from the explicit working
directory (the CLI defaults to the manifest directory). Existing standalone
regular-file argv values are canonicalized and identity-checked; paths embedded
inside an option string are not discovered by that check, so provider packages
should keep the executable as argv zero and use standalone path arguments for
material entrypoint files.

## A possible small MCP surface

OpenADA does not currently ship an MCP server, client, or normative MCP
binding. One future adapter could expose two conceptual tools:

- `openada.capabilities` returns installed, schema-valid driver capabilities;
- `openada.invoke` accepts one versioned OpenADA request and returns one
  advertised OpenADA result.

Those names are illustrative, not reserved protocol identifiers. In such an
adapter, the request and result should stay identical to local or remote
invocation. MCP authentication, endpoint discovery, sessions, and artifact
transfer remain connection concerns. Credentials never enter an operation
profile or driver manifest.

An EDA may instead expose low-level native MCP tools. In that case a thin
OpenADA driver may call those tools as its native mechanism. The driver still
owns:

- validating the closed operation parameters;
- mapping the request to an exact native action sequence;
- rejecting unsupported features without substitution;
- bounding and validating native output;
- returning normalized facts, artifacts, diagnostics, and provenance; and
- passing the same conformance cases as a CLI-backed implementation.

Low-level MCP tool names are therefore not OpenADA operation IDs. A server is
not a conforming OpenADA provider merely because an agent can call it.

The current `openada.driver-manifest/v0alpha1` schema has no normative MCP
binding, MCP method vocabulary, authentication model, or artifact-transfer
contract. A `session-api` or `remote-job` record may carry `mcp` as an
experimental generic `protocol` label, but consumers must not infer portable
MCP behavior from that string. Real implementations should drive an additive
transport revision rather than retroactively assigning semantics to v0alpha1.

## What an EDA marketplace catalogs

A future OpenADA marketplace should catalog providers of versioned
capabilities, not an unreviewed list of executables or agent prompts. Each
entry should bind:

- an immutable driver manifest and content digest;
- publisher and package/source identity;
- exact operation, assertion, feature, locator, and result-schema IDs;
- supported transports and native products;
- capability-level maturity;
- immutable conformance records and the native versions they exercised; and
- installation, authentication, platform, license, and trust metadata.

Marketplace discovery is never execution approval. A host must install or
approve a provider under its own trust policy, validate the manifest, resolve
capabilities deterministically, and record the selected provider and native
tool in every result. Search ranking, popularity, or sponsorship must not
silently override an exact driver selector or a stronger evidence requirement.

Provider maturity belongs to one capability. A simulator driver may be
workflow-validated for transient analysis, structured for AC, and unsupported
for noise. Marketplace UI should show those rows separately rather than
assigning one badge to the whole EDA.

The now executable `openada.driver-manifest/v0alpha1` still cannot represent
that marketplace model fully: it has no independent `capability_id`, and one
capability groups all listed features under one maturity value and one
conformance-record list. The explicit resolver rejects ambiguity rather than
inventing identity. A future additive manifest revision must define
per-capability identity and feature-level evidence before these marketplace
rows become machine-readable.

## Mining a connector without turning traces into truth

Multi-agent exploration is useful for learning a new EDA surface, but an
exploration trace is a hypothesis, not a contract. Use this promotion loop:

1. Freeze a task corpus covering success, unsupported input, malformed input,
   unavailable tool, timeout, stale output, and native failure.
2. Let independent explorer agents attempt the corpus in isolated disposable
   environments. Retain commands, inputs, native outputs, artifacts, versions,
   and environment observations.
3. Have an independent scorer classify observable behavior without seeing an
   intended connector implementation.
4. Synthesize a proposed mapping from repeated observations: invocation,
   configuration, outputs, error classes, evidence bounds, and provenance
   limits.
5. Have a human freeze the operation truth table and native mapping. Ambiguous
   behavior remains unsupported or `unknown`.
6. Implement the driver against those frozen semantics.
7. Run public conformance fixtures plus held-out cases. Only independently
   checked evidence may raise capability maturity.

Explorers may discover native mechanics. They may not invent portable
semantics, relax an assertion, or promote maturity. That division lets the
community learn connectors quickly while keeping the narrow waist stable.

## Contribution boundary

Put reusable analog-engineering judgment, application selection, and review
discipline in skills. Put operation meaning and evidence thresholds in
profiles. Put simulator syntax, session recovery, file formats, and native
diagnostics in drivers. Put endpoint, authentication, and job/session plumbing
in transports.

This boundary is the marketplace compatibility rule: a new provider can change
every native mechanic below the driver and still serve existing skills when it
implements the same semantic capability honestly.
