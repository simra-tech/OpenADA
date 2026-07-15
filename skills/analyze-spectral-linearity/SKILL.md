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
- Bind exact native vectors to a real sampled record with
  `openada.operation/result.series.extract/v1alpha1` and
  `openada.assertion/series.extraction.valid/v1alpha1`.
- Extract an implemented coherent single-tone ratio with
  `openada.operation/result.spectral.measure/v1alpha1` and
  `openada.assertion/spectral.measurement.valid/v1alpha1`.
- Use ordinary time-domain scalar algorithms through
  `openada.operation/result.measure/v1alpha1` and
  `openada.assertion/measurement.valid/v1alpha1`.
- Evaluate declared limits with
  `openada.operation/specification.evaluate/v1alpha1` and
  `openada.assertion/specification.satisfied/v1alpha1`.

Inspect installed capability records and parameter schemas before forming a
request. Use `openada profile show
openada.operation/result.spectral.measure/v1alpha1` for the packaged closed
schema. The implemented spectral alpha covers SNR, SINAD, THD, and SFDR only
for its exact coherent rectangular method. A different window, noncoherent
record, sine fit, PSD, averaging, SNDR alias, ENOB, jitter, or phase-noise
question remains **not evaluated — capability unavailable**. Never present an
ad hoc script or backend expression as an OpenADA measurement.

Read [references/standards-and-methods.md](references/standards-and-methods.md)
when a metric is connected to an ADC, DAC, waveform recorder, pulse, jitter,
or IEEE standard.

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

## Choose between the implemented and a future method

Before invoking the measurement contract, declare:

1. source artifact and signal expression;
2. observation crop and handling of nonuniform samples;
3. sample rate, FFT length, bin spacing, and coherent-cycle expectation;
4. window and normalization;
5. fundamental identification rule and search region;
6. DC, fundamental-skirt, harmonic, and spur exclusion rules;
7. harmonic orders, alias-folding convention, interpolation, and output units;
8. exact metric semantics.

The implemented v1alpha1 method fixes a power-of-two record, rectangular
window, mean removal, one-sided mean-square per-bin power, no averaging, exact
coherent fundamental bin, zero-bin integration width, and fold-to-first-
Nyquist harmonics with collision rejection. For harmonic order `k`, its folding
rule is equivalent to:

```text
f_alias = abs(((k * f_fundamental + f_sample / 2) mod f_sample)
              - f_sample / 2)
```

Use the operation only when those fixed choices match the test method. Preserve
the unfolded harmonic frequency, folded frequency/bin, resolution, and
collision with excluded regions. If they do not match, request the smallest
new versioned method instead of relaxing the record.

Keep metrics distinct. HD2 and HD3 below are planning terminology only in this
release: they are not individual `result.spectral.measure/v1alpha1` outputs and
must remain **not evaluated — capability unavailable**.

- **SFDR:** ratio of the fundamental to the largest included spur.
- **HD2/HD3:** ratio of the second/third harmonic to the fundamental after the
  declared folding rule.
- **SNR:** fundamental divided by residual power after DC, fundamental, and the
  declared in-band harmonic bins are removed.
- **SINAD:** fundamental divided by every other non-DC in-band residual bin.
- **THD:** declared in-band harmonic power divided by fundamental power,
  reported as a signed dB ratio.
- **SNDR and ENOB:** not aliases or implemented derivations. Do not derive them
  unless a future installed profile names the exact relation and reference.

If the waveform is nonuniform, the crop is noncoherent, or window normalization
is unspecified, do not repair the ambiguity silently. Ask for the missing
choice or mark the measurement unknown/not evaluated as dictated by the
contract result.

## Evaluate and compare

Invoke the specification operation only for an explicit numeric bound with
units and conditions. A valid supported measurement such as SFDR or THD without
a supplied limit is an observation, not a specification pass. HD2 and HD3 stay
not evaluated in this release.

For a before/after comparison, hold testbench, stimulus, signal mode, crop,
sample handling, FFT length, window, normalization, exclusions, metric
definition, and PVT condition fixed. Change one hypothesis. If any required
identity differs, report a non-comparable pair instead of an improvement.

## Route status

| State | Next action |
|---|---|
| Transient evidence pass | Attempt only measurement kinds advertised by the installed profile |
| Series extraction pass | Confirm observed sample interval, record length, signal mode, units, and canonical series digest |
| Series extraction unknown | Repair artifact, selector, component, unit, or condition binding before any spectral calculation |
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
