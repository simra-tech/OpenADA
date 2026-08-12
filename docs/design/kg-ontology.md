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
| `measured` | The source reports an observed sweep, trial, or comparison under named conditions. | Causal and tradeoff edges carry numeric baseline, observation, delta, ratio, or range. Valid only inside the recorded design point and condition scope. |
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
| `SpecRow` | A task row binding a measurement to an operator, units, optional declared condition set, and global or per-corner limits. A conditionless report-only placeholder remains conditionless. | `osc_freq_lock`, `phase_noise_1m` |
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
| `influences` | `Parameter|Topology|Condition|Corner|Mechanism` -> `Mechanism|Measurement` | If the source increases or changes as declared, what downstream quantity changes, in which direction, by how much, and through what path? | `measured`, `derived`, or `textbook`; task-specific magnitudes require `measured` observations. |
| `trades_off` | `SpecRow` -> `SpecRow`, with `Tradeoff` and `Mechanism` references | Which scored/report-only objectives fight, by how much in the recorded intervention, and why? | `measured` with numeric observations or `derived/causal_interpretation`; `textbook` may propose but cannot establish a task tradeoff. |
| `measured_by` | `Measurement` -> `RecipeStage` | Which stage produces this observable, under what output binding? | `derived/recipe_contract`. |
| `depends_on` | `RecipeStage` -> prerequisite `RecipeStage` | What must run first, and which named values flow into this stage? | `derived/recipe_contract`. |
| `models` | `ModelArtifact` -> `Device` | Which physical/model-bearing device is governed by this artifact? | `derived/task_contract|causal_interpretation|model_debug`. |
| `valid_when` | `ModelArtifact` -> `Condition|Corner`, with optional `Mechanism` reference | Within which explicitly checked range may this model evidence be used, and through which model mechanism? | `measured/model_debug` for validated points or `derived/model_debug|calculation` for a bounded rule. Never silently extrapolated. |
| `invalid_when` | `ModelArtifact` -> `Condition|Corner`, with optional `Mechanism` reference | Which condition makes evidence unknown or invalid, through what model mechanism, and what symptom occurs? | `measured/model_debug` or a bounded `derived/model_debug|calculation|causal_interpretation` rule. |
| `repairs` | fixed `ModelArtifact` -> defective `ModelArtifact` | Which exact artifact change addresses which defect, and what was rechecked? | `measured/model_debug` or `derived/model_debug|causal_interpretation`. |
| `guards` | `ValidityGate` -> `RecipeStage|Measurement|SpecRow|Corner` | Which downstream interpretation is blocked if this gate fails? | `derived/recipe_contract|causal_interpretation`; a threshold may be measured only if the source says so. |
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

Path signs compose only through `positive` and `negative` edges. A `none`
segment absorbs the path. Otherwise `unknown` takes precedence over `mixed`;
only a path containing none of those three values uses positive/negative parity.
The engine never guesses a direction.

Strength is an ordinal provenance aid: `structural`, `strong`, `moderate`,
`weak`, or `unknown`. `structural` means an identity or contract dependency,
not “larger physical sensitivity.” Other labels reflect the source author's
qualitative characterization and measured discrimination. They are not
comparable across unlike units or task families. Numeric observations remain
the authority; the query engine never ranks `89 MHz` against `0.984 V` as if
they shared a scale.

### Quantification and scope

Measured influence and tradeoff edges carry one or more closed observation
records; qualitative derived/textbook causal priors may use an empty list:

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
co-varied parameters, and an extrapolation policy. Every influence edge must
carry a scope and explicit extrapolation policy. Measured seed edges normally
use `extrapolation: forbidden`: they describe local evidence at the recorded
reference/golden designs. Multi-hop results report `compatible`,
`requires_review`, or `incompatible` scope composition. Distinct condition sets
or prose-scoped interventions require review; disjoint explicit corner sets are
incompatible. Such paths remain visible as caveat-marked reasoning priors, never
as silently extrapolated end-to-end sensitivities.

## Graph invariants

The JSON Schema provides closed local shapes. The prototype semantic validator
adds these graph-wide invariants:

1. Every ID is unique across the combined node-and-edge namespace.
2. Every edge source and target exists.
3. Endpoints match the relation table above.
4. Referenced mechanisms, tradeoffs, conditions, corners, and co-varied
   parameters exist and have the required entity kind.
