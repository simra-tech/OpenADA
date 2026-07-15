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

The review-only `openada.driver-manifest/v0alpha1` cannot represent that model
fully: it has no independent `capability_id`, and one capability groups all
listed features under one maturity value and one conformance-record list. A
future additive manifest revision must define per-capability identity and
feature-level evidence before these marketplace rows become machine-readable.

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
