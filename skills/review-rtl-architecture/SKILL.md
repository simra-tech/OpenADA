---
name: review-rtl-architecture
description: Review ASIC RTL architecture with backend-independent OpenADA lint and structural evidence. Use for SystemVerilog or Verilog design reviews involving hierarchy, clocks and resets, state/control structure, parameterization, inferred latches or combinational loops, arithmetic width, signedness, overflow or saturation diagnostics, interface protocols, or deciding whether RTL is ready for synthesis without treating a clean lint run as functional verification.
---

# Review RTL Architecture

Review the implemented RTL as a senior design reviewer, but keep every
conclusion inside the available evidence. Use `$openada:openada` for execution
and normalized results. Never teach this workflow a native tool command, parse
a native log into a second result, or rename a missing check as passed.

## Define the decision

State the application, design phase, top module, and immediate decision. Ask
whether the review is intended to establish:

- the declared source configuration and successful elaboration;
- strict diagnostic cleanliness;
- a plausible architecture for the declared throughput, latency, area, power,
  clock, reset, and test goals; or
- readiness for a separate synthesis, timing, CDC/RDC, formal, simulation, or
  implementation workflow.

These are different claims. RTL lint does not prove function, CDC safety,
reset release safety, timing, power, equivalence, or specification
satisfaction.

## Freeze the source configuration

Record one review manifest before invoking an operation:

- repository revision and exact ordered source files;
- top module, SystemVerilog revision, include directories, defines, and
  generated-source identity;
- declared clocks, clock relationships, reset domains and polarities, intended
  operating modes, and interface protocols;
- authoritative architecture/specification references and unresolved
  assumptions;
- selected operation/feature, implementation, native product/version,
  evidence directory, result-envelope path, artifact paths, byte counts, and
  hashes.

The current result envelope has no general request or result correlation field;
do not invent one. Bind reviews with the repository revision, exact file and
artifact hashes, operation profile, tool identity, and evidence path instead.

Do not guess the top, file order, defines, clock intent, reset intent, or a
black-box model. The v1alpha1 lint surface has no module-parameter override; if
the intended configuration requires one, report that configuration as not
evaluated instead of silently using defaults. If any supported choice can
change elaboration, stop and ask one narrow question. Treat source and
PDK/library trees as read-only and use a fresh task-local evidence directory.

## Inspect capabilities before execution

Require the typed lint contract:

- `openada.operation/rtl.lint/v1alpha1`
- `openada.assertion/rtl.lint.clean/v1alpha1`
- `openada.feature/rtl.lint.systemverilog/v1alpha1`

Inspect its installed closed request and result schemas:

```bash
openada profile show openada.operation/rtl.lint/v1alpha1
```

Run scoped preflight only for the smallest next assertion:

```bash
openada doctor --project-root /absolute/project --assertion rtl-lint-clean
```

Preflight proves only point-in-time tool and project-root readiness. It does
not read the RTL or evaluate lint. If the operation, feature, implementation,
or executable is unavailable, report **not evaluated — capability
unavailable**. Do not fall through to a raw backend command.

## Establish strict lint evidence

Invoke only the semantic command, using the exact frozen file order and
configuration:

```bash
openada rtl-lint rtl/pkg.sv rtl/top.sv \
  --top top \
  --language 1800-2017 \
  --include-dir rtl/include \
  --define ASIC \
  --output-dir evidence/rtl-lint
```

Supply repeatable include and define options only when they are present in the
frozen manifest. The v1alpha1 warning policy is strict: any normalized warning
or error makes the engineering assertion fail. Do not suppress a warning to
manufacture a pass.

Keep design RTL distinct from PDK, macro, generated black-box, and simulator
library sources. The current v1alpha1 request has no separate library-source or
waiver-provenance role, so warnings originating only in those support models
still make OpenADA's strict assertion fail. Preserve that result as a harness
coverage gap and correlate it with the exact synthesis-flow lint configuration;
do not relabel it as a clean design pass or add broad source suppressions. A
future semantic extension should bind library sources and reviewed warning
policy separately while retaining both original diagnostics and file hashes.

Treat `1800-2017` or `1800-2023` as the requested frontend language revision,
not as proof that the design or implementation has been certified conformant
to the corresponding IEEE 1800 standard.

Read `execution.status` separately from `engineering.status`. Confirm the
operation and assertion profile IDs in `data.protocol`, ordered sources,
dependency records, unresolved-literal-include disclosure, language, top,
strict policy, diagnostic counts and bounded records, native implementation
identity, retained transcript, and all input/artifact hashes before accepting
the result.

`data.include_dependencies` is conservative capture, not proof of the exact
conditional-preprocessor path. Files below an include root may be retained even
when inactive, and `data.unresolved_literal_includes` may include tokens from
inactive branches. Report those normalized facts exactly. Establish whether a
particular branch is active only by reviewing the source and declared defines,
and label that conclusion **source inspection**, not OpenADA-normalized lint
evidence.

Route the result as follows:

| Result | Meaning | Action |
|---|---|---|
| engineering `pass` | Complete diagnostic evidence contains no warning or error under the strict policy | Continue the architecture review; do not claim function |
| engineering `fail` | Complete evidence contains one or more normalized warnings or errors | Classify every diagnostic; repair root causes and rerun the same manifest |
| engineering `unknown` | Evidence, provenance, execution, capture, or diagnostic interpretation is incomplete | Stop all cleanliness and readiness claims; repair the cited gap |
| invalid request | The source/configuration request is malformed or unsafe | Correct the manifest without changing design intent |
| unavailable/timeout/invocation failure | No RTL assertion was evaluated | Restore the same capability and rerun; do not substitute a tool |

