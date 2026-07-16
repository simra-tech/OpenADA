# OpenADA request and driver protocol

OpenADA's driver protocol is the machine-readable boundary between a stable
engineering intent and one deterministic native implementation. It lets an
agent select a capable driver without turning the public ontology into a copy
of ngspice, Xyce, Xschem, KLayout, Netgen, Yosys, OpenROAD, or another tool's
command surface.

This document specifies the reviewed protocol and its deliberately small first
runtime binding. The current CLI accepts operation-specific flags and has
in-tree drivers. `provider invoke` also accepts one complete
`openada.request/v0alpha1` together with one explicitly supplied manifest and
resolves one local JSON-stdio `wait` transport. External dispatch is currently
registered only for `circuit.simulate/v1alpha2`; the other packaged profiles
use their built-in CLI bridges. It does not discover, install, rank, or approve
external manifests, and it does not implement the session, remote-job,
marketplace, or MCP bindings described as future work below.

The published protocol documents are:

- [`openada.request/v0alpha1`](../schemas/request-v0alpha1.schema.json), a
  transport-neutral request for one operation and assertion;
- [`openada.driver-manifest/v0alpha1`](../schemas/driver-manifest-v0alpha1.schema.json),
  a driver's identity, transports, capabilities, and conformance evidence;
- [`openada.operation-profile/v0alpha1`](../schemas/operation-profile-v0alpha1.schema.json),
  the closed shape for operation meaning, truth tables, facts, evidence, and
  SPICE-oriented backend mappings;
- [`openada.operation-profile/v0alpha2`](../schemas/operation-profile-v0alpha2.schema.json),
  an additive immutable shape that also supports deterministic semantic
  feature-to-algorithm bindings;
- [`circuit.simulate/v1alpha1`](../profiles/circuit.simulate-v1alpha1.json), the
  immutable historical 0.2.x simulation profile;
- [`circuit.simulate/v1alpha2`](../profiles/circuit.simulate-v1alpha2.json), the
  active typed shared simulation profile;
- [`result.measure/v1alpha1`](../profiles/result.measure-v1alpha1.json), the
  canonical-digest-bound normalized-series measurement profile;
- [`result.series.extract/v1alpha1`](../profiles/result.series.extract-v1alpha1.json),
  the exact native-artifact-to-normalized-series profile;
- [`result.spectral.measure/v1alpha1`](../profiles/result.spectral.measure-v1alpha1.json),
  the coherent single-tone SNR, SINAD, THD, and SFDR profile;
- [`result.transfer.measure/v1alpha1`](../profiles/result.transfer.measure-v1alpha1.json),
  the same-unit AC complex-ratio and closed crossing-measurement profile; and
- [`specification.evaluate/v1alpha1`](../profiles/specification.evaluate-v1alpha1.json),
  the exact-unit specification profile.

The digital set adds:

- [`rtl.lint/v1alpha1`](../profiles/rtl.lint-v1alpha1.json), strict
  SystemVerilog lint evidence;
- [`logic.synthesize/v1alpha1`](../profiles/logic.synthesize-v1alpha1.json),
  generic inference and Liberty-mapped synthesis evidence; and
- [`timing.analyze/v1alpha1`](../profiles/timing.analyze-v1alpha1.json),
  single-corner setup/hold timing evidence.

Those are nine active profiles plus the immutable historical circuit profile.
`openada profile list` returns their installed identities and
`openada profile show OPERATION-PROFILE-ID` returns one complete validated
document. Profile inspection is not external-provider discovery.

The built-in `measure`, `spectral`, and `transfer` bridges accept either their
canonical normalized-series document or one complete passing
`result.series.extract/v1alpha1` envelope. In the latter form the host validates
the envelope and extraction-profile data and unwraps only a verified embedded
series; the downstream operation still validates its own canonical series
digest, while the workflow retains the extraction result as the upstream
native-binding record.

