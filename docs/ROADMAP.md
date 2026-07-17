# Driver status and roadmap

OpenADA uses explicit maturity levels:

- **Discovered**: OpenADA can resolve the executable and inspect a bounded version string.
- **Structured**: A versioned semantic operation and normalized result exist.
- **Workflow-validated**: The operation passes a pinned, publicly reproducible
  fixture or design, required PDK (if any), and runtime conformance case.

## Current preview

| Tool | Discovery | Structured operation | Workflow validation |
|---|---:|---:|---:|
| Xschem | yes | `netlist` | pinned public IHP Xschem-to-ngspice recipe |
| ngspice | yes | `simulate` including shared OP/DC/AC/TRAN | shared TRAN workflow-validated; OP/DC/AC have pinned structured success evidence |
| KLayout | yes | `drc` | pinned public IHP inverter recipe |
| Netgen | yes | `lvs` | pinned public IHP inverter recipe |
| Yosys | yes | `rtl-check`; Liberty-mapped `synthesize` | pinned public IHP SAR structural check and ORFS Ibex synthesis recipes |
| Verilator | yes | strict `rtl-lint` | pinned public IHP SAR clean/failure recipe |
| OpenSTA | yes | single-corner `timing-analyze` | pinned ORFS Ibex/Nangate45 setup-hold recipe |
| Magic | yes | no | no |
| Xyce | yes | `simulate` shared DC/AC/TRAN alpha; OP unsupported | shared TRAN workflow-validated; DC/AC have pinned structured success evidence |
| OpenROAD / LibreLane | yes | no | no |
| Icarus / standalone slang / Surelog | yes | no | no |
| OpenVAF / Qucs-S / GTKWave | yes | no | no |
| OpenADA evidence kernels | n/a | verified series extraction; scalar/specification; coherent SNR/SINAD/THD/SFDR; closed AC transfer/gain/bandwidth/unity-frequency/phase-margin | structured unit/profile conformance; public native-chain bundle pending |
| Explicit external provider | explicit manifest | local JSON-stdio wait dispatch for active `circuit.simulate/v1alpha2` | hardened out-of-tree fake-provider suite; independent real provider pending |

## Protocol program

The v0.4 implementation proves a common evidence envelope and hardened native
drivers. The next program turns that foundation into a portable intent and
driver protocol. Each milestone has a concrete acceptance gate; adding more
one-off wrappers does not substitute for passing the gate.

Current status: milestone A is published in this repository. Milestone B now
includes executable request and driver-manifest v0alpha1 validation, immutable
operation-profile schemas v0alpha1 and v0alpha2, valid contributor templates,
nine active implemented typed profiles, one historical simulation profile,
cwd-independent profile inspection, and an explicit-manifest local JSON-stdio
wait resolver registered for active circuit simulation. Automatic discovery,
session/remote transports, and
MCP remain unimplemented. Milestone C's bounded portability gate now covers
ngspice OP/DC/AC/TRAN and Xyce DC/AC/TRAN success paths through pinned native
replay; the new OP/DC/AC rows remain structured until broader outcome cases are
published. The same native formats now feed verified normalized series and a
closed spectral and AC transfer kernels. Milestone D has eight experimental
tool-independent engineering skills above the execution adapter; the analog
coordinator now uses an immutable intent ledger and implemented-primitive
routing. One separate experimental onboarding coordinator freezes a fresh ASIC
project context and contains explicitly authorized native capability-gap work.
Fresh-agent forward tests and external engineering review still gate promotion.

### A. Publish the semantic boundary

- Define operation, assertion, request, driver capability, evidence, and
  artifact-lineage responsibilities in the public semantic model.
- State the bounded initial ontology and distinguish shipped command aliases
  from target operation profile identifiers.
- Put one-intent/multiple-backend examples and an honest current-versus-target
  table in the README.
- Publish contributor-facing operation and driver templates.

