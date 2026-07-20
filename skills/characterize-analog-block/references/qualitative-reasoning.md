# Qualitative analog reasoning

Use this workflow to analyze a supplied schematic or a stated parameter change
before selecting an EDA operation. Keep conclusions tied to visible
connectivity and explicit assumptions. Treat an unclear symbol, device type,
terminal, clock phase, or initial condition as unknown rather than silently
repairing the circuit.

## Freeze the requested observable

Write one sentence naming:

- the output quantity or trend being requested;
- the independent change;
- what remains fixed;
- the operating region, clock phase, or observation event;
- whether the requested result is initial, transient, steady-state, or
  small-signal.

Do not begin with a familiar formula until its held-fixed conditions match this
sentence. Distinguish instantaneous rate, interval duration, final value, and
settling or propagation time; they are not interchangeable observables.

## Build a topology and state ledger

Record only facts visible in the authoritative source:

| Item | Record |
|---|---|
| Devices and controlled paths | Device type, controlling terminal, and the path it enables |
| Nodes | Driven, floating, precharged, clamped, high impedance, or coupled |
| State | On, off, saturation, triode, uncertain, or changing |
| Phase boundary | Clock edge, threshold crossing, loss of saturation, or other stated event |
| Initial condition | Relevant node voltage, stored charge, or prior phase |
| Observation point | Exact node, branch current, differential quantity, or transfer ratio |

For switched or regenerative circuits, create one row per phase. Determine what
ends each phase from the circuit state rather than assuming a fixed duration.
Carry the ending state of one phase into the next.

## Propagate a change through the whole dependency chain

Start from a symbolic identity for the requested observable. For a transient
increment, use the general form

\[
\Delta x = \int_{t_0}^{t_1} \dot{x}(t)\,dt,
\]

or a stated approximation to it. Track the independent change through both the
rate and the event-defined interval. Simplify the combined expression before
claiming a trend; a parameter that slows an instantaneous rate may lengthen
the interval by the reciprocal amount and cancel from the final observable.

For every dependency, label it as increasing, decreasing, unchanged, or
unknown under the frozen conditions. Do not replace an unknown dependency with
a conventional value. If the conclusion depends on an ordering or matching
assumption, state the conditional result.

## Audit sizing and topology tradeoffs

For a device-size or component-value change, inspect all materially coupled
effects before declaring monotonic improvement:

- drive strength, transconductance, and on-resistance;
- capacitance at the observed node and at internal nodes;
- charge sharing and stored initial charge;
- voltage swing, headroom, and region transitions;
- loading of the preceding stage or clock source;
- regeneration strength, feedback polarity, and phase timing;
- noise and power when they are part of the requested observable.

Separate a local benefit from the complete-path result. Check both limits of
the changed parameter. A conclusion that predicts unbounded improvement while
a parasitic or fixed series element dominates in a limit requires an explicit
justification.

## Apply consistency checks

Before reporting the claim:

1. Check dimensions and sign conventions.
2. Substitute limiting values and compare them with physical behavior.
3. Verify continuity at region or phase boundaries when continuity is
   expected.
4. Re-read the exact observable and remove analysis of a different quantity.
5. Separate the primary conclusion from secondary effects that require
   unstated models or parameters.

## Decide whether OpenADA can add evidence

Use an OpenADA operation only when the supplied project contains an
authoritative executable target, required models and conditions, and a
supported semantic operation that measures the same observable. Use the
smallest such operation.

If those inputs are absent, retain the qualitative result as source-derived.
Describe a possible simulation only as a proposed experiment, list the missing
authoritative inputs, and do not fabricate a testbench or label the reasoning
as normalized evidence.