Complete valid examples that are safe to copy and replace live in the
[request template](../conformance/driver-kit/request.template.json) and
[driver-manifest template](../conformance/driver-kit/driver-manifest.template.json).
New shared semantics start from the
[operation-profile RFC template](../conformance/driver-kit/operation-profile.template.md).
The `example.org` identities and zero digests in those files are placeholders,
not OpenADA capability claims.

The active built-in alpha bridge maps
`openada.operation/circuit.simulate/v1alpha2` to ngspice or Xyce through the
same CLI shape:

```bash
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend ngspice --output-dir /tmp/ngspice-run
openada simulate conformance/circuit-simulate-v0alpha2/fixtures/rc-transient.cir \
  --backend xyce --output-dir /tmp/xyce-run
```

When `--backend` is omitted, the legacy ngspice interface remains the default.
The shared subset is one self-contained OP, DC, AC, or transient analysis, with
no includes, measurements, print directives, control-language blocks, FFT,
noise, Monte Carlo, or multiple analyses. The ngspice mapping is structured
for OP/DC/AC and workflow-validated for TRAN; Xyce is structured for DC/AC,
workflow-validated for TRAN, and rejects OP as unsupported. The pinned
[circuit-simulation portability replay](../conformance/circuit-simulate-v0alpha2/README.md)
independently parses native success evidence by analysis. The generic request
and external-manifest protocol is executable only through the explicit local
provider boundary documented in
[Providers, marketplaces, and MCP](PROVIDERS_AND_MCP.md); automatic discovery
and every non-local transport remain outside the runtime. In this release that
boundary invokes only the active `circuit.simulate/v1alpha2` profile.

## Invariants

Every invocation has one versioned operation profile and one versioned primary
assertion profile:

```text
agent request
  = operation meaning
  + one assertion and its evidence threshold
  + explicit target and configuration
  + evidence and execution policy

driver
  = deterministic translation to native actions
  + validation of native evidence
  + normalized OpenADA result
```

The native files, EDA database, PDK, models, decks, runsets, and reports remain
authoritative. A driver must not weaken an assertion, substitute project
collateral, or reinterpret an unsupported locator merely to complete a
request.

Process completion and engineering truth remain independent. A simulator that
exits zero may still produce an engineering `fail` or `unknown`; valid
simulation evidence does not mean a circuit meets its specification.

## Immutable identities and version axes

Schema, profile, implementation, and native-product versions answer different
questions:

| Identity | Example | Meaning |
|---|---|---|
| Request schema | `openada.request/v0alpha1` | Shape and base semantics of the request envelope |
| Operation profile | `openada.operation/circuit.simulate/v1alpha2` | Tool-independent action and parameter meaning |
| Assertion profile | `openada.assertion/simulation.evidence.valid/v1alpha1` | Exact claim and `pass`/`fail`/`unknown` evidence rules |
| Driver | `org.example.openada.driver.example-spice` at `0.1.0` | One implementation release |
| Native product | `org.example.eda.example-spice` with an observed native version | Product actually used below the driver |
| Result schema | `openada.result/v0alpha1` | Shape and common status semantics of returned evidence |

Published schema and profile identifiers are immutable. A change to a required
field, status meaning, assertion threshold, normalized fact, or closed shape
requires a new identifier and a new file. An implementation fix or new native
version may instead require a new driver version and new conformance evidence.

`circuit.simulate/v1alpha1` remains an immutable historical profile. Its
additive `circuit.simulate/v1alpha2` successor continues to use the immutable
v0alpha1 profile schema. The deterministic `result.measure/v1alpha1`,
`result.series.extract/v1alpha1`, `result.spectral.measure/v1alpha1`,
`result.transfer.measure/v1alpha1`, and `specification.evaluate/v1alpha1`
profiles use additive v0alpha2; they require semantic implementation mappings
rather than pretending every evidence kernel is a native EDA backend. Their CLI
bridges record profile and implementation identities in the existing
operation-owned result data. The base request envelope is dispatchable only
through `provider invoke` with one explicit manifest, exact selector, supported
local `wait` transport, and a host-registered semantic/result validator; only
`circuit.simulate/v1alpha2` currently meets that last condition.