5. Every edge has a nonempty evidence-pointer list. Pointer repository and
   revision equal the graph's public `source_snapshot`, and paths are normalized
   repository-relative paths without `.` or `..` traversal.
6. Relation-specific evidence grade/basis combinations follow the taxonomy
   table; for example, a `contains` declaration cannot masquerade as textbook
   evidence.
7. Parameter value type, domain shape, bounds, and reference agree. Spec
   operator/report-only/limit shapes agree; conditionless report-only rows are
   allowed, while scored rows require a condition and limit.
8. Parameter target arrays equal the union of their `targets` edges. Each
   SpecRow's measurement and optional condition agree with exactly one
   `specifies` and, when present, one condition-set `evaluated_under` edge.
9. Influence edges carry explicit scope/extrapolation; parameter target arrays
   and recipe bindings are nonempty where their relation requires them.
10. A dependency cycle is invalid; a recipe query cannot invent an execution
    order for cyclic stages.
11. JSON duplicate keys, non-finite numbers, and unknown fields at every nested
    layer fail closed in the standalone CLI as well as formal schema validation.

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
| `influences(parameter|topology|condition|corner)` | Traverse forward over `influences` edges through zero or more `Mechanism` nodes. Return each reachable measurement, composed sign/strength, scope-compatibility status, mechanism path, bound SpecRows, quantification, scope, and evidence. |
| `levers(measurement)` | Traverse the same paths in reverse. Rank parameters by scope compatibility, then task-specific evidence grade, declared strength, shortest path, and stable ID. This is a search priority, not normalized sensitivity. |
| `tradeoffs(spec_row)` | Search `trades_off` symmetrically and return the coupled row, first-class tradeoff, mechanism, numeric observations, scope, and evidence. |
| `recipe(measurement)` | Resolve `measured_by`, recursively close `depends_on`, reject cycles, and return prerequisites before the producing stage with named bindings. |
| `validity(condition)` | Return matching `valid_when` and `invalid_when` assertions, affected model/device, model-validity mechanism, predicates, validation bounds, repair relation, and evidence. Invalid or unpatched evidence remains visible even when a patched artifact exists. |

The prototype does not apply algebraic inference across arbitrary edge kinds.
In particular, `contains` and `targets` do not imply physical causality, a
tradeoff does not imply Pareto optimality, and two parameters targeting the same
device are not automatically substitutes.

A ranked lever whose best path names `co_varied_parameters` is explicitly a
**joint candidate**, not an independently established lever. The result carries
`requires_co_variation: true`, lists those parameters, and warns that rank and
sign are conditional on the recorded joint intervention. Separate list entries
make task-parameter discovery convenient; they do not split one experiment into
independent sensitivities.

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
- The task's `phase_noise_1m` placeholder declares no condition; the graph does
  not invent `ctrl_grid` merely to satisfy a schema shape.
- The unpatched svaricap model's mathematical temperature-law boundary and the
  empirically exercised failure condition are separate predicates. “Above
  52.5 °C” alone is not asserted to make every transient fail.
- The PLL1 drive-strength choice is present in the task topology surface. Its
  public design rationale connects drive strength to reset-pulse/dead-zone and
  charge-injection behavior, so topology influence paths are queryable with
  `unknown` sign/strength and bounded derived evidence. No public sweep isolates
  drive1 versus drive2, so the graph does not fabricate a measured sensitivity
  or include either topology in the parameter-only `levers` ranking.

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
7. **Empty-result status:** v0 recommends a successful query with an explicit
   empty result and coverage warning. Should a future assertion instead use
   engineering `fail` for “known graph contains no matching assertion,” despite
   the risk that agents read that as proof of no physical effect?
8. **Graph composition:** should v1alpha1 accept exactly one precomposed,
   hash-bound graph, or define a deterministic base-plus-task overlay/precedence
   model? The latter is more reusable but adds conflict and revocation semantics.
9. **Publication authority:** who may promote a new measured or derived causal
   edge into a scored task release, and what review/receipt binds that decision?
   A valid schema proves shape, not engineering correctness.
