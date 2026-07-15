# OpenADA semantic model

OpenADA is the semantic ABI between an agent's engineering intent and
deterministic EDA execution: a versioned intent goes in, and bounded,
auditable engineering evidence comes out.

The ABI analogy is deliberate. An agent should not need to learn a different
public language for every simulator, schematic editor, layout system, or
verification engine. A driver translates a stable operation into the native
API, CLI, scripts, files, and environment of one backend. The backend continues
to operate on its native design data and remains authoritative for the
underlying analysis.

```text
agent or workflow
      |
      |  versioned operation + assertion + request
      v
OpenADA semantic boundary
      |
      |  deterministic driver implementation
      v
native EDA, native files, PDK, models, decks, and setup
      |
      |  reports, logs, databases, waveforms, and exit state
      v
OpenADA result + normalized facts + retained evidence + provenance
      |
      v
next agent decision or engineer review
```

OpenADA is therefore a **narrow waist**, not a universal circuit data model and
not a lowest-common-denominator copy of every vendor command. Design formats,
PDKs, model libraries, rule decks, runsets, and native reports stay below the
waist. Agent-facing intent, status, evidence, artifact roles, and provenance sit
at the waist.

Tool-independent engineering skills may sit above the waist. A skill can
compose operations, preserve a review discipline, and choose the next action;
it does not become an operation profile merely because it ships in the same
plugin. Skills do not define result fields, assertion truth tables, driver
capabilities, or conformance maturity. This separation lets community workflows
evolve without turning every engineering procedure into protocol surface. The
shipped analog characterization, stability, spectral-linearity, and PVT/yield
skills inspect capabilities and leave missing primitives not evaluated; they
remain experimental even after fresh-agent forward tests. See
[Engineering skills above OpenADA](ENGINEERING_SKILLS.md).

This document describes the intended semantic model. It includes both the
implemented preview foundation and proposed contract layers that do not exist
yet. The [contract](CONTRACT.md), published schemas, and current CLI remain the
source of truth for shipped behavior.

## Contract concepts

The public contract separates six concepts that are often collapsed in an EDA
wrapper.

| Concept | Question it answers | Contract responsibility |
|---|---|---|
| Operation profile | What engineering action is requested? | Defines a small, tool-independent intent and its versioned meaning. |
| Assertion profile | What single claim may this invocation establish? | Defines evidence requirements and exact `pass`, `fail`, and `unknown` semantics. |
| Request | What design, collateral, configuration, and outputs apply to this run? | Binds the operation to explicit targets without guessing project-specific inputs. |
| Driver and capabilities | Which implementation can carry out that operation here? | Advertises supported profile versions and translates them into deterministic native actions. |
| Result and evidence | What did execution do, and what conclusion does the evidence support? | Separates process state from engineering state and returns bounded normalized facts. |
| Artifacts and lineage | Which native objects support the conclusion, and where did they come from? | Records stable snapshots, hashes, roles, derivation, and reproducibility limits. |

These layers should be independently versioned. A package release, an
operation profile, a result envelope, and a conformance workflow answer
different compatibility questions and must not be treated as one version.

### Operation profile

An operation is a stable engineering verb such as netlisting a schematic,
running a circuit analysis, or comparing layout and schematic connectivity. It
is not an executable name, a GUI gesture, or a session-management primitive.

A versioned operation profile should define:

- the target object types and required request fields;
- one primary engineering assertion;
- the normalized facts an agent may consume;
- required native evidence and artifact roles;
- status rules, bounds, and relevant diagnostics;
- capability requirements and permitted extensions;
- explicit limitations and conclusions the operation cannot support.

The active published typed profiles are
`openada.operation/circuit.simulate/v1alpha2`,
`openada.operation/result.measure/v1alpha1`, and
`openada.operation/specification.evaluate/v1alpha1`. The existing result
envelope still emits short top-level operation names such as `simulate`, `drc`,
and `lvs`; typed bridges record full profile and implementation identity inside
operation-owned data.

`openada.operation-profile/v0alpha2` is additive and immutable beside
v0alpha1. The historical `circuit.simulate/v1alpha1` profile remains unchanged;
its additive v1alpha2 successor still uses the v0alpha1 profile schema. The
measurement and specification profiles use v0alpha2 so one deterministic
semantic kernel can bind feature IDs to versioned algorithms without inventing
multiple native EDA mappings.