OpenADA-owned profile and feature IDs use `openada.operation/...`,
`openada.assertion/...`, and `openada.feature/...`. Third parties use a
reverse-DNS prefix such as `org.example.openada.operation/...`. Driver and
native-product IDs are also reverse-DNS names. Versions are part of every
semantic profile ID; consumers must compare the complete string rather than
guess compatibility from a prefix.

## Request envelope

A request contains no natural-language fallback. It identifies the exact
profile pair, target, configuration, operation parameters, evidence policy,
evidence destination, execution constraints, and optional driver selection.
`request_id` correlates request and result; it is not a digest or signature of
the other request fields. Consumers that need content binding must verify the
declared target/configuration identities and retained input records rather than
treating UUID equality as proof that the whole request is unchanged.

Before schema validation, a host must bound transport input. The alpha
reference limits are 1 MiB for one request and 4 MiB for one installed manifest;
hosts may choose lower limits. A host must also bound JSON nesting and reject
duplicate object keys. The open base `parameters` and namespaced `extensions`
objects do not authorize unbounded input; operation schemas and transports must
apply their own tighter structural limits.

### Targets and configuration

`target.locator` is discriminated by `type`:

| Locator | Intended use | Required identity |
|---|---|---|
| `filesystem` | A native local file or directory | Explicit path; optional expected SHA-256 |
| `eda-session` | A live design object behind an editor or runtime session API | Session ID and opaque native object reference |
| `artifact` | An artifact already managed by a harness or evidence store | Artifact ID; optional URI and digest |
| `uri` | A directly addressable immutable or remote resource | URI; optional digest |
| `workspace` | A path inside a named local or remote job workspace | Workspace ID and workspace-relative path |

A driver must support the locator type for the selected capability. It must not
turn an unresolved session object into a filesystem search, fetch an undeclared
URI, or treat a mutable artifact ID as content-addressed evidence.

The implemented external `circuit.simulate/v1alpha2` binding is narrower than
the base schema: the target and every configuration locator must name a
canonical absolute regular non-symlink file. The target ceiling is 16 MiB,
every configuration-file ceiling is 256 MiB, and their aggregate ceiling is
512 MiB. The host snapshots file identity, size, and SHA-256 before launch and
checks any locator digest against those bytes. The
[`request.template.json`](../conformance/driver-kit/request.template.json)
therefore uses absolute path placeholders; replace them with real canonical
regular-file paths rather than making them relative to the manifest or working
directory.

`configuration` is an explicit list of role-bearing references such as a PDK,
model library, corner, rule deck, LVS setup, runset, waiver file, or startup
script. Operation profiles define the roles they require. Credentials and
license secrets are never configuration references; transports receive those
through their host environment or credential system.

`parameters` is intentionally open only in the base request schema. Dispatch
requires a second validation against the closed parameter schema published by
the selected operation profile. A base-schema-valid request is not sufficient
to invoke a driver when its operation profile is unknown or unavailable.

### Evidence identity

`evidence_policy.identity_requirement` states what kind of artifact identity
the caller needs:

- `content-digest`: the complete authoritative bytes can be captured and
  content-addressed;
- `native-revision`: the authoritative design store supplies a stable native
  revision identity;
- `snapshot`: a retained point-in-time native snapshot is the available
  identity boundary;
- `best-available`: the caller accepts the strongest honest identity the
  driver can report, including a bounded observation with an explicit
  provenance limitation.

These mechanisms are not interchangeable for every native database. A driver
must decline an unsatisfied requirement rather than invent a digest for a
partial export or call a bounded observation a complete snapshot. It must
report the identity actually observed and any incomplete provenance in the
result evidence.

Required artifact roles and log/native-artifact retention are also explicit.
The caller's byte limits are ceilings, not permission to accept truncated
evidence as an engineering pass.

### Evidence destination