10. **Discrete topology levers:** v0 permits `Topology` as an `influences`
    source but keeps `levers` parameter-only. Should a later result union rank
    topology choices beside scalar parameters, or expose a separate
    `choices(measurement)` query to avoid incomparable candidate kinds?
11. **Recipe analysis versus role:** should `RecipeStage` split simulator
    `analysis` (`dc|tran|ac`) from stage role (`simulation|extraction|gate`)? V0
    follows the public stage labels, which means `dz_fine` and
    `loop_bhv_lock` do not expose their underlying transient analysis as a
    separate typed field.
12. **Scope algebra:** should a later profile define task-authored equivalence
    IDs for compatible design points/interventions? V0 can prove disjoint
    corners, but prose-scoped multi-hop composition remains conservatively
    `requires_review` rather than pretending string equality is physical proof.

## Integration sketch

Rung 2 should serve this graph as one read-only semantic operation beside rung
1's EDA operations. The research CLI demonstrates behavior:

```bash
python3 evaluation/kg/kg_query.py influences w_cc_p_um
python3 evaluation/kg/kg_query.py levers osc.startup_time
python3 evaluation/kg/kg_query.py tradeoffs cp_mismatch_pulse
python3 evaluation/kg/kg_query.py recipe linear.phase_margin
python3 evaluation/kg/kg_query.py validity unpatched_hot_biased_svaricap
```

Production naming should use:

- operation: `openada.operation/kg.query/v1alpha1`;
- assertion: `openada.assertion/kg.query.valid/v1alpha1`;
- side-effect mode: `read-only`;
- target kind: `analog-knowledge-graph`;
- locator: one content-bound `artifact` initially; and
- features:
  `openada.feature/kg.query.influences/v1alpha1`,
  `openada.feature/kg.query.levers/v1alpha1`,
  `openada.feature/kg.query.tradeoffs/v1alpha1`,
  `openada.feature/kg.query.recipe/v1alpha1`, and
  `openada.feature/kg.query.validity/v1alpha1`.

The package would add the profile, graph catalog/binding, and driver only after
the owner rulings above. This spike intentionally does not change protected
runtime surfaces.

### Proposed `kg.query/v1alpha1` profile

The operation profile should use `openada.operation-profile/v0alpha2`, an
`openada.request/v0alpha1` base request, and an `openada.result/v0alpha1`
normalized result. Its operation-owned request parameters are closed:

```json
{
  "query": {
    "kind": "influences",
    "entity_id": "parameter.pll3.w_cc_p_um"
  },
  "extensions": {}
}
```

`kind` is exactly one of `influences`, `levers`, `tradeoffs`, `recipe`, or
`validity`. `entity_id` is a stable full node ID in the versioned operation;
the research prototype's friendly-name resolution is CLI convenience and
should not enter the portable request contract. The base request's target is
the graph artifact and must carry its SHA-256. The semantic implementation
must verify the exact schema ID, canonical graph digest, closed JSON shape, and
graph-wide invariants before resolving the query. V1alpha1 should accept one
precomposed graph only unless the owner explicitly chooses overlay semantics.

Normalized `data` should contain:

```json
{
  "protocol": {
    "graph_schema": "openada.eval/analog-knowledge-graph/v0",
    "graph_id": "tapeoutbench.pll-public-seed",
    "graph_version": "0.1.0",
    "graph_sha256": "<64 lowercase hex characters>",
    "algorithm": "openada.algorithm/kg-query/v1alpha1"
  },
  "query": {
    "kind": "influences",
    "entity_id": "parameter.pll3.w_cc_p_um"
  },
  "matches": [],
  "coverage": {
    "match_count": 0,
    "empty_means_no_recorded_assertion": true
  },
  "limitations": [],
  "extensions": {}
}
```

Each kind needs a closed discriminated match schema corresponding to the v0
prototype result:

- influence matches bind measurement and SpecRow IDs, composed sign/strength,
  mechanism-node and edge paths, evidence pointers, quantification, scope, and
  extrapolation policy plus path-scope compatibility;
- lever matches add deterministic rank and its scope/grade/strength/path-length
  basis, `requires_co_variation`, co-varied parameter IDs, and a joint-trial
  warning, never a fabricated cross-unit sensitivity;
- tradeoff matches bind both SpecRows plus the `Tradeoff` and `Mechanism` IDs
  and measured numeric observations;