One invocation should evaluate one primary assertion. Workflows may compose
several operations, but combining unrelated conclusions into one status makes
failure recovery and agent reasoning ambiguous.

### Assertion profile

The assertion is the precise claim whose truth is evaluated from evidence. It
prevents a broad verb such as "simulate" or "verify" from carrying more meaning
than the run established.

The preview already uses fixed assertion ideas including:

- `schematic-netlist-generated`;
- `spice-analysis-evidence-valid`;
- `drc-clean`;
- `lvs-match`;
- `rtl-structural-check-passes`.

The next contract should make assertion identity and its versioned evidence
rules part of every structured operation profile. Assertions must remain
bounded by the request. For example, `drc-clean` means clean under the exact
input layout, top cell, rule deck, variables, waiver state, and tool execution
recorded for that run. It does not mean that every foundry or signoff rule was
evaluated.

### Request

A request binds an operation to a real project context. The review-only base
request schema represents, where relevant:

- the logical target and native locator, such as project/library/cell/view,
  file, database object, or top module;
- analysis kind and parameters;
- PDK, model, corner, rule deck, setup, runset, and waiver inputs;
- execution timeout, completion, and side-effect policy;
- required output roles, evidence bounds, and an explicit evidence
  destination with collision semantics;
- an optional explicit driver selection;
- namespaced backend extensions that do not change the core assertion.

Requests must not hide project assumptions. A driver may validate or resolve an
explicit logical locator, but it must not silently substitute a convenient PDK,
model, deck, setup, top cell, or prior report to manufacture a result.

OpenADA publishes a review-only
[`openada.request/v0alpha1`](../schemas/request-v0alpha1.schema.json) base
envelope and [driver protocol](DRIVER_PROTOCOL.md). The current CLI does not
consume that envelope as a generic dispatch input; operation-specific arguments
perform this role for each built-in operation. Operation-specific CLI bridges
implement the three published typed profiles. The common simulation subset
covers one self-contained advertised OP, DC, AC, or transient analysis; the
evidence profiles consume closed JSON inputs. General runtime request dispatch
remains future work.

### Driver and capabilities

A driver is a deterministic implementation of one or more operation-profile
versions. It translates semantic requests into native actions and translates
native observations back into the shared evidence contract.

The review-only machine-readable capability manifest states at least:

- driver identity and version;
- supported operation and assertion profile versions;
- supported target locator types and analysis variants;
- native tool names and compatible version ranges;
- required collateral and runtime assumptions;
- evidence and provenance guarantees;
- maturity and the exact conformance cases supporting that maturity.

One driver may orchestrate several tools. A schematic-to-simulation driver,
for example, may use Xschem for netlisting and ngspice for analysis while
implementing one public `circuit.simulate` operation. Future result schemas
will need to preserve the identity of material backend steps rather than
pretending such a driver is one process.

Capability negotiation may select a compatible driver when the caller does not
name one. Selection must be deterministic and the result must disclose the
actual driver, tools, and versions. A driver may decline an unsupported request;
it must not weaken the assertion to make the request appear supported.

The preview has built-in tool discovery and built-in Python drivers. A
review-only
[`openada.driver-manifest/v0alpha1`](../schemas/driver-manifest-v0alpha1.schema.json)
schema now defines the intended capability surface, but the runtime does not
yet discover or invoke external manifests. V0alpha1 also has no independent
capability ID, per-feature maturity rows, or normative MCP transport binding;
those require an additive manifest revision.

MCP may belong below capability resolution as a future transport adapter,
alongside local CLI, session API, and remote jobs. Such an adapter must carry
unchanged operation/assertion meaning and evidence thresholds. A future
marketplace catalogs conforming providers of exact capabilities; it does not
convert low-level MCP tools or raw executables into semantic operations. See
[Providers, marketplaces, and MCP](PROVIDERS_AND_MCP.md).

### Result and evidence

The implemented [`openada.result/v0alpha1`](../schemas/result-v0alpha1.schema.json)
envelope establishes the most important invariant: execution state and
engineering state are different facts.