`evidence_destination` grants a bounded place for current-run artifacts; it is
not design-write authority. A local filesystem destination must be absolute.
A workspace destination is resolved only inside its named workspace. The
collision policy is explicit: `fail-if-present` requires a new destination,
while `replace-driver-owned` permits replacement only of the exact artifact
paths declared by the operation. Drivers must leave unrelated files untouched
and must never hide this required location in a non-semantic extension.

The current external simulation runtime implements only the filesystem and
`fail-if-present` branch. The destination path must be canonical and absolute,
must not exist before launch, and must have an existing canonical non-linked
parent directory whose identity remains stable during execution. Every artifact
path in the returned envelope must be canonical, absolute, and beneath that
destination; an existing artifact must resolve there without symbolic-link
traversal. The request template's `/tmp/openada-circuit-simulate-example` value
is an absolute placeholder: its parent must already exist and the final path
must still be absent when invoked.

### Execution and side effects

`completion: wait` asks for the final result on the current invocation.
`completion: submit` permits a remote or queued transport to return a job
receipt and deliver the final result later. Acceptance into a queue is not an
engineering result. The alpha protocol does not yet standardize the job-receipt
shape, so a manifest may advertise `submit` only when its transport documents
that out-of-band lifecycle.

`side_effects` is the maximum authority granted by the caller:

- `read-only`: do not create evidence or modify the design;
- `evidence-only`: native execution may create declared run artifacts but must
  not mutate the authoritative design;
- `transactional-design-write`: a mutation profile may perform its declared
  write set under the mutation lifecycle.

The last value is not itself write authorization. A mutation operation must
still bind a reviewed plan, expected base revision, authorization scope,
transaction policy, and postconditions as specified by the
[mutation proposal](MUTATION_AND_VERSIONING.md). Drivers may always impose a
stricter side-effect policy.

### Driver selection

When `driver_selector` is present, the resolver must match its exact driver ID,
optional exact driver version, transport, and required feature IDs. It must not
silently substitute another driver.

When the selector is absent, a host may negotiate from installed manifests. A
deterministic resolver filters by the complete operation and assertion IDs,
locator type, completion and side-effect modes, required features, and
available native products. It then applies a host-documented stable preference
order. The eventual result must disclose what was actually selected.

## Driver manifest

A manifest is an installed implementation declaration, not proof that a native
tool is currently installed, reachable, or suitable for a particular project.
It contains:

- a reverse-DNS driver ID and semantic implementation version;
- every material native product the driver orchestrates;
- one or more local CLI, live-session API, or remote-job transports;
- capability records binding operation/assertion profiles to features,
  locators, transports, native products, result schemas, side effects, and
  completion modes;
- immutable conformance-record summaries and content-addressed evidence.

The three maturity values retain their existing narrow meanings:

- `discovered`: the product can be resolved and its version bounded, but the
  profile must not be dispatched as a structured operation;
- `structured`: versioned semantic input and normalized result behavior have
  deterministic conformance cases;
- `workflow-validated`: a pinned public workflow independently validates the
  native artifacts and engineering assertion.

Maturity belongs to a capability, not to a tool family or an entire EDA suite.
A driver can be workflow-validated for transient simulation and unsupported
for noise. It must not advertise an absent operation merely because the native
tool has an unrelated command with that name.

### Trust and installation

A manifest is not an installation instruction or a trust certificate. A host
must invoke only drivers installed or explicitly approved through its own trust
policy. Discovery must not download code, follow a manifest-provided package
URL, or execute an unreviewed binary automatically. For local CLI transports,
the host resolves the literal argv executable without a shell and applies its
normal executable ownership/path policy. The current runtime also resolves and
canonicalizes standalone argv elements that already name existing regular
files; it compares the executable and those bound-file identities before and
after invocation. This is not a package trust decision and does not recognize
paths embedded inside option strings such as `--config=/path`.

Manifests and requests must not contain credentials, license secrets, session
tokens, or mutation authorization secrets. Those remain in the host's
credential/authorization boundary and are disclosed to a driver only for an
explicit approved invocation. Semantic authorization scope still belongs in a
typed mutation request so the resulting receipt can be audited without
revealing the secret that granted it.

