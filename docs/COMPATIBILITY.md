# Compatibility policy

OpenADA has five version axes. They answer different questions and must not be
treated as interchangeable:

1. The Python package and plugin use semantic versions such as `0.1.0`.
2. Request and result envelopes declare contract identifiers such as
   `openada.request/v0alpha1` and `openada.result/v0alpha1`.
3. Operation and assertion profiles carry their own complete versioned IDs.
4. A driver manifest has a schema version while each driver implementation has
   its own release version and native-product observations.
5. A reproducible workflow declares its own conformance manifest/run schema and
   pins external design, runtime, tool, and PDK identities.

The package version tells a user which implementation produced a result. The
envelope identifiers tell a consumer how to validate a transport object. The
profile IDs define the operation meaning and evidence threshold. The driver and
native-product identities say which implementation executed it. The conformance
identity tells a reviewer what exact engineering case ran.

The alpha [request and driver protocol](DRIVER_PROTOCOL.md) is currently
review-only scaffolding: the `0.1.0` CLI neither accepts request envelopes nor
discovers manifests. Publishing those schemas establishes their identity and
review surface without implying runtime support.

## Immutable protocol identifiers

Once any request, result, driver-manifest, operation, or assertion schema is
present in a reviewed public revision, its identifier and schema artifact are
immutable. Fixing prose outside the schema is allowed. Changing required
fields, types, enums, authority semantics, assertion thresholds, or closed
object shapes requires a new identifier and a new file.

Vendor extensions are namespaced and cannot change the core operation,
assertion, status, artifact role, or authority meaning. If interoperability
depends on an extension, that behavior needs a versioned profile or advertised
feature ID.

## Immutable result identifiers

Once a result schema is present in a reviewed public revision, its identifier
and schema file are immutable. Fixing a typo in prose is allowed; changing a
required field, type, enum, status meaning, or closed-object shape requires a
new schema identifier and a new schema file. OpenADA will not silently rewrite
`result-v0alpha1.schema.json` after the preview candidate is frozen.

The v0alpha1 envelope is deliberately closed with `additionalProperties:
false`. Consumers should validate the declared schema exactly and reject an
unknown identifier rather than guessing. Producers must emit one complete
schema-valid object, including for invalid requests and internal failures.

For v0alpha1:

- a `completed` execution has an integer native/static exit code;
- `completed` does not imply engineering `pass`;
- an incomplete execution normally produces engineering `unknown`, but may
  preserve `fail` when independently reviewable native evidence is conclusive;
- recorded existing files include byte count and SHA-256, while absent files do
  not claim either value;
- diagnostic messages, hints, execution errors, and engineering summaries are
  explanatory text bounded to 4,000 characters, not stable machine interfaces.

## Changes that keep the same result identifier

The following can be compatible when they preserve all documented semantics:

- a new operation name;
- additional keys inside the operation-owned open `data` object;
- new diagnostic codes, artifacts, or input records;
- support for another native tool version or runtime profile;
- bug fixes that make an implementation conform to the existing schema and
  status definitions;
- documentation and bounded-message wording changes.

Consumers must route on the schema, operation, status fields, diagnostic codes,
artifact `kind`/`role`, and explicit operation data—not prose or array order.

## Changes that require a new result identifier

Examples include:

- adding, removing, or renaming a top-level or closed-object property;
- adding or removing an execution or engineering enum value;
- changing a field type, requiredness, or nullability;
- changing the meaning of a status, artifact role, or existing operation field;
- weakening an integrity or provenance guarantee represented by the schema;
- changing the relationship between process and engineering status.

A new identifier must ship beside the old schema artifact, document migration,
and add tests proving both the new schema and the intended rejection behavior.
During the `0.x` preview OpenADA may switch the single schema emitted by a new
package release after documenting the change. Long-term support for older
schemas is not promised before 1.0, but their published schema files and
meanings remain immutable for evidence already captured.

## Package and CLI compatibility during 0.x

Patch releases preserve the active result identifier and CLI argument meanings.
They may correct native-output interpretation when the previous behavior
violated the documented contract; such corrections must include a regression
test and release note.

The explicit ngspice execution/output policy is such a preview correction.
Earlier `simulate` documentation implied that `.measure` normalization was
available with the streaming `ngspice -b -r` command, but ngspice suppresses
dot-card measurements for that combination while still returning success.
`batch` remains the default and retains its wrapper-raw meaning; callers now
select additive `control` mode for `.measure`, `.control`, explicit init
files, and declared deck-owned outputs. A suppressed or missing requested
measurement can no longer produce engineering `pass`.

