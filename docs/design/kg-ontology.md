# Analog-design knowledge graph ontology

Status: research-spike decision and prototype contract, not an OpenADA operation

Scope: TapeoutBench plugin-ladder rung 2; public PLL1/PLL3 practice knowledge

Schema candidate: `openada.eval/analog-knowledge-graph/v0`

Last evidence review: 2026-08-12

## Decision

Use a **labeled property graph serialized as closed, typed JSON** for the rung-2
prototype. Nodes give stable identities to design entities; directed, typed
edges carry the engineering assertion, its sign and strength when applicable,
the exact scope in which it was established, and at least one committed source
pointer. JSON Schema closes the document shape. A small semantic validator
closes graph-wide rules that JSON Schema cannot express, such as endpoint
existence, identifier uniqueness, and relation-specific endpoint kinds.

This is the closest fit to OpenADA's current contract style:

- the complete graph is a versioned JSON value that can be canonically encoded,
  content-hashed, retained with a task release, and compared byte-for-byte;
- query inputs and outputs can later use the same closed request/result envelope
  pattern as other OpenADA semantic operations;
- no graph database, ontology reasoner, network service, or runtime package is
  needed for the evaluation prototype; and
- property-bearing causal edges are first-class rather than reified indirectly.

The recommendation is deliberately a property graph, not merely arbitrary JSON.
Node and relation kinds have closed meanings, IDs are global within one graph,
and consumers must reject unknown fields and kinds. A schema version is
immutable once published; changing a required field, endpoint rule, sign
meaning, or query result meaning requires a new schema/profile ID.

### Alternatives considered

| Representation | Useful property | Why it is not the v0 choice |
|---|---|---|
| RDF/OWL | Global identifiers, mature ontology tooling, formal entailment | Evidence-bearing n-ary claims such as “joint width x2 caused −89 MHz at TT and ctrl=0.81 V” require reification or named graphs; closed-world rejection and deterministic portable query behavior need extra policy; the dependency and authoring burden is disproportionate for the first two task families. |
| JSON-LD over RDF | JSON transport with a future RDF bridge | It retains the RDF reification/open-world issues while making the v0 author and validator understand two data models. A later exporter can map stable v0 IDs without making JSON-LD normative now. |
| Relational tables | Strong constraints and familiar joins | Mechanism paths and evolving heterogeneous entity payloads become many join tables, and shipping one immutable task-local artifact is less natural. The prototype does not need concurrent mutation or transaction semantics. |
| Free-form documents plus embeddings | Low authoring friction | They cannot reject invented relation kinds, guarantee evidence on every assertion, or return reproducible multi-hop answers. Retrieval similarity is not graph reasoning. |

RDF export remains possible, but it is a view of the versioned JSON graph, not a
second source of truth. No inference rule is implicit merely because a future
export uses an ontology vocabulary.

## Boundary and claim model

The graph is design knowledge, not fresh simulator evidence. It records what a
committed public source says, how that statement joins the task contract, and
where it applies. A graph query can prioritize a lever or expose a dependency;
it cannot prove that a new design meets a specification. Simulation,
measurement, specification evaluation, and signoff remain separate contract
layers.

The seed uses TapeoutBench's word “measured” for its committed characterization
record. In this graph that means an observed, quantified simulator experiment
described by the public source; it does not mean silicon measurement.

Every edge has exactly one evidence record containing one or more source
pointers. A pointer identifies repository, immutable revision, path, and a
stable human-readable section or structured locator. Node descriptions may
summarize public facts, but no actionable assertion is accepted without an
edge-level pointer.

### Evidence grades

The grade is about the assertion on one edge, not the prestige of its source.

| Grade | Closed meaning | Allowed use |
|---|---|---|
| `measured` | The source reports an observed sweep, trial, or comparison under named conditions. | May carry numeric baseline, observation, delta, or ratio. Valid only inside its recorded design point and condition scope. |
| `derived` | The assertion is copied from a task/recipe contract, calculated from measured values, or is an explicitly stated causal interpretation. `basis` distinguishes `task_contract`, `recipe_contract`, `calculation`, and `causal_interpretation`. | May drive exact joins and deterministic path reasoning. It must not be presented as an independently varied measurement. |
| `textbook` | General physical prior not established for this task instance. | May suggest a hypothesis or next experiment. It ranks below task-specific evidence and cannot supply a task-specific magnitude. |

Task declarations therefore use grade `derived` with basis `task_contract` or
`recipe_contract`. This keeps the requested three-grade ladder closed without
mislabeling a specification limit as a physical measurement. The distinct
`basis` field is mandatory so consumers can tell algebra, causal interpretation,
and authoritative task declarations apart.