The JSON Schema checks document shape but cannot enforce references between
arrays. A manifest consumer must additionally verify that:

1. driver, product, transport, capability, and conformance IDs are unique in
   the resolved registry;
2. every capability's transport and product IDs exist in that manifest;
3. requested modes are supported by both the capability and selected
   transport;
4. every referenced conformance record exists and matches the capability's
   profile pair and driver version;
5. every `structured` or `workflow-validated` claim references at least one
   matching, passing record at the claimed level, and workflow-level evidence
   is immutable;
6. those matching passing records collectively cover every native product the
   capability advertises; and
7. an advertised result schema is installed and understood by the consumer.

A failed conformance case is useful regression evidence but cannot support a
maturity claim. A successful structural fixture does not justify
`workflow-validated`. In the current explicit runtime, conformance records are
self-declared manifest metadata: their schema, internal references, claimed
level, URI shape, and digest shape are checked, but the URI is not fetched and
the declared evidence bytes are not independently rehashed. Installation and
review policy must establish that trust separately.

### Native versions

EDA version strings are not consistently semantic versions. Native products
therefore declare either `probe-and-record`, with the versions exercised by
the contributor, or `pinned-only`, where invocation is limited to the listed
observations. The concrete native version remains runtime evidence; a tested
version list is not a guarantee that an installation has that version.

## Transport bindings

The semantic request is transport-neutral. The v0alpha1 manifest advertises
the three bindings defined below. It does **not** define a normative MCP
binding, MCP tool names, artifact-transfer behavior, or authentication
semantics. Writing `mcp` into a generic `protocol` string may document an
experimental adapter, but it does not create a portable OpenADA/MCP contract.

A future MCP adapter should carry the same versioned request and result as a
local or remote invocation. A thin driver may instead call low-level native MCP
tools, but it still owns parameter validation, exact semantic mapping, evidence
bounds, and conformance. Endpoint discovery, credentials, sessions, and
artifact transfer remain transport concerns. See
[Providers, marketplaces, and MCP](PROVIDERS_AND_MCP.md).

A future marketplace likewise catalogs installed-or-approvable providers of
exact capabilities with immutable conformance records. The v0alpha1 manifest
groups multiple feature IDs under one operation-level maturity and supplies no
independent `capability_id`, so it cannot honestly encode per-feature maturity
rows. That needs an additive manifest revision. A marketplace is not runtime
approval, a raw executable list, or evidence that a provider is available. The
host must still validate, install/approve, resolve, invoke, and record the
selected provider deterministically.

### Local CLI

A `local-cli` transport declares a literal argv prefix. The host executes that
vector without a shell, writes exactly one UTF-8 JSON request to stdin, and
reads exactly one UTF-8 JSON result from stdout for `wait` mode. Native output
must be captured into the result or retained artifacts rather than mixed with
the protocol stream. The implemented runtime requires a zero transport-process
exit and empty stderr even when stdout contains valid JSON. It bounds all three
streams and time, uses a fresh process group, and terminates that group on
timeout, overflow, and after the provider parent exits. Only descendants that
remain in the fresh group are killed; a process that deliberately detaches from
it can escape this containment. This lifecycle hygiene is not a sandbox.
Bare entrypoints resolve from the authorized working directory's `bin`, the
current Python installation's scripts directory, and fixed system paths; caller
`PATH` is not consulted. The provider process receives a closed environment
with fixed locale, timezone, and `/usr/bin:/bin` search path, Python user-site
and bytecode writes disabled, and private home/temp directories. Ambient
Python, loader, PDK, and tool variables do not cross the transport boundary.
Provider-specific configuration must be carried by the request and explicitly
reintroduced by the selected provider for its native child.