- `execution.status` says whether OpenADA could invoke and observe the native
  process.
- `engineering.status` says what the operation's validated evidence supports.
- `diagnostics` explains bounded, machine-routable failure or uncertainty.
- `data` contains operation-specific normalized facts.
- `inputs`, `artifacts`, and `provenance` retain the evidence trail.

Normalized JSON is a decision index, not a replacement for native evidence.
Large waveforms, reports, databases, and logs should remain retained artifacts;
the result should contain only the bounded facts required to select the next
engineering action.

The preview envelope is closed at the top level, but its `operation` value and
operation-owned `data` object are not validated by the common result schema
against individual profiles. Typed operations therefore publish separate
immutable profile artifacts and validate their own closed data without
silently changing the meaning of `v0alpha1`.

### Artifacts and lineage

The preview records declared input and output files with kind, role, path,
size, and SHA-256 when they exist. That makes a single invocation substantially
more auditable, but it is not yet a cross-run lineage graph.

The target lineage contract should identify relationships such as:

```text
schematic snapshot
  --produced-by--> netlist operation
  --derived-as---> netlist snapshot
  --consumed-by--> simulation operation
  --supports-----> simulation-evidence assertion
  --consumed-by--> measurement operation
  --supports-----> specification assertion
```

Each material artifact snapshot should have a stable identity, a semantic role,
the invocation that produced or consumed it, and known derivation edges. The
contract must also report incomplete provenance. A file hash cannot enumerate
ambient environment state, transitive Tcl or Ruby reads, a mutable design
database, or an unrecorded model include.

Native artifacts remain authoritative. Lineage tells an agent which evidence
supports which conclusion; it does not convert every EDA database into an
OpenADA-owned representation.

## Status semantics are assertion semantics

For an operation with a primary assertion, engineering status has the following
meaning:

- `pass`: all required evidence was present, trustworthy under the profile,
  and supports the assertion.
- `fail`: trustworthy evidence supports the defined negative outcome.
- `unknown`: the required evidence was absent, stale, malformed, inconsistent,
  truncated, or otherwise insufficient for either conclusion.

The existing envelope also has `not_applicable` for operations such as
discovery that do not evaluate a design assertion.

Execution completion does not select among these engineering statuses. A
zero-exit native process can still yield `fail` or `unknown`, and conclusive
native failure evidence can sometimes survive an incomplete process.

### Simulation evidence is not specification satisfaction

`circuit.simulate` should establish that the requested analysis produced valid,
interpretable evidence under the declared models, corner, startup policy, and
analysis configuration.

- `pass`: the analysis completed and every required output passed the profile's
  freshness and structural checks, with no conclusive convergence or native
  fatal condition.
- `fail`: trustworthy native evidence proves a defined simulation failure such
  as terminal non-convergence.
- `unknown`: the waveform is missing, stale, corrupt, ambiguous, or cannot be
  bound to the requested analysis.

None of those states says that gain, bandwidth, power, noise, or another design
specification was met. Measurement extraction and specification evaluation are
separate operations:

```text
circuit.simulate       -> valid analysis evidence
result.measure         -> values with units and extraction provenance
specification.evaluate -> pass/fail against explicit limits
```

This separation lets an agent rerun only the stage whose evidence changed and
prevents "the simulator exited successfully" from becoming "the design works."

The implemented `result.measure/v1alpha1` is deliberately narrower than a
native waveform reader. It consumes a bounded normalized real inline series
whose canonical digest binds axis, signals, and condition records. Its scalar
kinds are closed and unit checks are exact. Optional lineage to a native
artifact is explicitly unverified; OpenADA does not yet extract this series
from ngspice or Xyce raw evidence.

The implemented `specification.evaluate/v1alpha1` compares one typed finite
measurement with explicit lower/upper limits, inclusive flags, and exact
condition bindings. It performs no implicit conversion. Missing measurements,
unit mismatch, or unproven conditions are `unknown`; only valid matched
evidence outside a limit is specification `fail`.

### DRC and LVS have bounded conclusions