- recipe matches put prerequisites before the producer and retain every named
  binding; and
- validity matches retain valid and invalid assertions together, including
  artifact/device and model-mechanism identity, repair path, condition
  predicate, bounded validation, and applicable gates.

Assertion truth should be narrow:

| Status | Meaning |
|---|---|
| `pass` | The exact content-bound graph passed formal and semantic validation, the typed entity resolved, and the deterministic algorithm completed. An empty match list is allowed and explicitly means only “no recorded assertion in this graph.” |
| `fail` | Not emitted in v1alpha1. A closed graph's silence cannot conclusively prove absence of a physical dependency, lever, tradeoff, recipe, or validity issue. |
| `unknown` | The request, schema/digest binding, graph invariants, entity identity, query kind, or algorithm execution was invalid or insufficient. A stable diagnostic identifies the boundary. |

The result's tool identity should name the deterministic KG implementation, not
an EDA backend. No native command, simulator, graph server, or network lookup is
needed. Query execution must not follow source links, execute graph text, or
load unbound overlays.

### Task-author extension workflow

Task authors extend one family in five reviewable steps:

1. Generate the mechanical task surface from the committed task contract:
   `Task`, `Topology`, `Parameter`, `Measurement`, `SpecRow`, `Condition`, and
   `Corner` nodes plus `targets`, `specifies`, and `evaluated_under` edges. The
   generator cannot create causality.
2. Transcribe the public stage recipe into `RecipeStage`, `ValidityGate`,
   `measured_by`, `depends_on`, and `guards`, preserving named bindings and
   whose-fault outcomes.
3. Add mechanisms, influences, tradeoffs, and traps only from a committed
   source. Grade each edge, retain numeric observations and exact scope, mark
   joint interventions, and make extrapolation policy explicit.
4. Validate the Draft 2020-12 schema and graph-wide semantics, add one fixture
   for every new query answer, and add a negative test for each new invariant.
   Reviewers separately assess engineering truth; schema validity is not that
   review.
5. Canonicalize and hash the graph, bind graph ID/version/digest to the task
   release, and retain the old graph for old evidence. Correcting a causal edge
   creates a new graph version/digest rather than rewriting prior task history.

A shared base graph may eventually hold genuinely task-independent textbook
priors. Until overlay conflict semantics are ruled, authors should ship one
precomposed task-family graph so query results have one unambiguous identity.

### Composition with skills and rung-1 operations

Skills consume rung 2 as bounded decision context, then use rung 1 for fresh
evidence. A characterization skill can query `levers` before choosing one
experiment; a stability skill can query `recipe` to discover the design-specific
gain binding; a PVT skill can query `validity` before scheduling a hot corner.
The returned edge scope becomes an assumption in the skill's context ledger.

A normal reasoning loop is:

```text
task + hash-bound graph
  -> kg.query: retrieve scoped mechanism/dependency prior
  -> skill: form one explicit hypothesis and choose one justified intent
  -> OpenADA rung-1 operation: produce fresh analysis evidence
  -> measurement/specification contracts: evaluate the exact new condition
  -> reviewed public task update: optionally publish a new graph version
```

Skills must not silently add edges, promote textbook priors, convert an empty
query into proof, or reuse a magnitude outside its returned scope. A simulation
result does not mutate the released graph in-session; graph authoring is a
separate reviewed publication action. Likewise, the graph may recommend a
measurement recipe, but only the corresponding OpenADA operation can establish
fresh analysis, measurement, or specification evidence.

### Operational and epistemic limits

Rung 2 improves search and explanation, not truth by decree. The integration
must continue to expose the limits already listed above, especially:

- content hash and evidence pointers prove identity, not that an engineering
  interpretation is correct;
- public measured edges in this seed are simulator characterization, not
  silicon data;
- graph coverage is incomplete by design, and absence remains unknown;
- quantified edges are design-point-, topology-, model-, and condition-specific;
- joint interventions remain joint; a query cannot manufacture isolated
  derivatives;
- ordinal strength is a ranking aid within this corpus, never a unitless global
  sensitivity; and
- no graph answer satisfies a task row, validates a simulator run, authorizes a
  mutation, or claims signoff.

Those boundaries belong in the future profile assertion and normalized result,
not only in skill prose, so every agent receives them with the answer.
