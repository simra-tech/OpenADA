# Mutation and design versioning

OpenADA's first operations observe or execute native EDA tools without changing
the authoritative design. Mutation is a separate contract tier because a safe
write needs more than a tool command that returned zero.

The goal is not to force Git onto opaque native databases, binary layouts, or
generated project stores. The goal is to give every agent-authored change a
reviewable, append-only engineering history:

```text
intent
  -> inspected base revision
  -> bounded change plan
  -> explicit authorization
  -> native EDA mutation
  -> post-change inspection and engineering checks
  -> new revision plus an auditable change receipt
```

Native databases and files remain authoritative. Git may store text-native
sources, manifests, semantic snapshots, and change receipts; large or opaque
native artifacts can remain in an artifact store referenced by digest or native
revision identity.

## Why mutation is a separate tier

Read and verification operations can fail closed without modifying the design.
A mutation can be partially applied, race another editor, invalidate a related
view, or succeed syntactically while violating the requested intent. Drivers
therefore must not implement mutation as an optional `apply` flag on an
otherwise underspecified command.

Mutation profiles add five concerns to the normal OpenADA request/result model:

1. **Revision identity**: exactly which observed state the plan was based on.
2. **Authorization**: exactly which targets and change classes may be written.
3. **Transaction disposition**: whether nothing was written, the change was
   applied, it was rolled back, or the final state is unknown.
4. **Postconditions**: evidence that the resulting native design expresses the
   requested change and still satisfies the declared checks.
5. **Idempotency**: a retried authorized request cannot silently apply the same
   change twice.

Execution status, engineering status, and transaction disposition are
orthogonal. A native write process may complete while its postcondition fails;
an attempted rollback may also leave the final state unknown.

## Lifecycle

### 1. Inspect the base

Create a bounded structural observation of the exact target and record a
revision reference. A revision reference declares its identity strength:

- `content_digest`: the complete authoritative artifact is content-addressed;
- `native_revision`: the EDA or project store supplied a stable revision;
- `snapshot`: a retained native snapshot and structural observation identify
  the state;
- `observed`: only a bounded observation is available.

Drivers must not imply content-addressed reproducibility when a native database
cannot be hashed completely.

### 2. Plan without writing

Planning is the default. It produces a content-addressed change plan containing:

- the human or agent intent;
- target and base revision;
- typed domain edits;
- affected objects and views;
- required locks, runtime resources, or writable copies;
- expected postconditions and verification operations;
- rollback capability and retained-state requirements;
- a bounded native preview when the EDA supports one.

A plan is evidence, not permission. Its engineering assertion is only that the
requested edit was resolved into a valid, reviewable plan against the stated
base.

### 3. Apply with optimistic concurrency

Apply requires an explicit authorization token or policy decision, the exact
plan digest, the expected base revision, and a caller-generated request ID used
as an idempotency key. Before the first write, the driver must re-observe the
target and reject drift. It must also reject targets outside the authorized
scope. Replaying a completed request ID must return its existing receipt rather
than write again; if the driver cannot determine whether an earlier attempt
wrote, transaction disposition is `unknown` and automatic retry is forbidden.

Drivers should prefer a disposable branch, clone, or task-local project copy.
Writing a shared source workspace requires the driver to advertise appropriate
locking and conflict-detection capabilities. OpenADA must never silently widen
authorization from one cell/view or file to a whole library or project.

### 4. Prove the result

After a native write, the driver captures the result revision and evaluates the
declared postconditions from fresh evidence. Examples include:

- the requested instance, net, parameter, constraint, or geometry is present;
- affected views can be opened or parsed;
- check-and-save or structural validation succeeds;
- a regenerated netlist reflects the requested connectivity;
- selected DRC, LVS, simulation, or RTL checks still have trustworthy evidence.

Only declared postconditions are implied. A schematic edit followed by a clean
structural check does not imply acceptable analog performance.

### 5. Record an append-only receipt

Every attempt creates a receipt, including rejected, failed, rolled-back, and
unknown attempts. A receipt links:

```text
base revision
  -> change plan
  -> native execution evidence
  -> result revision
  -> semantic and native diffs
  -> postcondition results
```

The receipt records the actor/harness identity supplied by the caller, driver
and native-tool versions, timestamps, authorization scope, diagnostics, and all
retained artifacts. It never stores hidden model reasoning as engineering
evidence.

### 6. Revert by creating another change