After parsing, the runtime validates the generic result, installed profile data
schema, assertion truth table, evidence roles and byte ceilings, and
operation/profile/provider correlation fields. It verifies every recorded local
input and artifact as a regular file against the declared size and SHA-256 under
aggregate limits. For conclusive circuit-simulation results, canonical absolute
filesystem target and configuration locators must also bind exactly one result
input record, including any digest supplied by the request. The normalized
analysis type must equal the request, and the result must retain a native tool
identity, nonempty native command, and native exit code; engineering `pass`
requires that native exit code to be zero. Transport-process exit zero is a
separate protocol requirement. After provider completion the host reopens every
request input and requires filesystem identity, size, and SHA-256 to match the
pre-launch snapshot. Each provider-retained input record must also match that
snapshot. Mutation, replacement, or disappearance invalidates the evidence.

### Session API

A `session-api` transport invokes a named method in an already established EDA
session. `eda-session` locators are opaque to OpenADA and meaningful only to a
compatible driver. Session state, locks, active library context, and any
unbounded ambient configuration must be reported as provenance limitations;
the presence of a session does not make its state reproducible.

### Remote job

A `remote-job` transport sends the same request to a configured endpoint. A
workspace or artifact locator should be used instead of assuming the remote
worker can see the caller's local path. Authentication, secrets, queue policy,
job cancellation, and artifact transfer remain deployment concerns. The final
engineering result must still use an advertised OpenADA result schema and
retain the execution/evidence distinction.

## Result compatibility during the alpha bridge

The current `openada.result/v0alpha1` envelope predates request IDs, versioned
profile IDs, external driver identity, asynchronous jobs, and multi-step
backend execution. It remains useful and is explicitly advertised per
capability, but it cannot by itself enforce the complete round trip.

An alpha bridge returning `openada.result/v0alpha1` should echo correlation and
selection facts inside the operation-owned `data` object:

```json
{
  "protocol": {
    "request_id": "018f4f8c-6d3a-7b2e-8c41-9d2f7a6b5c10",
    "operation_profile": "openada.operation/circuit.simulate/v1alpha2",
    "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
    "driver_id": "org.example.openada.driver.example-spice",
    "driver_version": "0.1.0"
  }
}
```

That fragment is permitted by the existing open `data` object, but the base
result schema does not require or type it. The registered simulation profile
does require and type the fragment at its operation-data layer. Even there,
`request_id` is correlation rather than a canonical digest of the request
document. A future immutable result revision should promote these fields,
define complete request-content binding, and represent material backend steps
directly. Consumers must not infer them from prose, an executable path, or an
exhaustive native log.

## Extensions

Every extensible closed object has an `extensions` bag. Keys are reverse-DNS
namespaces and values are JSON objects. For example:

```json
{
  "org.example.xyce": {
    "runtime_profile": "local-mpi"
  }
}
```

Extensions may carry non-semantic routing or product metadata. They must not
change the meaning of an operation, assertion, status, artifact role, or
identity claim. If interoperability depends on an extension, its behavior must
be represented by a versioned operation/profile revision or advertised
feature ID and validated before dispatch. Unknown extensions may be preserved
or ignored; they must never silently broaden authority.

## Contributor gate

Before publishing a driver manifest:

1. replace every `example.org` ID and placeholder digest in the templates;
2. validate both schemas themselves with a Draft 2020-12 validator;
3. validate the request and manifest with format checking enabled;
4. independently check the manifest's reference rules listed above;
5. cover success, engineering failure, invalid request, unavailable tool,
   malformed evidence, timeout, and output-bound cases;
6. publish immutable conformance evidence and record its SHA-256;
7. claim workflow validation only for a pinned, independently checked public
   workflow.

With OpenADA installed, schema and example validation can be reproduced with:

```bash
python3 - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

root = Path.cwd()
pairs = (
    ("schemas/request-v0alpha1.schema.json", "conformance/driver-kit/request.template.json"),
    ("schemas/driver-manifest-v0alpha1.schema.json", "conformance/driver-kit/driver-manifest.template.json"),
)
for schema_name, example_name in pairs:
    schema = json.loads((root / schema_name).read_text())
    example = json.loads((root / example_name).read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(example)
    print(f"valid: {example_name}")
PY
```

Schema validation proves protocol structure, not that a tool, PDK, session,
runset, native report, or engineering conclusion is trustworthy.