**Gate:** a new contributor can explain what belongs in an operation profile,
what remains driver-specific, and what evidence supports `pass`, `fail`, or
`unknown` without reading out-of-repository material.

### B. Encode the protocol

- Publish immutable alpha request and driver-manifest schemas.
- Publish typed measurement and specification profiles through the additive
  immutable operation-profile v0alpha2 schema while preserving v0alpha1.
- Add operation-profile identifiers, assertion identifiers, driver identity,
  and lineage to the next result envelope without changing the immutable
  `openada.result/v0alpha1` schema.
- Extend the implemented explicit local-CLI invocation boundary to separately
  versioned discovery, persistent-session, and remote-job transports only after
  their trust and artifact semantics are frozen.
- Extend conformance checks from the generic envelope to operation-specific
  request/result truth tables and fixtures.

**Gate:** a driver package outside the OpenADA Python source tree can advertise
one operation, receive a schema-valid request, and return conformance-checked
evidence without changing an agent harness.

### C. Prove portability

- Harden the built-in `circuit.simulate` alpha mapping through ngspice and
  Xyce.
- Use identical request semantics and assertion truth tables while retaining
  each simulator's native deck, command, logs, and result artifacts.
- Keep the shared alpha subset to one self-contained OP, DC, AC, or transient
  analysis with no includes, measurements, print directives, control-language
  blocks, FFT, noise, Monte Carlo, or multiple analyses.
- Demonstrate that execution success, valid simulation evidence, measurement
  extraction, spectral/scalar measurement, and specification satisfaction
  remain separate claims outside the shared simulation assertion.

**Success-path gate (passed for the advertised analysis rows):** ngspice 46 passes
OP/DC/AC/TRAN and Xyce 7.10-opensource passes DC/AC/TRAN in the pinned
IIC-OSIC-TOOLS `2026.06` image. Xyce OP is explicitly unsupported. The
independent verifier parses native raw evidence and compares analysis-specific
semantic results without requiring identical sampling. These expanded
success-only cases support structured maturity; they do not establish every
workflow outcome required for workflow-validated maturity.

### D. Grow the engineering-skill layer

- Keep the OpenADA execution skill thin and put engineering review or diagnosis
  workflows in separate sibling skills.
- Require engineering skills to compose versioned operations and normalized
  evidence rather than native commands or backend log grammars.
- Forward-test success, failure, unknown, invalid, and unavailable paths on
  realistic public tasks.
- Forward-test the simulation-review skill unchanged across the shared
  ngspice and Xyce mappings without conflating feature-level maturity.
- Keep analog characterization, feedback stability, spectral linearity, and
  PVT/yield skills capability-gated: unsupported measurements or campaign
  primitives remain not evaluated rather than becoming native-command
  fallbacks.
- Keep the ASIC onboarding coordinator separate: default missing operations to
  not evaluated, require explicit authorization for exploratory native gap
  work, and retain that evidence outside OpenADA result envelopes.
- Track skill maturity separately from driver maturity and contract versioning.

**Gate:** one contributed engineering skill composes a versioned operation on
two conforming backends without backend-specific prompt instructions, passes
realistic positive and uncertainty/failure cases, and is reviewed by an
engineer other than its author.

### E. Earn community extensibility

- Have an external maintainer or researcher author or review a driver and its
  operation mapping.
- Add a driver-development walkthrough with bounded pass, fail, unknown,
  unavailable-tool, malformed-evidence, and timeout cases.
- Require two plausible native mappings before promoting a core operation
  profile beyond alpha.

**Gate:** an external driver reaches structured maturity without adding a new
model-facing API or modifying OpenADA's central dispatch.

A provider marketplace and MCP integration belong to this milestone's future
connection layer. An MCP adapter should transport unchanged semantic
requests/results; a marketplace should catalog conforming capability providers
and immutable conformance records. The v0alpha1 manifest has neither a
normative MCP binding nor per-feature capability identity/maturity, so an
additive manifest revision is required. Neither concept substitutes for host
trust, deterministic resolution, or runtime capability checks. See
[Providers, marketplaces, and MCP](PROVIDERS_AND_MCP.md).