History is append-only. Revert or restore creates a new plan and receipt that
references the earlier change; it never deletes or rewrites the original
record. A whole-snapshot restore must not be described as inverse semantic edit
replay. The driver must back up the current destination before replacing it and
prove the post-restore state independently.

## Proposed operation profiles

Mutation is initially a cross-cutting lifecycle, not a promise that arbitrary
design edits share one universal payload.

| Profile | Primary assertion |
|---|---|
| `change.plan` | A typed, bounded change plan was resolved against the identified base revision without writing. |
| `change.apply` | The authorized plan was applied to the expected base and every declared postcondition was evaluated. |
| `change.revert` | The referenced prior state was restored as a new revision and declared restore postconditions were evaluated. |
| `change.inspect` | The requested change receipt, lineage, and retained evidence were resolved completely. |

Domain profiles define the edit payload, for example
`schematic.edit.instance`, `schematic.edit.connectivity`,
`design-parameter.edit`, `constraint.edit`, or `layout.edit.geometry`. They use
the same lifecycle and receipt model but may require different native proofs.
OpenADA should standardize a domain edit only after its target identity,
preconditions, postconditions, and failure modes are portable across at least
two plausible drivers.

## Transaction disposition

A future mutation result envelope needs a closed disposition independent of
execution and engineering status:

| Disposition | Meaning |
|---|---|
| `not_applied` | No authorized write began. This includes previews and rejected stale-base requests. |
| `applied` | The driver proved the requested native write reached the result revision. |
| `rolled_back` | A write began, a rollback completed, and the restored state was independently observed. |
| `partial` | Some declared writes occurred but the complete plan was not applied. |
| `unknown` | The driver cannot establish the authoritative final state. Human isolation and review are required. |

`applied` does not itself mean engineering `pass`: postconditions may provide
valid evidence of a bad resulting design. Conversely, the driver must not emit
`rolled_back` merely because it invoked a rollback command.

## Change sets and design version control

The portable history object is a semantic change receipt plus artifact lineage,
not a universal circuit file. This enables useful version-control behavior even
when the native database is opaque:

- browse who or what changed a design target and why;
- compare bounded schematic, layout, constraint, or setup observations;
- reproduce the native operation when retained inputs permit it;
- connect a change to the DRC, LVS, simulation, or synthesis evidence it caused;
- detect stale-base conflicts before a write;
- restore a retained snapshot without pretending the restore is a source-level
  merge;
- review an agent's proposed edit before authorizing it.

Automatic merge is deliberately deferred. A structural diff can identify a
conflict but does not prove electrical or physical equivalence. Early versions
should surface divergent changes for review rather than invent a merge.

## Driver capability requirements

A mutation-capable driver must declare, per domain profile:

- preview support and whether the preview is native or synthesized;
- base and result identity strengths;
- atomicity boundary;
- locking and stale-base detection;
- supported authorization scope;
- snapshot and restore behavior;
- rollback evidence and limitations;
- semantic diff coverage;
- required postcondition operations;
- whether a shared live workspace can be used safely.
- idempotent retry and receipt-lookup guarantees.

Drivers that only offer best-effort writes may incubate behind an experimental
profile, but they must fail closed and may not claim conformance to the stable
mutation lifecycle.

## Initial proof

The first public mutation proof should be deliberately small:

1. use a text-native schematic or RTL fixture in a disposable copy;
2. inspect and bind the base by content digest;
3. preview one typed parameter or instance change;
4. apply only with the plan digest and expected base;
5. capture the semantic diff and result digest;
6. run the relevant existing OpenADA inspection/netlist/check operation;
7. revert as a new append-only change and independently prove restoration;
8. include stale-base, failed-postcondition, partial-write, and failed-rollback
   fixtures plus a duplicate-request replay before calling the profile
   structured.

Only after the text-native proof is independently checkable should the same
lifecycle expand to more complex native stores. Such a driver must still use
guarded writes, lock audits, post-change structural inspection, retained
snapshots where needed, and append-only rollback receipts. Opaque native bytes
may remain outside Git while their snapshot identities and semantic
observations are referenced by the OpenADA history.

## Non-goals for the first mutation release

- editing a user's authoritative source without explicit apply authorization;
- universal semantic merge across native EDA databases;
- claiming that a structural diff proves electrical equivalence;
- inverse-edit rollback when the driver only supports snapshot restoration;
- hiding locks, partial application, or uncertain final state behind a generic
  failure;
- treating a Git commit alone as proof that a native EDA database is valid.
