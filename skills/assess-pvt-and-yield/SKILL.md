---
name: assess-pvt-and-yield
description: Plan and assess deterministic PVT, sweep, mismatch, and Monte Carlo campaigns through composable OpenADA intents. Use when expanding explicit corner matrices, checking specifications across process-voltage-temperature or load conditions, accounting for pass/fail/unknown points, computing bounded raw-yield summaries, preserving seeds and sample identity, clustering failure signatures, or deciding whether nominal evidence is mature enough for a campaign.
---

# Assess PVT and Yield

Compose repeated atomic OpenADA assertions into an auditable campaign. Do not
turn a workflow summary into a new EDA truth source. Use `$openada:openada` for
capability discovery, invocation, and normalized result interpretation.

## Preserve the contract ladder

For every planned point, use:

1. `openada.operation/circuit.simulate/v1alpha2` with
   `openada.assertion/simulation.evidence.valid/v1alpha1` for each requested
   analysis;
2. `openada.operation/result.measure/v1alpha1` with
   `openada.assertion/measurement.valid/v1alpha1` for each required metric;
3. `openada.operation/specification.evaluate/v1alpha1` with
   `openada.assertion/specification.satisfied/v1alpha1` for each explicit
   metric limit.

Inspect the installed schemas and capabilities first. Do not invent a campaign,
Monte Carlo, measurement, or statistical feature ID. If a required primitive
is unavailable, preserve the affected planned points as not evaluated/unknown
and report the capability gap. Do not run raw backend commands and call their
outputs OpenADA evidence.

## Distinguish campaign types

- **Deterministic matrix:** an explicit Cartesian or curated set of process,
  model, voltage, temperature, load, mode, or other declared conditions.
- **Parameter sweep:** ordered values of one or more declared variables; record
  whether values form a Cartesian product or paired trajectory.
- **Statistical ensemble:** samples drawn under an explicit variation model,
  seed policy, sample count, and mapping from sample ID to generated conditions.
- **Yield assessment:** specification outcomes over the complete frozen point
  set. It is not synonymous with simulation completion or measurement validity.

Never mix these populations into one denominator unless the campaign definition
explicitly declares that combination.

## Freeze the campaign manifest

Before execution, record:

- campaign ID, purpose, authoritative DUT/testbench, design phase, PDK/model
  identity, and revision;
- nominal point, dimensions and exact values, corner/model-section bindings,
  units, modes, and deterministic point IDs;
- for statistical work, variation scope, global seed, per-sample identity or
  derivation rule, sample count, and excluded/invalid-sample policy;
- ordered analysis intents, required feature IDs, source signals, semantic
  measurements, and explicit specifications;
- fresh evidence destination per point/analysis and resource/time ceilings;
- resume policy, collision policy, stop conditions, and authorization for a
  potentially large campaign.

Estimate the exact point and analysis count before launch. Do not silently
expand a requested sweep, change a seed, reuse stale point outputs, or discard a
sample because execution was inconvenient.

## Gate the campaign with nominal evidence

Require a plausible nominal baseline before broad execution:

1. Run the same analysis intents intended for the matrix at the declared
   nominal point.
2. Require valid analysis evidence, valid required measurements, and evaluated
   nominal specifications.
3. Stop if the nominal point is unknown, required measurement/specification
   capabilities are unavailable, or the testbench does not represent the
   intended mode.

A valid nominal simulation with unevaluated measurements is not a campaign
gate. Ask whether the user wants a plan-only artifact or the missing primitive
implemented before consuming broad compute.

## Execute and classify each point

Run points independently with immutable point IDs. Bind every measurement to
the exact point's source artifact and every specification to the exact
measurement result. Do not reuse a nominal scalar or a measurement from another
corner.

Classify required metrics first, then the point:

- **point pass:** every required specification has valid inputs and passes;
- **point fail:** at least one required specification validly fails; retain any
  additional unknown metrics instead of hiding them;
- **point unknown:** no specification validly fails, but any required analysis,
  measurement, specification, capability, or provenance condition failed before
  specification evaluation, is unknown, unavailable, invalid, or was not
  evaluated.

Keep execution failures, terminal non-convergence, malformed evidence, invalid
measurements, and genuine specification failures as distinct reason codes.
Simulation `engineering.status: fail` means the simulation assertion's solver
failure, not an automatic specification failure.

## Summarize without improving the denominator

Let `N` be every frozen planned point, including unavailable and unknown points.
Report counts for definite pass, definite fail, and unknown, with reason-code
breakdowns. Do not remove reruns, non-convergent samples, malformed artifacts,
or unavailable points from `N`.

When useful, report:

```text
definite-pass fraction = pass / N
possible-pass upper bound = (pass + unknown) / N
evaluated-only pass fraction = pass / (pass + fail)
```

Label the last value conditional on evaluated points and omit it when its
denominator is zero. Do not call any fraction silicon yield without an explicit
population model, sampling method, required coverage, statistical treatment,
and qualified signoff process.

For a statistical ensemble, report seed/sample identity and the chosen
confidence method before quoting an interval. If no supported statistical
primitive or reviewed method is available, report raw counts and bounds only.

## Diagnose patterns conservatively

Cluster failures only by recorded facts such as failed specification signature,
corner, condition, mode, or diagnostic code. A cluster is a routing aid, not a
root-cause proof. Preserve small clusters and unknown points. Propose one
focused follow-up experiment that discriminates among plausible causes.

For reruns, retain the original outcome and create a new attempt identity. State
the deterministic reconciliation rule; never overwrite history with the most
favorable attempt.

## Report

Return:

1. frozen campaign identity, dimensions, point count, seed policy, and coverage;
2. nominal-gate result and capabilities actually used;
3. pass/fail/unknown tables at analysis, measurement, specification, and point
   layers;
4. raw fractions/bounds with exact denominators and reason-code clusters;
5. artifact lineage, retries, provenance limitations, and missing primitives;
6. one smallest next experiment or capability addition.

Finish with `signoff: not claimed`. Raw campaign accounting is evidence for an
engineering review, not a statistical or foundry signoff engine.