When an edge is supported by a measured observation but its causal attribution
is not an isolated intervention, the edge is `derived/causal_interpretation` and
its quantification retains the measured comparison. For example, the PLL3
cross-coupled NMOS and PMOS widths changed together. Each parameter-to-mechanism
edge records the other width in `co_varied_parameters`; neither becomes a
silently isolated sensitivity.

## Entity taxonomy

The v0 inventory contains fifteen entity kinds. `Task` is separate from
`Block`: a grading contract is not a circuit block, and preserving that boundary
makes task-version joins explicit.

| Entity kind | Identity and role | Example |
|---|---|---|
| `Task` | One versioned TapeoutBench task surface, including harness identity. | `task.pll3-vco-sg13g2` |
| `Device` | A physical device, matched group, passive, or model-bearing implementation target. It may summarize several named instances when the task parameter changes them together. | PLL3 cross-coupled PMOS pair |
| `Block` | A functional circuit block within a task. | charge pump, loop filter, VCO core |
| `Topology` | A discrete or fixed connectivity choice implementing a block. | `pfd_drive2`, complementary LC-VCO |
| `Parameter` | An agent-adjustable typed task lever with exact target paths and legal domain. | `w_cc_p_um`, `c1_mult` |
| `Measurement` | A semantic observable independent of whether it is scored. | `osc.freq`, `charge.mismatch_worst` |
| `SpecRow` | A task row binding a measurement to an operator, units, condition set, and global or per-corner limits. | `osc_freq_lock`, `cp_compliance_span` |
| `Corner` | A named process/temperature/supply/model combination. Active and historical corners remain distinguishable. | `ss_85c_1v08` |
| `Condition` | A named stimulus, sweep, operating-point, or validity predicate that is not itself a PVT corner. | `ctrl_grid`, unpatched hot biased svaricap |
| `Mechanism` | A physical, model, or measurement mechanism through which influence propagates. | tank loading, partial edge collection |
| `Tradeoff` | A first-class antagonistic coupling between objectives, with its observed scope. | pulse mismatch versus compliance span |
| `RecipeStage` | One ordered analysis/extraction stage with declared outputs. | fine dead-zone fit, linear loop AC |
| `ValidityGate` | A predicate that controls whether downstream evidence may be interpreted. | dead-zone fit R² and settling gate |
| `Trap` | A tempting but contradicted shortcut or an intentionally discriminated failure mode. | “DC current equals delivered-charge current” |
| `ModelArtifact` | A compact-model/library/image variant whose identity and validity range matter. | unpatched svaricap `dsubw` card |

Devices do not replace task target strings: the parameter retains exact
instance/property targets, while a `targets` edge connects those strings to a
reasoning-level device group. Conditions do not replace corners: a control grid
can be reused across three corners, and the two scopes must remain independently
queryable.

## Relation taxonomy

Every relation is directed in storage even when its query meaning is symmetric.
`trades_off` is queried in both directions and stored once using the graph
author's canonical ID order. The “agent question” column is normative: a
relation that cannot answer that question belongs under another kind or in a
new schema version.

