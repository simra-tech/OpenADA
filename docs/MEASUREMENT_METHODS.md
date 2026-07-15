# Measurement methods and standards context

OpenADA names a measurement only when its observation model is closed enough
for two implementations to assign the same evidence to the same components.
`SNR`, `THD`, or `rise time` alone is not such a definition: the application,
acquisition, bandwidth, window, component exclusions, reference convention,
and operating conditions can change the result while leaving the label intact.

The implemented methods are OpenADA definitions. A standards reference records
where a method may eventually align; it is not a claim of IEEE conformity.

## Current IEEE scope map

This map records the public IEEE Standards Association scope and lifecycle
pages reviewed on 2026-07-15. Those pages do not expose the normative clauses
needed for a conformance claim.

| Application | Relevant current standard | What the public scope establishes | OpenADA status |
|---|---|---|---|
| Analog-to-digital converter | [IEEE 1241-2023](https://standards.ieee.org/ieee/1241/6797/) | Terminology and test methods for nominally uniformly sampled and quantized ADCs | Candidate context for converter records; no clause-level conformance claim |
| Digital-to-analog converter device | [IEEE 1658-2023](https://standards.ieee.org/ieee/1658/7350/) | Terminology and test methods for monolithic, hybrid, and module DACs, not encompassing systems | Candidate context for DAC-device observations |
| Digitizing waveform recorder, analyzer, or oscilloscope | [IEEE 1057-2017](https://standards.ieee.org/ieee/1057/5945/) | Terminology and test methods for waveform recorders; [P1057](https://standards.ieee.org/ieee/1057/12062/) is an active revision project | Pin the 2017 edition when this context is selected |
| Jitter and phase noise | [IEEE 2414-2020](https://standards.ieee.org/ieee/2414/5935/) | A taxonomy and modeling framework for timing error, period and cycle-to-cycle jitter, deterministic/random components, wander, BER, and phase noise | Terminology reference only; no jitter extraction operation yet |
| Two-state transitions and pulses | [IEEE 181-2025](https://standards.ieee.org/ieee/181/10551/) | Terms and algorithms for transition, state-level, pulse, and aberration measurements | Candidate for a future transition-method revision; current threshold-crossing algorithms do not claim alignment |

[IEEE 519-2022](https://standards.ieee.org/ieee/519/10677/) concerns harmonic
control in electric power systems at a point of common coupling. It is not a
generic authority for amplifier or converter THD and must not be cited that
way in OpenADA evidence.

## Alignment vocabulary

- `openada-definition`: the complete method is owned and versioned by OpenADA,
  with no external-standard alignment claim.
- `candidate`: the application lies within the public scope of the named
  standard, but the method has not passed a licensed clause-level review.
- `reviewed`: reserved for a future profile whose crosswalk records the exact
  edition, clauses, choices, deviations, reviewer, and review artifact.
- `conformant`: reserved for a future independently reviewed profile and
  conformance suite. No implemented OpenADA measurement currently uses it.

`result.spectral.measure/v1alpha1` permits `openada-definition` for a generic
sampled waveform and `candidate` for the exact ADC, DAC, or recorder editions
above. It rejects stronger labels.

## Coherent single-tone spectral method v1alpha1

The first spectral method is deliberately narrow:

- one provenance-bound finite real time series;
- 8 through 65,536 uniformly spaced samples, with a power-of-two record length;
- a caller-declared relative interval tolerance;
- a rectangular window, arithmetic-mean removal, no segments or averaging;
- a one-sided DFT represented as mean-square power per bin;
- a caller-declared fundamental exactly on a DFT bin;
- explicit harmonic orders, folded into the first Nyquist zone;
- zero-bin integration width, a closed analysis band, and collision rejection;
- clipping recorded as `not_assessed` and missing samples rejected; and
- exactly one requested scalar: SNR, SINAD, THD, or SFDR in dB.

Let `P_f` be fundamental-bin power. In the closed retained band, DC and the
fundamental are removed before the following partitions are formed:

- `P_h`: sum of the declared, folded, non-colliding, in-band harmonic bins;
- `P_n`: all remaining bins after the declared harmonic bins are also removed;
- `P_r`: all residual bins, including harmonics, noise, and spurs; and
- `P_s`: the largest residual-bin power, with the lowest frequency winning an
  exact tie.

The versioned OpenADA ratios are:

```text
SNR   = 10 log10(P_f / P_n)
SINAD = 10 log10(P_f / P_r)
THD   = 10 log10(P_h / P_f)
SFDR  = 10 log10(P_f / P_s)
```

THD is therefore a signed dB ratio (normally negative), while SNR, SINAD, and
SFDR are normally positive. The result retains compressed membership ranges,
component powers, harmonic records, the winning spur, and a SHA-256 digest of
the complete uncompressed partition. An absent fundamental is `not_found`.
A zero numerator or denominator that would require infinity remains `unknown`
with a null value; the implementation does not invent a numeric floor.

This fixed method is useful for model-free simulator regression and coherent
converter test records. It must not be substituted for a noncoherent/windowed
measurement, sine fit, PSD integration, Welch average, arbitrary-waveform
noise measurement, or hardware test whose analog bandwidth is not represented.
Current series extraction selects one native scalar voltage/current vector; it
does not sample digital edges or assemble a multibit ADC bus into codes. An ADC
candidate-context workflow therefore needs an already prepared,
provenance-bound scalar code series until those operations receive their own
versioned contract.

## Why ENOB, SNDR, and jitter are not aliases

`sinad` is the canonical implemented name. `sndr` is not accepted as an
unqualified alias because different workflows can assign harmonics, spurs,
clock feedthrough, and DC differently.

ENOB is not a generic analog metric. A future converter-specific derivation
must name an upstream SINAD/noise-and-distortion result, full-scale convention
(peak, peak-to-peak, or RMS), input amplitude, single-ended or differential
reference, and derivation variant. Campaign averaging must occur over the
defined linear power quantities before a nonlinear dB or ENOB derivation; it
must not silently average final dB or bit values.

Jitter extraction is likewise deferred. A safe operation needs an input and
clock reference, event or sine-fit model, observation interval and bandwidth,
detrending and wander policy, statistic, and decomposition assumptions. The
existence of IEEE 2414 terminology does not select those mechanics for us.

## Promotion gate for a standard-aligned method

Before changing `candidate` to a stronger alignment, contribute all of:

1. the exact licensed standard edition and a clause-level terminology and
   algorithm crosswalk, without copying restricted normative text;
2. every profile choice the standard leaves to an application or test plan;
3. declared deviations and an explanation of why they remain compatible, or a
   distinct OpenADA method when they do not;
4. independently implemented conformance vectors for coherent and
   noncoherent tones, leakage, folded and colliding harmonics, DC, in-band and
   out-of-band spurs, ties, clipping, gaps, irregular axes, and numeric limits;
5. source/partition/result digests and linear-power intermediate evidence; and
6. review by someone other than the method author.

Published method and feature identifiers are immutable. A changed component
partition, window normalization, bandwidth, alias rule, tie break, formula, or
standard meaning requires a new identifier.
