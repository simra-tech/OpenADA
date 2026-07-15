# Engineering skills above OpenADA

OpenADA can ship reusable engineering skills without making those skills part
of the semantic protocol. The two layers solve different problems:

| Layer | Stable responsibility | Examples |
|---|---|---|
| Semantic contract and drivers | Turn a versioned intent into deterministic native execution and bounded evidence | simulate, DRC, LVS, netlist, capability discovery |
| Engineering skills | Compose operations and evidence into a disciplined engineering decision | review a simulation, diagnose a DRC result, route an LVS mismatch |

The contract is the narrow waist between agents and EDA backends. Skills sit
above it and are distributed by the plugin. They can evolve more quickly,
encode review practice, and guide an agent through several decisions without
adding a new driver API for every workflow.

```text
engineer or agent goal
          │
          ▼
tool-independent engineering skill
          │  composes versioned intents and evidence
          ▼
OpenADA operation + assertion contract
          │
          ▼
deterministic driver → native open-source EDA
```

## What belongs in an engineering skill

A core engineering skill should:

- express an engineering decision or review workflow, not a tool tutorial;
- consume versioned OpenADA operations and normalized evidence;
- remain backend-independent when two drivers implement the same profile;
- distinguish execution, evidence validity, measurement, specification, and
  signoff claims;
- preserve explicit project inputs such as PDKs, models, corners, decks, and
  top cells instead of guessing substitutes;
- define how `pass`, `fail`, `unknown`, unavailable tools, and invalid requests
  affect the next action;
- use native artifacts for audit and drill-down without reparsing them into a
  competing public contract.

A workflow that needs a missing semantic primitive should expose that gap. It
must not hide a backend flag or log heuristic inside a supposedly portable
skill. Backend-specific knowledge belongs in a driver when it affects
invocation or evidence normalization. A tool-specific skill is reasonable only
as an explicitly temporary exploration of behavior that has no shared OpenADA
operation yet.

Skills are also not conformance evidence. A polished workflow cannot promote a
driver from structured to workflow-validated maturity, and skill wording does
not change the meaning of a versioned operation or assertion.

## Plugin structure

The Codex plugin manifest registers the `skills/` directory, so each child is
discovered as a separate skill:

```text
skills/
├── openada/                       # execution and evidence adapter
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   └── references/
├── review-circuit-simulation/     # one-run evidence review
├── characterize-analog-block/     # application-aware coordinator
├── analyze-feedback-stability/    # loop-evidence workflow
├── analyze-spectral-linearity/    # waveform-derived spectrum review
└── assess-pvt-and-yield/           # campaign accounting workflow
```

Every workflow directory contains `SKILL.md` and `agents/openai.yaml`;
`characterize-analog-block` carries application-recipe and implemented-intent
routing references; `analyze-spectral-linearity` carries a standards/method
scope reference.

Use `skills/<lowercase-hyphen-name>/SKILL.md` for the workflow. Add
`agents/openai.yaml` for discoverability. Add `references/` only when detailed
material must be loaded on demand, and add `scripts/` only for deterministic
helpers that are genuinely reusable. Do not put driver implementations or
protocol schemas inside a skill.

Installing the OpenADA plugin installs all shipped skills. Other
Agent-Skills-compatible harnesses can register the same directories. The CLI
and JSON contracts remain portable even when a harness does not support skills.

Plugin skill names are namespace-qualified by the plugin:

- Codex uses `$openada:openada`, `$openada:characterize-analog-block`, and the
  corresponding `$openada:<skill-name>` IDs.
- Claude Code uses `/openada:openada`, `/openada:characterize-analog-block`, and
  the corresponding `/openada:<skill-name>` commands.

For a standalone skill-only installation, copy the desired skill directories
under `~/.agents/skills/`. Standalone discovery is harness-managed and does not
create the plugin namespace.

## Initial catalog and maturity

| Skill | Kind | Status | Contract dependency |
|---|---|---|---|
| `openada` | Execution and evidence adapter | preview | Current CLI and result contract |
| `review-circuit-simulation` | Tool-independent engineering workflow | experimental | `circuit.simulate/v1alpha2` |
| `characterize-analog-block` | Application-aware workflow coordinator | experimental | Immutable intent-ledger composition from simulation through verified series, ordinary/spectral/AC-transfer measurement, and specification |
| `analyze-feedback-stability` | Feedback-loop evidence workflow | experimental | AC Cartesian extraction → `result.transfer.measure/v1alpha1` for first-frequency gain, -3 dB bandwidth, unity frequency, and explicitly declared negative-feedback phase margin |
| `analyze-spectral-linearity` | Waveform-derived spectral review | experimental | `circuit.simulate/v1alpha2` → `result.series.extract/v1alpha1` → coherent `result.spectral.measure/v1alpha1` → specification |
| `assess-pvt-and-yield` | PVT/statistical campaign workflow | experimental | Repeated capability-gated simulation → verified series extraction → ordinary/spectral/AC-transfer measurement → specification intents |

Track skill maturity separately from driver maturity. **Experimental** means
the workflow boundary and prompts are open for refinement. **Reviewed** should
require realistic forward tests, failure/unknown cases, and review by another
engineer. **Validated** should additionally require repeatable public task
fixtures demonstrating that the skill stays inside its declared contract
boundary. None of these labels imply foundry signoff.

The analog workflows deliberately treat measurement and specification
operations as capability-gated. Their plans remain useful when those profiles
or a requested metric are unavailable, but the affected row stays `not
evaluated`; skill prose cannot manufacture a semantic capability. For the
implemented evidence path, `measure`, `spectral`, and `transfer` accept a
complete passing extraction envelope directly, so a skill need not invent a
second normalized-series serialization step. `openada profile list/show`
provides the packaged identities; profile presence still does not imply an
external-provider mapping.

The feedback-stability skill must preserve the transfer profile's narrow
semantics: “low-frequency gain” is the first positive simulated frequency,
phase margin requires an explicitly reviewed negative-feedback loop-gain
interpretation, and gain margin or multi-crossing selection remains not
evaluated rather than inferred from the trace.

Good next candidates, after their required semantic profiles exist, are:

- diagnose a DRC result without confusing rule-deck output with signoff;
- review and route an LVS mismatch;
- assess a design change by comparing pre/post evidence and lineage;
- plan an open-source RTL-to-layout evidence chain.

Keep each skill narrow enough that its engineering question, required evidence,
and stop conditions can be reviewed independently.

## Contribution gate

A proposed engineering skill is ready to ship when:

1. its trigger description names concrete engineering tasks;
2. the workflow uses OpenADA semantic operations rather than native tool CLIs;
3. pass, fail, unknown, invalid, and unavailable paths are explicit;
4. at least one realistic success case and one uncertainty or failure case
   have been forward-tested;
5. the same instructions work unchanged across two backends whenever the
   contract advertises two mappings;
6. repository skill and plugin validation pass.

Reviews should ask whether the skill improves the next engineering decision,
not whether it contains the largest possible flow. Small composable skills are
preferable to one universal “design a chip” prompt.