| Relation kind | Typed endpoints | Agent question answered | Evidence grades |
|---|---|---|---|
| `contains` | `Task|Block|Topology` -> contained entity | What task/block/topology owns this entity? | Usually `derived/task_contract`; `textbook` is forbidden. |
| `implements` | `Topology` -> `Block` | Which connectivity choice realizes this functional block? | `derived/task_contract`. |
| `targets` | `Parameter` -> `Device` | Which physical instances or matched group does this lever change? | `derived/task_contract`. |
| `specifies` | `SpecRow` -> `Measurement` | Which observable, rather than a similarly named row, is evaluated? | `derived/task_contract`. |
| `evaluated_under` | `SpecRow|Measurement|RecipeStage` -> `Condition|Corner` | Under which stimulus set or PVT corner is the statement evaluated? | `derived/task_contract|recipe_contract`, or `measured` for an observed dataset. |
| `influences` | `Parameter|Condition|Corner|Mechanism` -> `Mechanism|Measurement` | If the source increases or changes as declared, what downstream quantity changes, in which direction, by how much, and through what path? | `measured`, `derived`, or `textbook`; task-specific magnitudes require `measured` observations. |
| `trades_off` | `SpecRow` -> `SpecRow`, with `Tradeoff` and `Mechanism` references | Which scored/report-only objectives fight, and why? | `measured` or `derived/causal_interpretation`; `textbook` may propose but cannot establish a task tradeoff. |
| `measured_by` | `Measurement` -> `RecipeStage` | Which stage produces this observable, under what output binding? | `derived/recipe_contract`. |
| `depends_on` | `RecipeStage` -> prerequisite `RecipeStage` | What must run first, and which named values flow into this stage? | `derived/recipe_contract`. |
| `models` | `ModelArtifact` -> `Device` | Which physical/model-bearing device is governed by this artifact? | `derived/task_contract|causal_interpretation`. |
| `valid_when` | `ModelArtifact` -> `Condition|Corner` | Within which explicitly checked range may this model evidence be used? | `measured` for validated points or `derived` for a bounded rule. Never silently extrapolated. |
| `invalid_when` | `ModelArtifact` -> `Condition|Corner` | Which condition makes evidence unknown or invalid, and what symptom occurs? | `measured` and/or `derived/causal_interpretation`. |
| `repairs` | fixed `ModelArtifact` -> defective `ModelArtifact` | Which exact artifact change addresses which defect, and what was rechecked? | `measured` or `derived/causal_interpretation`. |
| `guards` | `ValidityGate` -> `RecipeStage|Measurement|SpecRow|Corner` | Which downstream interpretation is blocked if this gate fails? | `derived/recipe_contract`; a threshold may be measured only if the source says so. |
| `catches` | `SpecRow|ValidityGate` -> `Trap` | Which row or gate prevents an attractive but invalid shortcut? | `derived/causal_interpretation`, normally backed by measured trials. |

### Influence sign and strength

An `influences` edge declares one of `positive`, `negative`, `mixed`, `none`, or
`unknown`:

- `positive`: increasing the source increases the target in the frozen scope;
- `negative`: increasing the source decreases the target;
- `mixed`: the direction changes over the recorded interval or several coupled
  outputs are intentionally represented together;
- `none`: the measured change was negligible for the stated conclusion; and
- `unknown`: the source establishes relevance but not a defensible direction.

Path signs compose only through `positive` and `negative` edges. Any `mixed`,
`none`, or `unknown` member makes the composed sign respectively mixed, none,
or unknown; the engine never guesses a direction.

Strength is an ordinal provenance aid: `structural`, `strong`, `moderate`,
`weak`, or `unknown`. `structural` means an identity or contract dependency,
not “larger physical sensitivity.” Other labels reflect the source author's
qualitative characterization and measured discrimination. They are not
comparable across unlike units or task families. Numeric observations remain
the authority; the query engine never ranks `89 MHz` against `0.984 V` as if
they shared a scale.

### Quantification and scope

Measured influence may carry one or more closed observation records:

```json
{
  "quantity": "osc.freq",
  "intervention": "joint cross-coupled widths x2",
  "baseline": 2439900000.0,
  "observed": 2350500000.0,
  "delta": -89000000.0,
  "unit": "Hz",
  "note": "delta is the source-reported rounded value"
}
```

An observation is not required to contain all of baseline, observed, delta, and
ratio, but it must contain at least one numeric fact. Units are explicit and no
conversion is implicit.

Scope can name condition IDs, corner IDs, a design point, intervention text,
co-varied parameters, and an extrapolation policy. Measured seed edges use
`extrapolation: forbidden`: they describe local evidence at the recorded
reference/golden designs. A query may return such an edge outside its scope only
as a caveat-marked prior; it may not silently present the magnitude as a
prediction.

## Graph invariants

The JSON Schema provides closed local shapes. The prototype semantic validator
adds these graph-wide invariants:

1. Node IDs and edge IDs are individually unique.
2. Every edge source and target exists.
3. Endpoints match the relation table above.
4. Referenced mechanisms, tradeoffs, conditions, corners, and co-varied
   parameters exist and have the required entity kind.
5. Every edge has a nonempty evidence-pointer list; every pointer uses an
   immutable commit revision.
6. Parameter target arrays and recipe bindings are nonempty where their relation
   requires them.
7. A dependency cycle is invalid; a recipe query cannot invent an execution
   order for cyclic stages.
8. JSON duplicate keys, non-finite numbers, and unknown fields fail closed.

Canonical graph hashing uses UTF-8 JSON with sorted keys, no insignificant
whitespace, no NaN/infinity, and a terminal-free byte string equivalent to
`json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)`.
Human-maintained seed files may be pretty-printed; their semantic digest is over
the canonical form.

## Query contract for the prototype

