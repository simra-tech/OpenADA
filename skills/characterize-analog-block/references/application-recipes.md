# Application-aware analog characterization recipes

Use these recipes as dependency maps, not universal test plans. Select only the
rows that answer the current engineering question, and bind every row to the
actual topology, technology, testbench, conditions, and approved limits. A
recipe never supplies a missing specification or project input.

## Contents

- [Common gates](#common-gates)
- [Amplifier, OTA, and transconductor](#amplifier-ota-and-transconductor)
- [Regulator and power-management loop](#regulator-and-power-management-loop)
- [Comparator and decision circuit](#comparator-and-decision-circuit)
- [Bias or voltage/current reference](#bias-or-voltagecurrent-reference)
- [Oscillator and timing block](#oscillator-and-timing-block)
- [Data converter and sampled analog front end](#data-converter-and-sampled-analog-front-end)
- [Technology and representation modifiers](#technology-and-representation-modifiers)

## Common gates

Apply these gates to every application:

1. Freeze the authoritative DUT, testbench, PDK/model revision, mode, supply,
   stimulus, load, temperature, and corner.
2. Establish nominal OP evidence before interpreting AC, transient, noise, or
   statistical behavior. Validly measure the topology-specific bias, headroom,
   common-mode, and current facts; an OP run alone does not prove they are good.
3. Preserve one operation assertion per invocation and one evidence destination
   per run.
4. Bind every measurement to exact source artifacts and every specification to
   a measurement, units, conditions, and an explicit bound.
5. Delay broad PVT or statistical campaigns until nominal behavior and the
   metric definitions are credible.

## Amplifier, OTA, and transconductor

Use this order when gain or signal transfer is central:

1. **OP:** bias currents, device operating intent, input/output common mode,
   headroom, quiescent power, and unexplained railing.
2. **DC:** input range, output range, offset-sensitive transfer, load dependence,
   and any explicit large-signal transfer limits.
3. **AC:** gain, bandwidth, poles/zeros when supported, output impedance, and
   loop behavior. Route feedback-loop claims to `$openada:analyze-feedback-stability`.
4. **Transient:** slew, settling, overload recovery, clipping, and common-mode
   response under the declared source and load.
5. **Noise or distortion:** request only when a shared semantic primitive is
   advertised. Route waveform-derived spectra to `$openada:analyze-spectral-linearity`.
6. **Variation:** apply `$openada:assess-pvt-and-yield` to frozen required metrics.

Keep open-loop and closed-loop probes distinct. Do not treat a nominal gain or
one scalar phase margin as complete multi-loop characterization.

## Regulator and power-management loop

Use this order for an LDO, switched control block, or similar regulator:

1. **OP:** reference and bias state, pass/control-device headroom, load current,
   quiescent current, and operating-mode identity.
2. **DC:** line regulation, load regulation, dropout or compliance boundary,
   current limit, and output range where applicable.
3. **AC:** loop stability across declared load and output-network conditions;
   route to `$openada:analyze-feedback-stability`. Evaluate PSRR only from an explicit
   supply-to-output transfer definition.
4. **Transient:** startup, enable/disable, line step, load step, settling,
   overshoot, undershoot, and recovery with exact edge and load conditions.
5. **Noise and protection:** treat output noise, SOA, thermal, and protection
   checks as unavailable until their required semantic operations and evidence
   are advertised; do not infer them from ordinary OP or transient evidence.
6. **Variation:** cover supply, temperature, process, load, and output-network
   variables only after nominal definitions are frozen.

## Comparator and decision circuit

Use this order for static, clocked, or regenerative decisions:

1. **OP:** reset/evaluate mode, bias state, common mode, internal railing, and
   the exact clock phase represented by the testbench.
2. **DC:** decision threshold, hysteresis, input range, and static offset only
   when a DC interpretation is valid for the topology.
3. **Transient:** delay versus overdrive, regeneration, reset recovery,
   metastability behavior, kickback at an explicitly observed node, and power.
4. **Statistical:** input-referred offset and decision yield require a frozen
   mismatch model, sampling plan, seed identity, and specification. Route to
   `$openada:assess-pvt-and-yield`.

Do not describe a finite set of overdrive or mismatch points as a universal
metastability or offset guarantee.

## Bias or voltage/current reference

Use this order when stability of a generated bias is central:

1. **OP:** all intended equilibrium states, branch currents, device headroom,
   output compliance, power, and evidence that a valid rather than parasitic
   state was reached.
2. **DC:** line sensitivity, load sensitivity, compliance, and temperature
   dependence under explicit model/corner conditions.
3. **AC:** supply rejection and output impedance from explicit transfer
   definitions; analyze any feedback loop separately.
4. **Transient:** startup over declared initial conditions, supply ramps,
   enable sequences, and recovery from disturbances.
5. **Noise and variation:** require shared noise/statistical primitives or mark
   them not evaluated; do not derive them from unrelated evidence.

One successful startup trajectory does not prove unique startup behavior.

## Oscillator and timing block

Use this order for autonomous or injection-driven timing circuits:

1. **OP:** use only to inspect bias and static state. Do not require an
   autonomous oscillator's desired behavior to appear in DC equilibrium.
2. **Transient:** startup, amplitude envelope, frequency, duty cycle, settling,
   tuning range, and supply/load sensitivity over an observation interval long
   enough for the declared metric.
3. **Spectrum:** route waveform-based harmonic or spur questions to
   `$openada:analyze-spectral-linearity` with an explicit steady-state crop.
4. **Specialized timing/noise:** phase noise, cycle-to-cycle jitter, periodic
   steady-state, and injection locking require semantic primitives that the
   basic OP/DC/AC/transient profile does not establish. Mark them not evaluated
   unless a capable versioned operation is advertised.
5. **Variation:** preserve startup failures and non-oscillating points as real
   fail or unknown outcomes according to their underlying assertions.

## Data converter and sampled analog front end

Use this order for ADC, DAC, switched-capacitor, mixer-like, or sampled paths:

1. **OP and phase context:** identify the exact clock phase, switch state,
   common mode, references, and loading represented by any static evidence.
2. **DC:** transfer, endpoint behavior, monotonicity, or code boundaries only
   when the stimulus/code sequence and measurement definition are explicit.
3. **Transient:** capture, settling, residue, decision timing, clock sequence,
   and source/load interaction with exact phase identities.
4. **Spectrum:** route FFT, harmonics, SFDR, or SNDR-like questions to
   `$openada:analyze-spectral-linearity`; freeze sampling and exclusion rules first.
5. **Variation:** apply `$openada:assess-pvt-and-yield` to explicit static or dynamic
   specifications without dropping failed conversions or missing samples.

Do not substitute simulator maximum step for observed sample interval, or
oversampling ratio for FFT length or samples per period.

## Technology and representation modifiers

- **Extracted or post-layout:** identify the exact extraction revision and
  included parasitic scope. Compare with pre-layout only through matched
  conditions, measurements, and lineage.
- **Low-voltage or body-sensitive designs:** make body domains, common-mode,
  headroom, and device operating intent explicit; do not assume bulk ties.
- **High-voltage or power devices:** ordinary simulation evidence does not
  establish safe operating area, reliability, self-heating, or aging.
- **RF or periodic systems:** S-parameters, periodic steady-state, periodic
  noise, and phase noise need explicit semantic capabilities; do not force them
  into ordinary AC or transient claims.
- **Behavioral or mixed abstraction:** record model fidelity and interface
  assumptions. A valid abstract-model result is not transistor-level evidence.
