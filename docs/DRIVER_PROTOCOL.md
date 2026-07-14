# OpenADA request and driver protocol

OpenADA's driver protocol is the machine-readable boundary between a stable
engineering intent and one deterministic native implementation. It lets an
agent select a capable driver without turning the public ontology into a copy
of ngspice, Xyce, Xschem, KLayout, Netgen, Yosys, OpenROAD, or another tool's
command surface.

This document specifies protocol scaffolding for review. The current `0.1.0`
CLI still accepts operation-specific flags and has in-tree drivers; it does not
yet discover external manifests or accept `openada.request/v0alpha1` on stdin.
The shipped CLI and [result contract](CONTRACT.md) remain authoritative until
that wiring lands.

The published protocol documents are:

- [`openada.request/v0alpha1`](../schemas/request-v0alpha1.schema.json), a
  transport-neutral request for one operation and assertion;
- [`openada.driver-manifest/v0alpha1`](../schemas/driver-manifest-v0alpha1.schema.json),
  a driver's identity, transports, capabilities, and conformance evidence;
- [`openada.operation-profile/v0alpha1`](../schemas/operation-profile-v0alpha1.schema.json),
  the closed shape for operation meaning, truth tables, facts, evidence, and
  backend mappings; and
- [`circuit.simulate/v1alpha1`](../profiles/circuit.simulate-v1alpha1.json), the
  first concrete typed operation profile.

Complete valid examples that are safe to copy and replace live in the
[request template](../conformance/driver-kit/request.template.json) and
[driver-manifest template](../conformance/driver-kit/driver-manifest.template.json).
New shared semantics start from the
[operation-profile RFC template](../conformance/driver-kit/operation-profile.template.md).
The `example.org` identities and zero digests in those files are placeholders,
not OpenADA capability claims.

The first built-in alpha bridge maps
`openada.operation/circuit.simulate/v1alpha1` to ngspice or Xyce through the
same CLI shape:

```bash
openada simulate conformance/circuit-simulate/fixtures/rc-transient.cir \
  --backend ngspice --output-dir /tmp/ngspice-run
openada simulate conformance/circuit-simulate/fixtures/rc-transient.cir \
  --backend xyce --output-dir /tmp/xyce-run
```

When `--backend` is omitted, the legacy ngspice interface remains the default.
The shared subset is one self-contained transient analysis, with no includes,
measurements, or control-language blocks. ngspice has native workflow evidence; Xyce currently
has synthetic contract tests only because the development server has no native
Xyce installation. The generic request and external-manifest transport remain
review scaffolding rather than a runtime interface.

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
| Operation profile | `openada.operation/circuit.simulate/v1alpha1` | Tool-independent action and parameter meaning |
| Assertion profile | `openada.assertion/simulation.evidence.valid/v1alpha1` | Exact claim and `pass`/`fail`/`unknown` evidence rules |
| Driver | `org.example.openada.driver.example-spice` at `0.1.0` | One implementation release |
| Native product | `org.example.eda.example-spice` with an observed native version | Product actually used below the driver |
| Result schema | `openada.result/v0alpha1` | Shape and common status semantics of returned evidence |

Published schema and profile identifiers are immutable. A change to a required
field, status meaning, assertion threshold, normalized fact, or closed shape
requires a new identifier and a new file. An implementation fix or new native
version may instead require a new driver version and new conformance evidence.

The `circuit.simulate/v1alpha1` profile is the first published typed operation
profile. Its built-in CLI bridge records the profile and selected simulation
driver in the existing operation-owned result data; the base request envelope
still is not a general runtime dispatch interface.

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

Maturity belongs to a capability, not to a tool family or an entire EDA suite. A
driver can be workflow-validated for transient simulation and only structured
for noise analysis.

### Trust and installation

A manifest is not an installation instruction or a trust certificate. A host
must invoke only drivers installed or explicitly approved through its own trust
policy. Discovery must not download code, follow a manifest-provided package
URL, or execute an unreviewed binary automatically. For local CLI transports,
the host resolves the literal argv executable without a shell, applies its
normal executable ownership/path policy, and records the selected path and
observed identity in the result.

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
6. an advertised result schema is installed and understood by the consumer.

A failed conformance case is useful regression evidence but cannot support a
maturity claim. A successful structural fixture does not justify
`workflow-validated`.

### Native versions

EDA version strings are not consistently semantic versions. Native products
therefore declare either `probe-and-record`, with the versions exercised by
the contributor, or `pinned-only`, where invocation is limited to the listed
observations. The concrete native version remains runtime evidence; a tested
version list is not a guarantee that an installation has that version.

## Transport bindings

The semantic request is transport-neutral. The manifest advertises how a host
can deliver it:

### Local CLI

A `local-cli` transport declares a literal argv prefix. The host executes that
vector without a shell, writes exactly one UTF-8 JSON request to stdin, and
reads exactly one UTF-8 JSON result from stdout for `wait` mode. Native output
must be captured into the result or retained artifacts rather than mixed with
the protocol stream.

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
    "operation_profile": "openada.operation/circuit.simulate/v1alpha1",
    "assertion_profile": "openada.assertion/simulation.evidence.valid/v1alpha1",
    "driver_id": "org.example.openada.driver.example-spice",
    "driver_version": "0.1.0"
  }
}
```

That fragment is permitted by the existing open `data` object, but the base
result schema does not require or type it. A future immutable result revision
should promote these fields and represent material backend steps directly.
Consumers must not infer them from prose, an executable path, or an exhaustive
native log.

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

With the optional conformance dependency installed, schema and example
validation can be reproduced with:

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