The evaluation prototype supports five read-only query kinds. Entity arguments
accept a full node ID or an unambiguous local semantic name. Ambiguity and an
unknown entity are errors, not empty “successes.” Results always use stable IDs
and deterministic ordering.

| Query | Result meaning |
|---|---|
| `influences(parameter)` | Traverse forward over `influences` edges through zero or more `Mechanism` nodes. Return each reachable measurement, composed sign/strength, mechanism path, bound SpecRows, quantification, scope, and evidence. |
| `levers(measurement)` | Traverse the same paths in reverse. Rank parameters by task-specific evidence grade, then declared strength, then shortest path and stable ID. This is a search priority, not normalized sensitivity. |
| `tradeoffs(spec_row)` | Search `trades_off` symmetrically and return the coupled row, first-class tradeoff, mechanism, scope, and evidence. |
| `recipe(measurement)` | Resolve `measured_by`, recursively close `depends_on`, reject cycles, and return prerequisites before the producing stage with named bindings. |
| `validity(condition)` | Return matching `valid_when` and `invalid_when` assertions, affected model/device, predicates, validation bounds, repair relation, and evidence. Invalid or unpatched evidence remains visible even when a patched artifact exists. |

The prototype does not apply algebraic inference across arbitrary edge kinds.
In particular, `contains` and `targets` do not imply physical causality, a
tradeoff does not imply Pareto optimality, and two parameters targeting the same
device are not automatically substitutes.

## Seed policy and public-data boundary

The seed graph is limited to tracked, committed facts in the public
`bench-arena` record named by each edge. It does not copy or inspect the local
unlicensed reproduction tree. It retains corrected PLL1 remeasurement claims
and excludes the earlier withdrawn dead-zone, loop-gain, phase-margin, and
limit-cycle interpretations.

Important authoring rules exposed by this seed are:

- `osc_freq_lock` is a `SpecRow`; `osc.freq` is its `Measurement`. Query results
  expose both instead of conflating their names.
- The PLL3 x0.5/x2 core trials co-varied NMOS and PMOS widths. Individual lever
  paths retain that joint-intervention qualifier.
- The historical SS/40 °C VCO dataset remains evidence but is not an active task
  corner after the svaricap fix restored SS/85 °C.
- The unpatched svaricap model's mathematical temperature-law boundary and the
  empirically exercised failure condition are separate predicates. “Above
  52.5 °C” alone is not asserted to make every transient fail.
- The PLL1 drive-strength choice is present in the task topology surface, but no
  requested public sweep isolates drive1 versus drive2. The graph does not
  fabricate a measured drive-strength sensitivity.

## Honest limits

The graph must not claim any of the following:

- that a simulator observation is silicon measurement or signoff evidence;
- that a quantified edge extrapolates to another corner, topology, model
  revision, or design point unless a separately evidenced relation says so;
- that a joint intervention establishes isolated partial derivatives;
- that a structural identity is a freely tunable independent objective;
- that task validity, behavioral lock, or successful simulation implies all
  performance specifications pass;
- that absence of an edge proves absence of an effect; or
- that ranking a lever proves it is the best optimization move.

Unknown knowledge remains unknown. Task authors extend the graph by adding
evidenced nodes and edges, not by loosening the schema or putting prose blobs in
an extension field.

## Owner rulings still open

1. **Contract evidence grade:** v0 uses `derived` plus a mandatory
   `task_contract|recipe_contract` basis. Should a future schema add a fourth
   `declared` grade, or keep the three-grade ladder uniform?
2. **Joint interventions:** v0 returns individual candidate levers with explicit
   `co_varied_parameters`. Should future authoring instead require a first-class
   `Intervention` entity before any non-isolated causal edge is accepted?
3. **Corner identity:** should equal PVT triples be shared graph-wide, as v0 does,
   or namespaced per task so differing model-image provenance cannot be hidden?
4. **Strength vocabulary:** should `strong/moderate/weak` remain author labels,
   or be removed until each measurement family defines normalized sensitivity?
5. **Artifact provenance conflict:** PLL3's top-level task image digest still
   names the base image while its active SS/85 °C comments require the
   `pdkfix-1099` image and patched-library hash. Which identity must a released
   task bind before the KG may call that corner runnable?
6. **Historical evidence:** should retired conditions such as SS/40 °C remain in
   the operational seed by default, or move to a separate history graph loaded
   only on request?

## Integration sketch

Deferred to the final spike tier after the prototype query and negative tests
establish the smallest viable request/result surface. No runtime integration or
`src/openada/` change is part of this spike.
