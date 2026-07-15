# Standards and spectral-method routing

Use an IEEE reference only after identifying the object being tested. A
simulator waveform, ADC output code record, DAC device observation, and
oscilloscope record can carry the same metric label without sharing a complete
test method.

## Public scope references

| Object under test | Reference | Allowed current claim |
|---|---|---|
| ADC | [IEEE 1241-2023](https://standards.ieee.org/ieee/1241/6797/) | `candidate` context for an ADC record |
| DAC device | [IEEE 1658-2023](https://standards.ieee.org/ieee/1658/7350/) | `candidate` context for a DAC device; not a whole system |
| Digitizing waveform recorder/analyzer/oscilloscope | [IEEE 1057-2017](https://standards.ieee.org/ieee/1057/5945/) | `candidate` recorder context; an active P1057 revision exists |
| Jitter and phase noise terminology | [IEEE 2414-2020](https://standards.ieee.org/ieee/2414/5935/) | terminology only; no OpenADA jitter extractor |
| Two-state transition and pulse metrics | [IEEE 181-2025](https://standards.ieee.org/ieee/181/10551/) | future method-review candidate |

The public pages establish scope and edition status, not the normative metric
clauses. Do not say “IEEE compliant,” “IEEE SNR,” or “IEEE ENOB” from a
`candidate` record. IEEE 519 is a power-system harmonic-control standard and is
not a generic amplifier or data-converter THD reference.

## Route the record

1. Identify whether the source values are analog samples, quantized codes, or
   a recorder output and record their unit and full-scale convention.
2. Confirm the exact sample times. A requested simulator maximum step is not
   proof of uniform output samples.
3. Use `result.series.extract/v1alpha1` to bind a native transient voltage or
   current vector. It performs no resampling or mathematical signal expression.
4. Use `result.spectral.measure/v1alpha1` only when the record is uniformly
   sampled, power-of-two length, coherently sampled, and compatible with its
   fixed rectangular one-sided method.
5. Select generic `openada-definition` context unless the actual object lies in
   the exact ADC, DAC-device, or recorder scope. Those domains permit only
   `candidate` alignment in v1alpha1.

Extraction does not sample digital edges or assemble a multibit bus into ADC
codes. An ADC code-record workflow therefore needs an already prepared
provenance-bound scalar code series or a future versioned edge-sampling and
bus-to-code operation. Selecting the IEEE 1241 candidate context does not add
that missing capability.

The implemented partition removes DC, then assigns the fundamental, declared
folded in-band harmonic bins, remaining noise bins, all residual bins, and
SFDR candidates. SNR removes declared harmonics from noise; SINAD does not.
SFDR keeps harmonics as competitors. THD is a signed dB power ratio. Report the
partition digest and winning spur, not only the scalar.

Mark the metric unavailable when the test requires a Hann or other window,
main-lobe integration, noncoherent estimation, sine fit, PSD or bandwidth
integration, segmented/overlapped averaging, an SNDR alias, ENOB derivation,
jitter, or phase noise. State the smallest missing semantic primitive.
