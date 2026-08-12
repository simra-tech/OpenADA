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

## Oscillator transient and sweep method v1alpha1

`openada.operation/result.osc.measure/v1alpha1` is a separate profile family rather than another
scalar kind in `result.measure`. It establishes
`openada.assertion/oscillator.measurement.valid/v1alpha1`; the closed semantic implementation is
`org.openada.kernel.oscillator-evidence` version `1.0.0`. An oscillator
observation has a coupled validity decision: frequency, differential amplitude,
and supply power must come from one declared waveform crop, and a frequency is
not a result unless the waveform passes the startup and
sustained-oscillation gates. The profile is an `openada-definition` and has
three mutually exclusive modes:

- `transient` measures one provenance-bound transient record;
- `tuning_grid` composes sustained transient observations over a declared
  control grid; and
- `frequency_shift` compares declared reference and perturbed observations for
  supply pushing, load pulling, or another named perturbation.

Transient mode returns a typed oscillator verdict. `sustained` validates finite
period and frequency. `never_started` means the record never reached the
minimum amplitude and crossing qualification. `collapsed` means it qualified
as started and subsequently lost the hold criteria. `not_sustained` means
oscillatory activity was visible but no complete qualifying hold interval was
established, or a qualifying onset/window did not contain the declared count
with complete edge coverage. `multimode` is the QC outcome for beating,
two-tone behavior, or
inconsistent periods that cannot honestly be represented by one oscillator
frequency. `unknown` covers an invalid request, invalid source, or insufficient
evidence. The latter five statuses are typed non-results, with numeric values
for period and frequency left null; they are never encoded as NaN, infinity,
zero frequency, or a best-looking sub-window. Grid and shift compositions
return typed `measured` or `unknown` status and retain every input verdict. On a structurally valid transient
crop, differential amplitude and supply power remain `measured` even when the
oscillator verdict is not sustained. Their availability describes the crop; it
does not upgrade the oscillator verdict or make period and frequency valid.

### One crop, three transient measurements

The caller declares the late measurement window explicitly, including its
settle crop and integer cycle count. The result retains the source identity and
a canonical SHA-256 window identity. Frequency, differential peak-to-peak
amplitude, and average supply power in the same transient receipt all cite that
same window hash. A consumer can therefore reject a record assembled from a
frequency crop, amplitude crop, or power crop with a different source or
window, even when the reported endpoints happen to be equal.

The receipt embeds the complete normalized transient request and fixed producer
identity (`openada.operation/result.osc.measure/v1alpha1`, its oscillator
assertion, `org.openada.kernel.oscillator-evidence`, version `1.0.0`). A
consumer can therefore independently recompute the request, method, window,
and whole-receipt digests. These hashes establish exact content identity and
internal consistency, not cryptographic authorship; a workflow exposed to
hostile receipt injection must authenticate the containing prior result at its
trust boundary.

For terminal signals `a` and `b`, the measured oscillator signal is exactly
`d(t) = a(t) - b(t)`. A rising event arms only after `d` reaches the declared
negative hysteresis level. The algorithm remembers the linearly interpolated
zero time on the subsequent rising sign crossing, emits that time only after
`d` reaches the positive hysteresis level, and must re-arm below the negative
level before another event. An unconfirmed candidate is cancelled if `d`
returns to or below the negative level. Hysteresis therefore rejects chatter without
shifting the reported event from zero to a hysteresis threshold. Counting `N`
complete cycles uses the first `N + 1` qualified rising events at or after the
window start. The elapsed time between the first and last counted event gives

```text
period    = (t[N] - t[0]) / N
frequency = N / (t[N] - t[0])
```

The period sequence must also satisfy the declared consistency tolerance. The
request caps period relative deviation at 0.05 and amplitude relative deviation
at 0.20 so a caller cannot relax QC enough to hide beating. All remaining late
crossings and crop-edge coverage remain QC inputs: the first crossing must be
within one mean late period of the start and the last within 1.1 mean periods of
the stop, with the trailing allowance reserved for hysteresis confirmation.
Every complete late cycle must meet minimum amplitude. A partial tail or missing
confirmation is `not_sustained`, not positive collapse evidence; collapse
requires observed post-onset amplitude loss. These checks prevent startup
ringing or an isolated edge from becoming a frequency measurement.

Differential amplitude is `max(d) - min(d)` over the exact shared crop. Supply
power uses the declared supply-voltage and supply-current signals. Its explicit
orientation is operative: `positive_into_load` gives instantaneous consumption
as `vdd(t) * i(vdd)(t)`, while `positive_into_source` gives
`-vdd(t) * i(vdd)(t)`. The selected orientation is retained in the result. The
mean is the trapezoidal time integral divided by the crop duration, not the
arithmetic mean of samples, so a nonuniform transient time axis does not bias
the result.

Startup is assessed over its declared hold interval, not just at the first
threshold crossing. The minimum differential amplitude and period-consistency
criteria must remain satisfied for the complete hold. The result retains the
first qualifying startup time and the hold assessment when sustained; loss of
qualification after an apparent start is `collapsed`, while period structure
consistent with beating or multiple modes is surfaced as `multimode` for QC.

### Tuning grids and perturbation shifts

A `tuning_grid` request binds every control coordinate to its typed transient
observation. Every declared point must be sustained before the mode returns
numeric frequency, span, or Kvco values; an invalid point is retained as a
typed non-result, never dropped to make a shorter grid pass. The frequency span
is `max(f) - min(f)` over the complete declared grid. Adjacent frequencies are
checked for a consistent monotonic direction; a reversal remains explicit in
the result rather than being hidden by the span.

Local Kvco is a curve, never one fitted or averaged number. The declared
control coordinates must be strictly increasing. Each endpoint uses its
adjacent one-sided secant. At every interior point, the profile evaluates the
derivative of the quadratic interpolant through that point and its two
neighbors; this is the unequal-spacing central difference, not a uniform-grid
shortcut. For `h- = x[i] - x[i-1]` and `h+ = x[i+1] - x[i]`, it is

```text
Kvco[i] = -h+ / (h- * (h- + h+)) * f[i-1]
          + (h+ - h-) / (h- * h+) * f[i]
          + h- / (h+ * (h- + h+)) * f[i+1]
```

The result retains every control coordinate, local `df/dV`, and source
identity. A nonuniform control grid therefore remains nonuniform and a strongly
nonlinear curve such as roughly 33 through 130 MHz/V remains visible point by
point.

`frequency_shift` binds one sustained reference observation and one sustained
perturbed observation. It returns both

```text
signed_shift   = f(perturbed) - f(reference)
absolute_shift = abs(signed_shift)
```

along with both input identities and the named perturbation. The signed value
preserves pushing or pulling direction; the absolute value supports symmetric
limits without discarding that direction.

Phase-noise, jitter, spectral-density, and offset-frequency measurements are
explicitly outside `openada.operation/result.osc.measure/v1alpha1`. Phase noise requires its own
method and profile; this oscillator primitive must not manufacture a phase-noise
number from transient crossings.

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