### F. Add mutation and design history

- Implement the preview/apply/revert lifecycle in
  [Mutation and design versioning](MUTATION_AND_VERSIONING.md).
- Bind every plan to an observed base revision and require explicit apply
  authorization plus optimistic-concurrency checks.
- Record append-only change receipts, semantic/native diffs, postconditions,
  and base-to-result artifact lineage.
- Prove the lifecycle first on a small text-native SPICE or Xschem fixture in a
  disposable copy, then expand to other native formats only after identity,
  conflict, and rollback behavior are independently checkable.

**Gate:** stale-base, failed-postcondition, partial-write, and failed-rollback
fixtures fail closed, while a successful apply and revert each produce an
independently checkable new revision. No test writes an authoritative shared
design in place.

The bootstrap is ready for a quiet, explicitly preview-quality group when
these gates pass:

- Standalone installation and direct checkout execution.
- Codex and Claude Code manifests validate.
- Unit tests cover process/engineering status separation.
- One pinned public workflow has a replayable recipe and semantic assertions.
- Project-scoped preflight maps one fixed assertion to one tool/operation
  without recursively inventorying project or PDK contents.

Claims such as "faster," "more reliable," or "outperforms raw binaries"
additionally require credible comparison evidence:

- A pinned reference-container identity is recorded for published comparisons.
- The launch comparison publishes identical prompts, design revision, model,
  runtime, time budget, and publishable action/tool traces for both conditions.
- A precommitted pair schedule, exact per-file treatment manifest, single-attempt
  supervisor observations, sealed trial rows, and complete intention-to-treat
  accounting establish which rows are eligible for each reported metric.
- A signed summary alone authenticates publisher output. Independent
  recomputation requires publication of the exact campaign, fixed-algorithm
  plan, and every sealed sanitized row committed by that summary.

A single with/without trace can introduce the category as an illustration. It
must not be presented as a general benchmark result.

## Current pinned local validation

The July 2026 validation pass uses IIC-OSIC-TOOLS `2026.06` on linux/amd64
(manifest digest
`sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0`)
and IHP AnalogAcademy revision
`133ecf657572e021b5921b5a1b7693abfb209623`. The DRC/LVS manifest also binds
the bundled `ihp-sg13g2` PDK revision
`144f811cdffda49b71d28f64e8a92b697b61cf06` through its hashed `COMMIT`
file. In that environment:

- all eight structured or workflow-validated tool binaries pass bounded
  discovery;
- a real IHP Xschem inverter netlists without unresolved symbols;
- the real inverter GDS reports zero multiplicity-weighted KLayout DRC
  violations from a fresh exact LYRDB whose generator and top cell match the
  invocation;
- the supplied extracted and schematic inverter netlists match in Netgen with
  an exact fresh comparison report, agreeing native JSON, and a clean bounded
  setup/completion transcript;
- the SAR logic RTL elaborates and passes Yosys structural checks;
- the same SAR logic passes strict Verilator lint with no warnings or errors;
- pinned ORFS Ibex RTL synthesizes through the Slang/Yosys frontend into a
  complete Nangate45 Liberty-mapped netlist with retained inference and mapped
  statistics;
- OpenSTA returns constraint-complete setup/hold evidence for that mapped Ibex
  netlist and exact ORFS SDC; any negative slack remains an engineering failure,
  not a connector failure;
- the Xschem-to-ngspice path captures an explicitly declared deck-owned raw
  artifact and independently verifies finite transient inverter behavior;
- the shared model-free RC profile runs natively through ngspice 46 and Xyce
  7.10-opensource across their advertised OP/DC/AC/TRAN matrix, with independent
  native raw parsing and matching engineering behavior where both advertise an
  analysis.

