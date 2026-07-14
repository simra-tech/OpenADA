# Operation profile proposal: `<domain.operation>`

Use this RFC template before assigning a stable OpenADA operation or assertion
identifier. Delete instructional text when submitting a proposal. A proposal is
not a shipped profile until its schemas, fixtures, and compatibility status are
reviewed in a public revision.

## Identity

| Field | Proposed value |
|---|---|
| Operation ID | `org.example.openada.operation/domain.operation/v1alpha1` |
| Assertion ID | `org.example.openada.assertion/domain.assertion/v1alpha1` |
| Owner/maintainers | `<names or project>` |
| Maturity | `proposal` |
| Result schema | `openada.result/v0alpha1` or proposed successor |

State whether the IDs are project-namespaced incubating profiles or candidates
for the shared `openada.*` namespace.

## Purpose

In one paragraph, state the engineering action and the single conclusion an
agent needs for its next decision. Do not describe a particular executable or
GUI sequence as the operation.

## Non-goals

- `<conclusion this profile does not establish>`
- `<workflow or mutation intentionally outside the operation>`
- `<native detail that remains backend-specific>`

## Target and configuration

Define the accepted logical target kinds and locator types.

| Role | Required | Locator types | Meaning and identity requirement |
|---|---:|---|---|
| Primary target | yes | `filesystem`, `eda-session`, ... | `<exact object evaluated>` |
| `<pdk/model/deck/setup role>` | yes/no | `<types>` | `<why it is material>` |

State which project-specific inputs must always be explicit and which, if any,
may be deterministically resolved. Never permit a driver to substitute a PDK,
model, rule deck, setup, runset, top cell, or prior result merely to pass.
State how the base request's explicit `evidence_destination` is used, which
exact artifact paths the operation owns, and whether each collision policy is
supported. Evidence-only authority never permits mutation of the target.

## Request parameters

Provide a closed Draft 2020-12 JSON Schema for `request.parameters`. Include
units, bounds, defaults, and feature-gated fields. The base
`openada.request/v0alpha1` schema intentionally leaves this object open; a
request is dispatchable only after both schemas validate it.

```json
{
  "type": "object",
  "required": [],
  "properties": {},
  "additionalProperties": false
}
```

## Primary assertion

Write one declarative statement whose truth is bounded by the exact request.

> `<The requested target ... under the declared configuration.>`

### Truth table

| Engineering status | Required meaning | Minimum evidence |
|---|---|---|
| `pass` | `<positive assertion is supported>` | `<fresh native evidence and checks>` |
| `fail` | `<defined negative outcome is supported>` | `<trustworthy conclusive evidence>` |
| `unknown` | `<neither conclusion is justified>` | Missing, stale, malformed, conflicting, truncated, or unsupported evidence. |

State the allowed relationship to every execution status. Process completion
must not imply engineering `pass`, and process failure alone must not imply
engineering `fail`.

## Normalized facts

List only bounded facts needed for the next engineering decision. Native
reports, databases, waveforms, and logs remain retained evidence.

| JSON path | Type/unit | Required for | Meaning and bounds |
|---|---|---|---|
| `data.<field>` | `<type or quantity unit>` | `pass`, `fail`, ... | `<definition>` |

## Evidence and artifact roles

| Artifact role | Required for | Freshness/integrity rule | Native authority |
|---|---|---|---|
| `<evidence>` | `pass`/`fail` | `<absence-before-run, digest, snapshot, etc.>` | `<native format/tool>` |

Describe bounded-log policy, artifact-size policy, incomplete transitive or
ambient provenance, and cases where a native revision or retained snapshot is
more honest than a content digest.

## Diagnostics

Define stable machine codes and routing meaning. Explanatory prose is bounded
and non-normative.

| Code | Severity | Condition | Suggested next action |
|---|---|---|---|
| `<domain.reason>` | `info`/`warning`/`error` | `<condition>` | `<bounded action>` |

## Capabilities and feature IDs

List optional semantic features separately from backend flags. A driver must
decline an unsupported required feature rather than weaken the assertion.

| Feature ID | Request/result effect | Conformance case |
|---|---|---|
| `org.example.openada.feature/domain.feature/v1alpha1` | `<effect>` | `<fixture>` |

## Native mappings

Describe at least two plausible independent implementations before proposing a
shared core profile.

| Driver/backend | Native actions and artifacts | Material limitation |
|---|---|---|
| `<backend A>` | `<CLI/API/session sequence>` | `<difference retained below waist>` |
| `<backend B>` | `<CLI/API/session sequence>` | `<difference retained below waist>` |

If the mappings cannot support the same assertion, split the profile instead
of normalizing away the difference.

## Conformance matrix

| Case | Expected execution | Expected engineering | Required evidence/rejection |
|---|---|---|---|
| Valid positive | `completed` | `pass` | `<fixture>` |
| Valid negative | `completed` or justified incomplete state | `fail` | `<fixture>` |
| Invalid request | `invalid_request` | `unknown` | `<fixture>` |
| Tool unavailable | `not_available` | `unknown` | `<fixture>` |
| Timeout | `timed_out` | `unknown` unless conclusive evidence survives | `<fixture>` |
| Missing/stale output | `completed` or `failed` | `unknown` | `<fixture>` |
| Malformed/conflicting output | `completed` or `failed` | `unknown` | `<fixture>` |
| Bounds exceeded | `<defined>` | `unknown` | `<fixture>` |

Add operation-specific adversarial cases. Workflow validation also requires a
pinned public design, native-product/runtime identities, retained artifacts,
and an independent verifier.

## Mutation and side effects

Declare the maximum side-effect mode. For a design-write operation, complete a
separate mutation profile covering preview, exact base revision, scoped apply
authorization, transaction disposition, postconditions, append-only receipt,
idempotent retry, and evidenced revert. Do not add an informal `apply` field to
a read/evidence profile.

## Compatibility

List every field and truth-table rule that is normative. Any later change to
the assertion, status threshold, required fact, artifact role, authority, or
closed request shape requires a new profile ID. Note proposed migration from
any shipped short CLI command.

## Open questions

- `<unresolved semantic or conformance issue>`