`layout.drc` evaluates cleanliness under one exact layout, top cell, deck,
binding set, waiver state, and native report. A clean result does not imply that
the deck was the foundry signoff deck, that every manufacturing requirement was
covered, or that the circuit performs correctly.

`layout.lvs` evaluates whether two declared representations match under one
exact setup and comparison policy. A match does not establish DRC cleanliness,
parasitic-aware performance, reliability, or suitability for tapeout.

DRC-clean and LVS-match should therefore remain separate assertions and
separate evidence records. A workflow may require both without inventing a
single vague `verification-pass` status.

## Bounded initial ontology

The first public ontology should stay small. It should cover common agent
decisions, not mirror every backend command. The following is a working target,
not a list of accepted profile identifiers or a claim of current CLI support.

| Domain | Candidate operation | Primary conclusion | Preview foundation |
|---|---|---|---|
| Control | `capabilities.inspect` | Which exact operations can run in this environment? | `doctor` provides built-in discovery and scoped preflight. |
| Inspection | `schematic.inspect` | What bounded hierarchy, instances, nets, pins, and parameters are observable? | Not yet implemented as a shared operation. |
| Generation | `schematic.netlist` | Was a resolved native netlist generated from the declared schematic? | `netlist` through Xschem. |
| Analysis | `circuit.simulate` | Was valid evidence produced for the requested circuit analysis? | `simulate` through the workflow-validated ngspice/Xyce shared alpha. |
| Evidence | `result.measure` | Were the requested values extracted with units and source provenance? | `measure` implements a closed scalar vocabulary over canonical-digest-bound normalized real inline series; native waveform extraction is not built in. |
| Evidence | `specification.evaluate` | Do declared measurements satisfy explicit limits? | `evaluate` implements exact-unit lower/upper bounds and explicit condition binding over one typed measurement. |
| Inspection | `layout.inspect` | What bounded cells, hierarchy, layers, geometry summaries, and connectivity are observable? | Not yet implemented as a shared operation. |
| Verification | `layout.drc` | Is the declared layout clean under the declared DRC setup? | `drc` through KLayout. |
| Verification | `layout.lvs` | Do the declared layout and schematic representations match under the declared LVS setup? | `lvs` through Netgen. |
| Extraction | `layout.pex` | Was a parasitic representation generated and bound to the declared source layout/setup? | Not yet implemented as a shared operation. |
| Digital | `rtl.check` | Did the declared RTL elaborate and pass the defined structural checks? | `rtl-check` through Yosys. |

Sweeps, corner matrices, Monte Carlo, optimization loops, schematic-to-layout
flows, and signoff reviews are workflows composed from these operations. They
may later receive versioned workflow contracts, but they should not force every
backend's orchestration commands into the initial operation ontology.

Likewise, opening an editor session, invoking a tool-specific script, parsing a
native waveform, polling a job, or recovering a native transaction may be
necessary driver primitives. They are not automatically public engineering
operations.

## Same intent, different backends

The clearest interoperability test is two independent drivers implementing the
same operation and assertion profile.

| Public intent | Possible native implementations | Shared result meaning |
|---|---|---|
| `circuit.simulate` | ngspice CLI; Xyce CLI | Declared analysis, convergence classification, evidence artifact roles, and recorded provenance with explicit completeness limits. |
| `schematic.inspect` | Xschem native files or headless queries; Qucs-S project queries | Bounded hierarchy, instances, nets, pins, parameters, unresolved references, and inspection limitations. |
| `layout.drc` | KLayout plus a caller-supplied Ruby deck; Magic plus an explicit rule setup | The same DRC assertion and status rules, while retaining each engine's native report and deck identity. |

The normalized facts must be genuinely equivalent at the assertion boundary;
OpenADA should not erase material backend differences. Backend-only controls can
remain namespaced request extensions and native evidence. If two tools cannot
support the same assertion, they should advertise different capabilities rather
than returning superficially similar JSON.

The first portability proof is `circuit.simulate`, mapped to ngspice and Xyce
against the same analysis semantics. Its common alpha subset is deliberately
narrow: one self-contained OP, DC, AC, or transient analysis, with no includes,
measurements, print directives, control-language blocks, FFT, noise, Monte
Carlo, or multiple analyses. ngspice is structured for OP/DC/AC and
workflow-validated for TRAN; Xyce is structured for DC/AC,
workflow-validated for TRAN, and rejects OP. The expanded independent verifier
parses native success evidence and permits backend-native sampling differences
where both mappings advertise an analysis; success-only cases do not establish
workflow-validated maturity.

