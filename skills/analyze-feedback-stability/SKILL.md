---
name: analyze-feedback-stability
description: Analyze feedback-loop stability with backend-independent OpenADA evidence. Use for differential, common-mode, nested, feed-forward, regulator, or switched-loop questions involving operating-point validity, loop injection and probe identity, gain and phase crossover review, phase or gain margin, closed-loop correlation, oscillation diagnosis, or choosing one focused compensation experiment without relying on simulator-specific commands.
---

# Analyze Feedback Stability

Produce an evidence-bounded diagnosis, not a universal compensation recipe.
Use `$openada:openada` to invoke semantic operations and interpret results. Keep the
topology, prepared testbench, PDK/models, and native artifacts authoritative.

## Separate the claims

Use these exact layers:

- Establish analysis evidence with
  `openada.operation/circuit.simulate/v1alpha2` and
  `openada.assertion/simulation.evidence.valid/v1alpha1`.
- Extract loop metrics only through
  `openada.operation/result.measure/v1alpha1` and
  `openada.assertion/measurement.valid/v1alpha1`.
- Judge an explicit stability limit only through
  `openada.operation/specification.evaluate/v1alpha1` and
  `openada.assertion/specification.satisfied/v1alpha1`.

Inspect the installed schemas and capability records before constructing a
request. Do not invent a metric kind or fall back to a backend expression when
the requested measurement is not supported. Mark that metric **not evaluated**.
None of these layers alone establishes silicon, reliability, or signoff.

## Freeze one baseline

Record:

- authoritative design/testbench identity and design phase;
- supply, bias, common mode, differential stimulus, load, corner, temperature,
  and operating mode;
- enabled paths, stages, switches, feedback elements, and intended loop state;
- exact loop break or injection point, direction/sign convention, return
  probe, loop-response signals, and requested closed-loop probes;
- selected operation/feature, driver, native product/version, request/result
  IDs, and evidence destination.

Classify differential, common-mode, nested, local, global, and feed-forward
paths from authoritative connectivity evidence. Signal names and a remembered
topology are not proof. If topology, polarity, enabled state, injection, or
probe orientation remains ambiguous, ask one narrow question and stop before
quantitative margin claims.

The simulation operation consumes a prepared testbench; it does not create a
safe loop break or mutate the design. Require a reviewed testbench or a separate
authorized design/testbench-construction task. Record how the injection source
preserves the relevant DC state and loading.

## Pass the DC gate

1. Require capability
   `openada.feature/simulation.analysis.op/v1alpha1` and request nominal OP
   evidence under the frozen conditions.
2. Treat analysis-evidence pass only as proof of valid OP evidence.
3. Use the measurement contract for the topology-specific common-mode, bias,
   rail, headroom, saturation, or device-operating facts needed by the review.
4. Evaluate explicit acceptable ranges through the specification contract when
   they exist. Otherwise label the engineering acceptance as an identified
   reviewer assumption, not a specification result.

Stop quantitative stability interpretation when OP evidence fails or is
unknown, when required DC measurements are invalid/unavailable, or when the
observed state is inconsistent with the intended loop mode. Report that gate;
do not explain railing, saturation, or a floating state as phase-margin failure.

## Acquire complete loop evidence

Require capability `openada.feature/simulation.analysis.ac/v1alpha1` for the
declared frequency range. Bind the request to the same baseline and retain the
exact generated simulator input and native result artifacts.

After analysis evidence passes, request supported measurements that preserve:

- loop-response signal definition, orientation, and sign convention;
- full magnitude and unwrapped phase over the declared frequency range;
- every 0 dB unity-gain crossover, including interpolation method;
- phase at every unity-gain crossover and phase margin under the stated
  convention;
- every relevant phase crossover and corresponding gain margin;
- ambiguous, missing, or multiple crossings rather than selecting the most
  favorable scalar.

Inspect the installed measurement schema first. If it cannot express the
required crossing, phase-unwrapping, interpolation, or complex-signal
semantics, preserve the valid AC artifact but mark the margin measurement not
evaluated. A plot inspection may motivate a hypothesis, but it is not a
contract measurement or specification result.

## Correlate the closed loop

Use a separate AC or transient request, with the matching advertised feature,
for the exact closed-loop input/output and common-mode probes. Measure only
supported quantities such as settling, overshoot, ringing, or closed-loop gain
from evidence bound to the same baseline.

Do not answer a closed-loop question from an open-loop signal. Do not declare a
physical instability until the evidence distinguishes it from:

- unintended switch or operating mode;
- floating nodes, saturation, or invalid bias;
- convergence or numerical artifacts;
- model/configuration changes or simulator-version effects;
- incorrect injection, probe orientation, or measurement convention.

## Evaluate specifications and one hypothesis

Evaluate phase margin, gain margin, crossover range, settling, or other limits
only when the user/project supplied explicit bounds, units, and conditions.
Keep one result per loop and condition; one passing crossover does not cover a
multi-loop system.

When evidence supports a diagnosis, propose one change tied to one hypothesis.
Run it in a separately identified baseline, preserve the design/testbench diff,
and repeat the same OP, loop, and closed-loop evidence. Do not change topology,
compensation, or authoritative design data without authorization.

## Route status

| State | Interpretation |
|---|---|
| Analysis evidence pass | The requested native analysis is trustworthy enough for supported measurements |
| Analysis evidence fail | The solver's defined terminal failure is proven; no margin conclusion follows |
| Analysis evidence unknown | Resolve evidence, binding, configuration, or execution uncertainty |
| Measurement pass | The stated metric is valid for its exact signals and method |
| Measurement fail/unknown/unavailable | Margin or response is not evaluated; repair or add the semantic primitive |
| Specification pass/fail | Only the explicit limit at the frozen condition was evaluated |

## Report

Return the frozen baseline, loop classification, DC gate, injection and probes,
all observed crossovers, closed-loop correlation, contract status at each
layer, exact driver/artifact lineage, confounders, and one smallest next
experiment. Mark statements as **observed**, **inferred**, or **proposed**.
Finish with `signoff: not claimed`.