## Establish elaborated structure separately

When successful elaboration and structural checks are required, preflight and
invoke the existing OpenADA surface as a distinct assertion:

```bash
openada doctor --project-root /absolute/project \
  --assertion rtl-structural-check-passes
openada rtl-check rtl/pkg.sv rtl/top.sv \
  --top top --output-dir evidence/rtl-structure
```

Keep this result separate from lint. A structural pass establishes only the
defined elaboration and structural checks for the exact source configuration.
Use this surface only when its advertised arguments can express that
configuration; otherwise keep the structural row not evaluated instead of
dropping includes, defines, generated sources, or a required parameter
override.

An engineering `unknown` stops hierarchy and readiness conclusions. Preserve
the generated structural artifact and hashes rather than reconstructing
hierarchy from log text.

## Perform the architecture review

Review authoritative RTL and normalized evidence together, while preserving
their different authority. Label contract fields and hashed artifacts
**normalized evidence**; label findings obtained by reading RTL, intent
documents, or generated sources **source inspection**; mark hypotheses
**inferred** and uncovered checks **not evaluated**. Examine:

- ownership and relationships of clocks, asynchronous inputs, resets, enables,
  and power/test modes;
- reset assertion/deassertion strategy and state initialized by reset versus
  configuration or protocol;
- state machines, arbitration, backpressure, outstanding transactions, buffer
  depth, and progress/deadlock assumptions;
- combinational depth and feedback, implicit storage, priority structure,
  signedness, truncation/extension, out-of-range indexing, and parameter edge
  cases;
- intended register, latch, memory, clock-gating, arithmetic, and mux
  structures versus what the source actually describes;
- hierarchy boundaries, reusable interfaces, generated variants, and whether
  the frozen top represents the intended ASIC configuration.

Do not call visual inspection a CDC/RDC, formal, simulation, synthesis, DFT,
security, or power-domain result. Record these as open coverage rows when no
installed semantic primitive evaluates them. Route hardware-inference and
mapping questions to `$openada:assess-synthesis-and-inference`; route
constraint-bound delay questions to `$openada:assess-asic-timing` only after a
valid mapped netlist exists.

When functional simulation is supplied as adjacent evidence, audit what each
test actually proves before citing its pass count. A test that only completes a
command, logs an expected value, catches a missing internal signal, or replaces
an unread value with zero is execution coverage, not a checked functional
result. Prefer assertions on the package-facing interface and compare observed
data with an independent reference model. Read only status bits defined by the
interface contract; unresolved unused pad bits must not contaminate a defined
status slice, but they must remain visible as separate unknown coverage. Make
reset release and the first command distinct clock events so simulator
scheduling cannot silently drop the first transaction. For multi-byte or
multi-cycle protocols, assert the documented transfer length, address meaning,
byte ordering, and final numerical result. If strengthening these checks turns
a prior pass into a failure, supersede downstream synthesis/physical evidence
that was built from the failing RTL rather than grandfathering it. A lint pass
is not functional evidence.

For arithmetic checks, freeze the operand-width contract before constructing
the harness: signedness, declared and intermediate widths, legal and reachable
operand ranges, and the intended truncation, rounding, overflow, or saturation
behavior. Build the independent oracle from explicitly widened signed operands
and intermediates; never let implicit expression sizing, an unsized literal, or
a DUT helper function define the expected value. Drive the threshold boundaries
directly, the reachable extrema, and deterministic nominal and mixed-sign
vectors. Classify a vector outside the frozen reachable range as an illegal
harness input, not a design defect, unless the implemented interface can admit
it.

Make the harness fail closed. Every mismatch must produce a nonzero process exit
and engineering `fail`; zero executed checks, unknown observed values, timeout,
compile or invocation failure, or incomplete capture must produce engineering
`unknown`, never `pass`. Retain the exact RTL, harness, vector/reference, and
tool identities plus hashes of both the failing transcript and the passing
transcript after repair. Keep native adjacent evidence labeled as such when no
OpenADA semantic operation produced its engineering status.

## Stop boundaries

Stop before an RTL-readiness conclusion when the declared source set,
top/configuration, unresolved-include interpretation, provenance, or either
required evidence result is unknown. Stop before a functional conclusion even
when lint and structural checks pass. Stop before modifying RTL unless the user
separately authorizes a design change.

For each proposed change, state one hypothesis and the expected observable
effect. Change one cause at a time, preserve the source diff, create a new
manifest/evidence directory, and rerun the same required assertions. Never
compare two reviews when ordered sources, captured dependency hashes, top,
defines, language, or intent changed without labeling the comparison
non-equivalent.

## Report

Return the frozen source/configuration identity; execution and engineering
statuses; operation, assertion, feature, implementation and tool version;
diagnostic and structural evidence with artifact hashes; architecture findings
classified as normalized evidence/source inspection/inferred/not evaluated;
the conservative include-capture limitation; uncovered verification domains;
and one smallest next action that can change the decision. Finish with
`signoff: not claimed`.