## Tool-specific control surfaces belong below the waist

Tool CLIs, editor automation, and flow scripts provide native control and
connectivity. They do not need to become the public ontology.

```text
OpenADA operation
      |
OpenADA driver
      |
tool-specific action sequence
      |
native CLI, script, or editor API
      |
Xschem, ngspice, Xyce, KLayout, Netgen, Yosys, or OpenROAD
```

The driver may use many backend actions to implement one public operation.
Those actions can preserve native fidelity, safety checks, session recovery,
and project configuration without asking every agent to reason over the entire
backend command inventory. The same separation applies to flow tools such as
LibreLane and OpenROAD as well as single-purpose CLIs.

OpenADA should therefore standardize the meaning of the request and evidence,
not replace a mature native control layer. A driver is also the right place to
map native error categories, report formats, and database locators into the
shared assertion profile.

## Mutation is a separately gated contract tier

Design mutation is strategically important because many EDA databases do not
participate cleanly in text-oriented version control. It is also riskier than
the current inspection and execution operations and is deliberately outside
the preview contract.

Mutation must add explicit preview, apply authorization, base/result revision,
transaction disposition, and postcondition semantics. A successful native
write does not establish DRC, LVS, simulation, or specification success; those
remain linked but independent assertions. A stale base or unprovable final
state must fail closed rather than overwrite or bless the design.

The proposed lifecycle, change receipts, rollback rules, and initial proof are
specified in [Mutation and design versioning](MUTATION_AND_VERSIONING.md).

## Extension rule

New domains should extend the ontology only when the engineering assertion and
minimum evidence can be stated precisely. A new driver may implement an
existing profile where the semantics truly match or propose a namespaced
operation where they do not. It should not overload `circuit.simulate` with
incompatible state, result, or correctness meanings merely to appear
universal.

An operation earns a place in the shared ontology when at least two of the
following are true:

1. More than one backend can implement the same assertion.
2. More than one agent workflow needs the normalized result.
3. The result supports a stable next engineering decision.
4. Public fixtures can test pass, fail, and unknown behavior.

## Current foundation and target protocol

The distinction between shipped behavior and intended protocol is material.

| Area | Implemented preview | Target protocol |
|---|---|---|
| Result envelope | Closed `openada.result/v0alpha1` with execution/engineering separation, bounded diagnostics, artifact records, and provenance. | New immutable result version linked to typed operation and assertion profiles, with multi-step driver identity where needed. |
| Requests | Per-operation CLI arguments plus a review-only `openada.request/v0alpha1` base schema not consumed as a generic CLI request. | Runtime generic request dispatch over installed typed profiles. |
| Operations | Short operation names and an open operation-owned `data` object; simulation, measurement, and specification bridges record full profile identities there. | Independently versioned profiles for remaining operations and machine-readable profile discovery. |
| Drivers | Built-in discovery and statically integrated open-tool drivers plus a review-only manifest schema. | Runtime manifest discovery, deterministic selection, driver conformance, and independent installation. |
| Portability proof | `circuit.simulate` maps one alpha profile to ngspice OP/DC/AC/TRAN and Xyce DC/AC/TRAN, with pinned analysis-specific replay. | More operations, open-source backends, and runtime environments pass equivalent independently checked conformance. |
| Artifacts | Declared files have roles, paths, sizes, and hashes; several drivers enforce fresh evidence. | Cross-run invocation and derivation lineage, including explicit incomplete-provenance records. |
| Mutation | No general design-mutation or workspace-transaction contract. | Reviewable change sets, exact base/post identities, transaction semantics, conflicts, rollback, and linked validation evidence. |

Today, OpenADA is a credible implementation of the evidence boundary and a
reference for the broader semantic protocol. It can earn the role of a broadly
shared agent-facing contract only through published operation schemas,
independent drivers, same-intent cross-backend conformance, and adoption. The
architecture is designed for that direction; the repository must continue to
label the difference honestly.
