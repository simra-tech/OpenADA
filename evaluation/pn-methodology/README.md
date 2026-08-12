# Phase-noise methodology research prototype

This directory is evidence for the OpenADA#7 methodology decision. It is not a
versioned primitive, is not imported by `src/openada`, and makes no claim about
physical transistor phase noise or signoff.

`phase_noise.py` implements the candidate-A event-phase pipeline and the
candidate-B time-domain correlations described in
[`docs/design/pn-methodology-options.md`](../../docs/design/pn-methodology-options.md):

- rising-threshold crossings from authoritative, possibly adaptive native
  timestamps using adjacent-sample linear interpolation;
- one global affine crossing ephemeris and unwrapped event phase;
- a periodic-Hann, 50%-overlap, arithmetic-mean-detrended one-sided Welch PSD;
- `L(f) = 10 log10(S_phi(f)/2)` at deterministic nearest bins;
- locally-white chi-square interval bookkeeping;
- RMS period/cycle jitter, overlapping Allan variance, and period-PSD
  inversion; and
- fail-closed input, event, bin, and record checks.

The script depends only on the evaluation-local packages in `requirements.txt`.
It does not add NumPy or SciPy to OpenADA's runtime dependencies.

## Reproduce the synthetic closure

From the repository root:

```bash
python3 -m pip install -r evaluation/pn-methodology/requirements.txt
python3 evaluation/pn-methodology/test_phase_noise.py
python3 evaluation/pn-methodology/test_benchmark.py
python3 evaluation/pn-methodology/phase_noise.py self-test \
  --output evaluation/pn-methodology/synthetic-closure.json
```

The full self-test streams eight fixed NumPy-PCG64 seeds rather than retaining
all waveforms. For each seed it constructs both:

1. white event phase with `L = -60 dBc/Hz`; and
2. Wiener phase (white FM) with `L = -80 dBc/Hz` at bin 40,
   `97.65625 Hz` for the scaled 10 kHz carrier.

The construction contains 32,768 retained events, 64 waveform samples per
carrier cycle, 4,096-event Welch segments, and 50% overlap. That produces 15
segments per seed and approximately 228.17 effective degrees of freedom after
eight-seed linear PSD aggregation. A smooth `C1` cubic cycle map puts the known
events into a sampled sine waveform without introducing a slope cusp at each
crossing.

Acceptance is deliberately band-based. A single correct PSD bin has a random
chance to leave a pointwise 95% interval, so the named-bin `+/-1.5 dB` check is
a deterministic regression tolerance, not a confidence interval. The stronger
checks are median normalization, spectral slope, pointwise coverage across
hundreds of bins, extracted-versus-oracle phase/PSD agreement, 32/64/128
samples-per-cycle convergence, and period/Allan analytic correlation.

## Analyze a waveform CSV

The CSV must retain native timestamps and have a header. For example:

```bash
python3 evaluation/pn-methodology/phase_noise.py analyze \
  --input waveform.csv \
  --time-column time_s \
  --value-column vdiff \
  --value-unit V \
  --signal-mode differential \
  --signal-expression 'v(outp)-v(outn)' \
  --crop-start 1e-6 \
  --crop-stop 20e-6 \
  --expected-crossings 45600 \
  --segment-length 4096 \
  --minimum-bin 4 \
  --offset 1e6 \
  --output result.json
```

An invalid record emits `status: unknown` and a diagnostic with exit status 2.
A valid research estimate emits the source digest, exact crossing/phase/Welch
ledger, requested and actual bins, phase density, statistical interval, jitter,
and Allan rows. It still does not establish the missing physical transient-noise
model, stationarity, spur, systematic-uncertainty, or owner-ratification gates.

## Reproduce the cost and capability evidence

On a host with ngspice-46 on `PATH`:

```bash
python3 evaluation/pn-methodology/benchmark.py synthetic \
  --repeats 3 \
  --output evaluation/pn-methodology/cost-scaling.json
python3 evaluation/pn-methodology/benchmark.py probes \
  --output evaluation/pn-methodology/ngspice-capability-probes.json
python3 evaluation/pn-methodology/benchmark.py ngspice \
  --repeats 1 \
  --timeout 600 \
  --output evaluation/pn-methodology/ngspice-behavioral-cost.json
```

The ngspice command has a hard per-run subprocess timeout. If the documented
container fallback is used instead, wrap the entire Docker invocation in the
required outer `timeout` as well. Full results and their claim boundaries are
summarized in [`cost-results.md`](cost-results.md).

`behavioral-noisy-oscillator.cir` is a PDK-free pipeline/runtime specimen. It
integrates an explicitly authored TRNOISE frequency-error source, saves phase
truth, and emits a carrier. It is intentionally not autonomous and cannot
stand in for a physical transistor oscillator or validate compact-model noise.

## Scope limits

- The one-event-per-cycle phase sample rate aliases modulation at and above
  half the carrier frequency.
- The confidence interval is a locally-white Gaussian approximation. It is not
  exact in the close-in bins of a diffusive free-running phase process.
- A period-jitter or Allan scalar is not converted to point phase noise. Only
  the period-error PSD retains enough spectral information for the declared
  inversion.
- `L(f)=S_phi/2` is reported by convention. Literal RF sideband/carrier power
  interpretation additionally needs a ratified small-modulation/correction
  gate.
- Synthetic closure proves the estimator's scale and event extraction. It does
  not prove that ngspice `TRNOISE` represents compact-model device noise.