These results are not an endorsement by JKU or IHP. KLayout DRC and Netgen LVS
are workflow-validated through the public
[IHP inverter replay](../conformance/ihp-inverter/README.md). Xschem and ngspice
are workflow-validated through the public
[Xschem-to-ngspice replay](../conformance/ihp-inverter-ngspice/README.md). The
shared transient ngspice/Xyce mappings retain workflow-validated maturity.
Expanded OP/DC/AC success cases are independently checked through the
[native portability replay](../conformance/circuit-simulate-v0alpha2/README.md)
at structured maturity. The public IHP SAR and ORFS Ibex recipes separately
bind Yosys structural and mapped-synthesis evidence; Verilator lint and OpenSTA
timing remain scoped to their exact declared contexts.

## First public conformance case

The `ihp-inverter-drc-lvs` manifest pins the linux/amd64 image digest, public
design revision and license, bundled PDK revision file, exact native inputs,
DRC/LVS semantics, and fresh artifact verification. Its runner disables network
access for each EDA process. Its verifier independently rehashes the LYRDB and
bounded KLayout transcript, checks the native generator/top-cell/category
shape, totals item multiplicities, and separately checks Netgen's final report,
native JSON, bounded transcript, declared PDK provenance input, and agreement
with the normalized LVS decision.

## Next contract increments

The preview candidate now includes an immutable-schema
[compatibility policy](COMPATIBILITY.md) and a small
[driver conformance kit](../conformance/driver-kit/README.md). The kit documents
the extension path from discovery to structured operation to independently
verified workflow validation.

1. Exercise the scoped preflight plus one operation with technically credible
   external users; fix installation, discovery, and collateral-selection
   failures without adding project crawling.
2. Exercise and refine that contributor path with one independently authored
   driver or operation.
3. Keep the pinned simulation chain reproducible across the native server,
   sandbox, and frozen container. Record exact binary, PDK, system `spinit`, and
   environment identities; do not equate different point counts or bytewise raw
   hashes with different waveform semantics. A department-runtime row is
   deferred and is not a current gate.
4. The offline raw-agent versus OpenADA plan/reducer/scorer/summarizer is now
   available. Implement and independently validate the credential-isolated live
   supervisor which produces the required raw-absence/treatment-exact,
   single-attempt, dispatch-clock, and complete-pair observations. Add a trusted
   native executor audit only before reporting native-process causality, then
   run every precommitted fresh pair. Publish the exact campaign, plan, signed
   summary, and sealed sanitized rows for full verification before making a
   comparative performance claim.
5. Add broader digital and mixed-signal operations only after they have bounded
   semantics and public fixtures.
6. Define install/approval, capability resolution, and immutable conformance
   records for provider catalogs before calling any provider list a marketplace;
   retain MCP as transport rather than adding MCP tool names to the ontology.

## Deliberately outside v0.4

- Write-capable mutation or workspace-wide rollback machinery. The lifecycle is
  specified now, but runtime support begins only after the read/evidence driver
  protocol is externally invocable and conformance-checked.
- Heuristic schematic-to-layout correlation.
- Generic or substituted DRC/LVS collateral.
- Claims of foundry signoff or universal EDA support.
- A container as the only installation path.
- Harness-specific reasoning logic in the core Python package.
- True zero-frequency DC gain, gain margin, phase-crossing search, poles/zeros,
  integrated noise, corners, nested sweeps, Monte Carlo, and campaign/yield
  execution. The implemented transfer alpha covers a same-unit Cartesian ratio,
  full magnitude/phase trace, unique falling bandwidth/unity crossings, and
  explicitly declared negative-feedback phase margin only.
- Noncoherent/windowed spectral methods, PSD/averaging, ENOB, jitter, and phase
  noise without complete versioned observation models.
- External-manifest auto-discovery, installation, ranking, MCP invocation,
  sessions, remote jobs, artifact transfer, and a live provider marketplace.