Control mode also exposes additive `--system-init-file` startup provenance.
The option pins ngspice's standard `spinit`; `--init-file` continues to mean a
project/PDK init and `-n` is documented narrowly as disabling local/user
`.spiceinit`. Batch decks with unenumerated `.include`/`.inc`/`.lib` directives
are rejected in this preview because included control blocks execute under
`-b`; callers migrate those decks to control mode or a reviewed flattened deck.

The explicit KLayout report policy is another preview correction within the
same closed `openada.result/v0alpha1` envelope. Earlier implementations could
accept an already-existing final report, locate a different temporary report,
read through symbolic or hard links, omit the native child `generator`, and
count each LYRDB item once even when its multiplicity was greater than one.
Those behaviors contradicted the documented requirement for current-run native
evidence. The corrected driver binds one exact report before launch, requires
the report and its OpenADA transcript to be absent, captures them through an
anchored real parent directory, validates the native generator and optional top
cell, and uses multiplicity-weighted violation counts. Existing automation must
choose a fresh report basename for every run and must not pre-create either
output file. Script-owned `--expect-report`, explicit `--waiver-file`, deck
variables, and declared provenance inputs are additive options. New structured
details remain inside the operation-owned open `data` object; consumers that
need to distinguish historical preview behavior should also inspect
`openada_version`.

The explicit Netgen evidence policy is likewise a preview correction within the
same envelope. Earlier implementations ran Netgen against a temporary report,
moved that file into place, and could treat a zero-exit match-shaped report as
sufficient. Netgen setup Tcl errors may be caught and ignored by the native
batch command while comparison continues, so that policy could expose a false
engineering pass. The corrected connector gives Netgen the caller's exact
fresh final report path and `-json`, requires the derived native JSON and an
OpenADA bounded transcript, checks for a clean setup read and completion with
stderr either empty or limited to the exact reviewed
`Unable to permute model <token> pins <token>, <token>.` warning grammar, and
accepts `pass` or `fail` only when the report and JSON outcomes and structural
device/net totals agree. Accepted warning lines remain explicit through
`netgen.stderr_reviewed_warning`; any other stderr still produces `unknown`.

Existing LVS automation must choose a report filename with a suffix and a fresh
basename for every run. It must not pre-create the report, the `.json` path
formed by replacing the report's final suffix, or the sibling
`<report>.openada.log`. Missing, linked, stale, malformed, conflicting, or
truncated evidence now yields engineering `unknown`. Repeatable
`--provenance-input` is additive and should declare known setup/PDK dependencies;
each declared input is currently limited to 512 MiB by the preview stability
checker. Larger inputs that the older connector would launch are rejected
before execution and require a future bounded-large-input contract. The
connector still warns that executable Tcl can read unenumerated transitive
files and ambient state. The additional artifact records and normalized capture
details remain within the existing open artifact array and operation-owned
`data` object. Consumers that must distinguish historical behavior should
inspect `openada_version`.

Scoped `doctor --project-root ROOT --assertion ASSERTION` is an additive
preview interface. It adds only operation-owned keys under the open `data`
object and leaves legacy `doctor --tool/--require` behavior available. Its
fixed assertion IDs select one existing semantic operation and one tool; they
do not evaluate a design assertion or infer project collateral. Consumers
must check `data.preflight.assertion_evaluated`, the singular `target`, and the
PDK/startup enumeration flags rather than treating an empty `pdks` or
`selected_files` array as proof of absence. The version probe is also
fail-closed for missing, truncated, malformed, invalid-UTF-8, or
identity-changing output, correcting earlier preview behavior that could label
a zero-exit but versionless or wrong-product binary as available. The 30-second
version-probe ceiling applies only to scoped preflight; legacy doctor retains
its positive finite timeout behavior. Xschem is the sole reviewed nonzero
version-probe exception: its exact product/version grammar may exit 1 only with
empty stderr, and the accepted code remains explicit in scoped preflight data.

Minor `0.x` releases may add operations or introduce a new result identifier.
Removing a command, changing an option's meaning, or changing exit-code mapping
requires an explicit migration note. `--help` and `--version` remain
human-readable; every engineering command and malformed invocation emits one
JSON result.

OpenADA records native tool versions but does not claim every version behaves
identically. Driver maturity is evidence-scoped: **discovered**, **structured**,
or **workflow-validated**. Only the exact pinned public workflow justifies the
last label.

## Contributor gate

Before changing the contract or a structured driver:

1. classify the change against this policy;
2. validate representative results with the
   [driver conformance kit](../conformance/driver-kit/README.md);
3. add schema-valid success and failure cases plus a rejection regression;
4. preserve and independently check native artifacts for workflow claims;
5. update the contract, roadmap, and migration notes when compatibility changes.

The conformance helper validates structure and declared expectations. It does
not replace review of the native rule deck, model library, tool configuration,
or engineering evidence.
