---
name: analyze-spectral-linearity
description: Analyze waveform spectra and linearity with backend-independent OpenADA evidence. Use for FFT setup, coherent sampling, windowing, alias folding, SFDR, harmonic distortion, HD2 or HD3, sampled-data spurs, before/after linearity comparisons, or diagnosing an untrustworthy spectral scalar while preserving exact waveform, sampling, signal-mode, and metric provenance.
---

# Analyze Spectral Linearity

Treat spectral analysis as a measurement derived from identified waveform
evidence, not as a simulator side effect. Use `$openada:openada` for semantic operations
and normalized results; keep the exact testbench and native waveform artifact
authoritative.

## Preserve the contract layers

- Establish waveform evidence with
  `openada.operation/circuit.simulate/v1alpha2` and
  `openada.assertion/simulation.evidence.valid/v1alpha1`.
- Extract spectral metrics with
  `openada.operation/result.measure/v1alpha1` and
  `openada.assertion/measurement.valid/v1alpha1`.
- Evaluate declared limits with
  `openada.operation/specification.evaluate/v1alpha1` and
  `openada.assertion/specification.satisfied/v1alpha1`.

Inspect installed capability records and parameter schemas before forming a
request. Use semantic metric names in the plan, but do not assume the profile
implements an FFT, window, SFDR, or harmonic measurement kind. If it does not,
retain valid waveform evidence and mark the derived metric **not evaluated —
capability unavailable**. Never present an ad hoc script or backend expression
as an OpenADA measurement.

## Freeze provenance and signal meaning

Record:

- exact testbench, configuration, corner, temperature, mode, stimulus, clock,
  driver, native product/version, and run/result identity;
- native waveform artifact identity and the requested transient interval;
- source signal names and whether the analyzed signal is differential,
  common-mode, single-ended, or a declared mathematical combination;
- fundamental or clock source and how its frequency was established;
- requested metric, units, included/excluded content, and specification source.

Do not reuse a frequency, edge, clock ratio, crop interval, or signal expression
from an earlier run without reconfirming it against the current baseline.

## Establish waveform evidence

Require `openada.feature/simulation.analysis.tran/v1alpha1` and request a
transient analysis covering the intended startup and observation intervals.
Treat simulation-evidence pass only as proof that valid transient evidence
exists. It does not prove uniform sampling, coherent observation, or any
spectral metric.

Derive sampling facts from the retained waveform evidence and the measurement
definition. Keep these values distinct:

- requested simulator step or maximum step;
- observed waveform sample interval and sample rate;
- samples per signal or clock period;
- observation start, stop, duration, and startup crop;
- FFT length and bin spacing;
- coherent-cycle count;
- window type, parameters, coherent-gain normalization, and noise-bandwidth
  normalization where relevant;
- system oversampling ratio.

A requested maximum step is not an observed sample interval. Oversampling ratio
is not samples per period or FFT length.

## Define the measurement completely

Before invoking the measurement contract, declare:

1. source artifact and signal expression;
2. observation crop and handling of nonuniform samples;
3. sample rate, FFT length, bin spacing, and coherent-cycle expectation;
4. window and normalization;
5. fundamental identification rule and search region;
6. DC, fundamental-skirt, harmonic, and spur exclusion rules;
7. harmonic orders, alias-folding convention, interpolation, and output units;
8. exact metric semantics.

For harmonic order `k`, one signed-Nyquist mapping is:

```text
f_alias = abs(((k * f_fundamental + f_sample / 2) mod f_sample)
              - f_sample / 2)
```

Use that only when it matches the declared measurement convention. Preserve the
unfolded harmonic frequency, folded frequency/bin, resolution, and collision
with excluded regions.

Keep metrics distinct:

- **SFDR:** ratio of the fundamental to the largest included spur.
- **HD2/HD3:** ratio of the second/third harmonic to the fundamental after the
  declared folding rule.
- **THD, SNR, SNDR, or ENOB-like metrics:** each requires its own included-bin,
  noise, harmonic, bandwidth, and unit definition. Do not derive one from
  another unless the installed measurement contract defines that relation.

If the waveform is nonuniform, the crop is noncoherent, or window normalization
is unspecified, do not repair the ambiguity silently. Ask for the missing
choice or mark the measurement unknown/not evaluated as dictated by the
contract result.

## Evaluate and compare

Invoke the specification operation only for an explicit numeric bound with
units and conditions. A valid SFDR or HD3 measurement without a supplied limit
is an observation, not a specification pass.

For a before/after comparison, hold testbench, stimulus, signal mode, crop,
sample handling, FFT length, window, normalization, exclusions, metric
definition, and PVT condition fixed. Change one hypothesis. If any required
identity differs, report a non-comparable pair instead of an improvement.

## Route status

| State | Next action |
|---|---|
| Transient evidence pass | Attempt only measurement kinds advertised by the installed profile |
| Transient evidence fail | Diagnose the defined solver failure; no spectrum follows |
| Transient evidence unknown | Repair the waveform evidence or binding before spectral work |
| Measurement pass | Report the metric with its complete sampling and bin ledger |
| Measurement fail/unknown | Correct the declared method or source evidence; do not use a favorable plot scalar |
| Measurement capability unavailable | Keep the waveform valid but the metric not evaluated; propose the smallest missing primitive |
| Specification pass/fail | Scope the result to the exact metric, limit, and condition |

## Report

Return the frozen run identity, signal mode, waveform lineage, sampling ledger,
window/normalization, folded-harmonic table, metric definitions, contract status
at each layer, specifications actually evaluated, comparability limits, and one
smallest next experiment. Separate **observed** results from **inferred** causes
and **proposed** changes. Finish with `signoff: not claimed`.
