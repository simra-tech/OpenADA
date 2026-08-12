# Autonomous-oscillator phase-noise methodology options

Status: decision input, not an OpenADA operation or a frozen method

Issue: simra-tech/OpenADA#7

Backend examined: ngspice-46

Last evidence review: 2026-08-12

## Decision to be made

The analog owner must choose and ratify a complete observation model before an
autonomous-oscillator phase-noise primitive can be versioned. This memo surveys
the methods that are defensible, conditionally useful, or unavailable in the
required ngspice-46 environment. It is accompanied by a working
[`phase_noise.py`](../../evaluation/pn-methodology/phase_noise.py) prototype,
[`synthetic-closure.json`](../../evaluation/pn-methodology/synthetic-closure.json),
and [measured cost evidence](../../evaluation/pn-methodology/cost-results.md).
Those artifacts make the recommendation evidence-based; they do not themselves
freeze a method or authorize a primitive.

The decision is not whether an FFT can display a skirt. It is whether two
implementations, given the same waveform and method declaration, will produce
the same phase-error record, density estimate, offset value, and uncertainty—or
will both reject the evidence for the same reason.

This is a methodology gate, not a specification decision. The source paper's
reported `-100.8 dBc/Hz at 1 MHz` is context only. It is not a limit and is not
evidence that ngspice models the same noise mechanisms.

The current OpenADA boundary is explicit:

- [`result.spectral.measure/v1alpha1`](../../profiles/result.spectral.measure-v1alpha1.json)
  implements coherent rectangular-bin SNR, SINAD, THD, and SFDR only. It
  rejects PSDs, windowed/averaged spectra, jitter, and phase noise.
- [`MEASUREMENT_METHODS.md`](../MEASUREMENT_METHODS.md) already requires a
  future jitter method to freeze its reference/event model, interval and
  bandwidth, detrending/wander policy, statistic, and decomposition
  assumptions.
- Typed `circuit.simulate/v1alpha2` has no noise-analysis capability. Native
  control-deck research below is therefore outside the current typed evidence
  chain.

## Recommended ruling at a glance

Ratify Candidate A's crossing-phase/Welch architecture as the direction for a
future point-PN method and Candidate B as mandatory correlation evidence, but
do **not** freeze a scored method or authorize a versioned primitive yet. Two
blocking decisions have no defensible stock-ngspice default: the physical
device-to-transient-noise mapping and a reproducible random-sequence policy.
Keep `phase_noise_1m` report-only with a null value until both blockers and
every parameter and validity gate in the owner checklist are ratified,
implemented, and validated. The [ranked recommendation](#ranked-recommendation),
[owner checklist](#frozen-method-checklist-awaiting-owner-ruling), and
[interim payload](#interim-report-only-bench-row) below state the exact ruling
surface.

## Definitions shared by the candidates

Write the oscillator output as

```text
v(t) = A(t) sin(2 pi f0 t + phi(t)).
```

For positive offset frequency `f`, let `S_phi(f)` be the one-sided phase PSD in
`rad^2/Hz`. It contains the contribution represented by both physical
sidebands. The conventional linear single-sideband phase-noise quantity and
its logarithmic form are

```text
L_linear(f) = S_phi(f) / 2
L_dBc_per_Hz(f) = 10 log10(S_phi(f) / 2).
```

The factor of one half and `10 log10`, rather than `20 log10`, are normative
choices. NIST uses this convention for single-sideband phase noise
([NIST SP 250-90, symbol table](https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication250-90.pdf)).
Interpreting `L(f)` literally as the power in one RF sideband per hertz divided
by carrier power uses the small-phase-modulation approximation. A method must
therefore report integrated phase over a declared band and ratify a
small-modulation bound or correction before making that physical power-ratio
interpretation. The `S_phi/2` phase-noise quantity remains the reported
convention; the extra gate prevents an overclaim about its RF measurement
interpretation.

With time deviation `x(t) = phi(t)/(2 pi f0)` and fractional frequency
`y(t) = dx/dt`, the one-sided densities obey

```text
S_x(f)   = S_phi(f) / (2 pi f0)^2
S_y(f)   = (f/f0)^2 S_phi(f)
S_phi(f) = (f0/f)^2 S_y(f).
```

Integrated RMS timing jitter is meaningful only with declared integration
limits:

```text
sigma_t[f_lo, f_hi]
  = sqrt(integral(S_phi(f), f_lo, f_hi)) / (2 pi f0).
```

These definitions follow the frequency-stability framework in
[NIST SP 1065](https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication1065.pdf).
IEEE 2414-2020 is useful terminology context, but its public scope page does not
select an implementation for OpenADA
([IEEE 2414-2020](https://standards.ieee.org/ieee/2414/5935)).

## ngspice-46 capability audit

The inspected host binary is
`/home/specialpedrito/.local/bin/ngspice`, SHA-256
`c0b252f4b91a030abf210062e8383975786bfb6d0718e064a3b2265e62310be7`.
`ngspice --version-full` identifies ngspice-46, KLU, and XSPICE.

The primary reference is the
[ngspice-46 user manual](https://ngspice.sourceforge.io/docs/ngspice-46-manual.pdf).
The following table separates runnable features from names that appear in the
manual but are not usable in this build.

| Native surface | v46 status | Relevance to autonomous PN |
|---|---|---|
| `.tran` plus `TRNOISE(...)` | Runnable, experimental | Can propagate explicitly authored stochastic voltage/current sources through a nonlinear oscillator. Manual section 11.3.11 has stale text saying current sources are unavailable, but a live v46 current-source probe produced stochastic output. It does not automatically enable compact-model device noise. |
| `TRRANDOM(...)` plus behavioral/filter sources | Runnable | Can build explicit stochastic macromodels, but supplies no physical mapping or PN calibration by itself. |
| `.noise` | Runnable small-signal analysis | Linearizes about a DC operating point and needs an input source. It does not find the oscillator's periodic orbit or noise-to-phase conversion. |
| `psd ave vector` / `fft` | Runnable postprocessing | Operates on waveform values, not oscillator phase. Its native smoothing does not freeze Welch overlap, effective degrees of freedom, or a PN uncertainty model. |
| `.pss` | Not available | Manual section 11.3.12 says the experimental code is not publicly available. A live `.pss` deck returns `unimplemented dot command '.pss'`. |
| HB, PAC, PNoise | Not available | No runnable commands or harmonic-balance/periodic-noise analysis are present. Live `.hb` also returns `unimplemented dot command '.hb'`. |

### What `TRNOISE` actually supplies

Manual sections 4.1.7 and 11.3.11 define

```text
TRNOISE(NA NT NALPHA NAMP RTSAM RTSCAPT RTSEMT)
```

on an independent voltage or current source used by ordinary `.tran`:

- `NA`: Gaussian sample RMS amplitude in volts or amperes;
- `NT`: noise update interval; breakpoints are forced at its multiples;
- `NALPHA`, `NAMP`: `1/f^alpha` exponent and amplitude;
- `RTSAM`, `RTSCAPT`, `RTSEMT`: random-telegraph amplitude and mean capture
  and emission times.

White, colored, and telegraph terms may be combined. `set notrnoise` disables
all such sources. The manual describes this as an experimental, low-frequency
facility and expressly lists unresolved calibration, timestep, numerical-floor,
device-noise-generation, and applicability questions.

White samples force breakpoints at `NT`. The documented Kasdin colored-noise
path also preallocates roughly `tstop/NT` values rounded up to a power of two,
so long close-in records can incur a separate memory cliff even before circuit
state and waveform retention.

In particular, `TRNOISE` does not translate the thermal, shot, or flicker noise
inside a compact transistor model into a noisy transient. The manual's shot
noise example authors a unit-noise source and scales it with a behavioral
expression. A physically scored LC-VCO therefore needs an owner-reviewed
noise-source manifest; adding `TRNOISE` at a convenient node without that
mapping measures the chosen injector, not transistor phase noise.

There is also a seed blocker in this installed build. The manual says that
`setseed nn` in `spinit` or `.spiceinit` should reproduce a sequence. Two
isolated, byte-identical ngspice-46 runs loaded the same `.spiceinit` containing
`setseed 12345` before loading the same white `TRNOISE(1m 10p 0 0)` deck. They
still produced different waveform hashes; their last values at `50 ps` were
`1.60407 mV` and `86.6490 uV`. A third run with seed 54321 was distinct, and
each process printed an init-loaded marker. This is stronger than a
control-block placement test: it exercises the documented startup placement
and a different-seed control. The deck/init/output digests and logs are
retained in
[`ngspice-capability-probes.json`](../../evaluation/pn-methodology/ngspice-capability-probes.json).
Until this automated replay test passes, the documented control must not be
represented as a deterministic seed contract.

## Candidate A: noisy transient, crossing phase, and phase PSD

### Exact candidate recipe

1. **Bind the noisy model.** Retain the exact netlist, model libraries,
   temperature, supplies, startup stimulus, ngspice binary, and init files.
   Add a manifest for every explicit stochastic source: source/injection node,
   physical mechanism, formula, units, `TRNOISE` or behavioral parameters,
   update interval/bandwidth, correlations, and temperature/bias dependence.
   Compact-model `.noise` availability is not evidence of transient-noise
   coverage. A behavioral oscillator is allowed for method closure and cost
   work, but cannot validate a transistor oscillator's physical PN.
2. **Freeze the random sequence policy.** A method needs an explicit seed list,
   PRNG identity, replay test, number of independent runs, and aggregation
   rule. Average PSDs in linear units, then convert to dB. The present v46
   `TRNOISE` replay failure makes this gate fail for a scored primitive unless
   the noise is generated by a deterministic, provenance-bound external
   sequence or the backend defect is repaired.
3. **Run through startup and retain exact waveform evidence.** Start noise at a
   declared time, retain a declared settling interval, and analyze one
   continuous post-startup record. Freeze `.tran` output step and `tmax`
   separately from `TRNOISE` `NT`: `tmax` controls oscillator/crossing fidelity;
   `NT` controls noise bandwidth and forced breakpoints. The owner proposal is
   `TMAX = T0/40`, checked at `T0/80`, and a source-specific `NT` followed by a
   bandwidth/update convergence study. These are not calibration facts. Bind
   the actual adaptive timestamps and saved native vector values as
   authoritative—do not infer them from requested steps. Freeze the saved
   vectors and exact differential
   expression, raw encoding/precision, disabled waveform compression or
   decimation, crop endpoint inclusion, and SHA-256 of the retained artifact.
   Do not `linearize` or otherwise resample before crossing extraction.
4. **Extract one event per cycle.** Use one named differential or single-ended
   probe, one threshold, and one polarity. For adjacent retained samples
   bracketing the threshold, linearly interpolate

   ```text
   t_cross = t_i + (t_(i+1)-t_i)
                     * (V_threshold-v_i)/(v_(i+1)-v_i).
   ```

   Reject nonmonotone time, nonfinite values, too-small crossing slope,
   missing/double crossings, phase slips, insufficient amplitude, or a record
   with fewer events than required. Rising-only differential zero crossings
   are the initial candidate; falling-polarity and threshold perturbations are
   systematic-sensitivity checks, not extra averages.
5. **Remove only nuisance carrier terms.** Apply the declared analysis-block
   policy, index exactly its accepted crossings `k=0...N_used-1`, and fit one
   affine ephemeris over that whole block:

   ```text
   [a, T_hat] = arg min sum_k (t_k - a - k T)^2
   r_k        = t_k - a - k T_hat
   phi_k      = -2 pi r_k / T_hat.
   ```

   Keep `phi_k` unwrapped and regard it as sampled at `Fs_phase = 1/T_hat`.
   The global fit removes initial phase and mean carrier frequency. Use only
   the exact per-segment arithmetic-mean removal specified in step 6.
   Segment-wise linear or polynomial detrending is forbidden because it
   suppresses close-in noise.
   A deterministic frequency ramp must fail stationarity or be reported with a
   separately frozen drift statistic; it is not silently fitted away.
6. **Estimate a one-sided phase density.** Freeze an even, power-of-two segment
   length `M`, a positive segment count `K`, and integer hop `H=M/2` before the
   run. Require at least

   ```text
   N_used = M + (K-1) H
   ```

   primary events, one immediately following valid crossing for the mandatory
   period-error correlation, and the waveform samples needed to bracket all
   crossings. Select exactly one declared consecutive primary block of
   `N_used` events; the proposal is the earliest complete block after the
   startup crop. Emit the observed, leading-discarded, analyzed,
   correlation-guard, and trailing-discarded event counts.
   Do not opportunistically add a `(K+1)`th segment when a long acquisition has
   extra events, and never zero-pad a partial segment. Start the `K` segments at
   analyzed event indices `0,H,...,(K-1)H`. Before windowing, subtract the
   arithmetic mean of the unwindowed phase values in each segment. Freeze the
   *periodic* Hann window exactly:

   ```text
   w[n] = 0.5 - 0.5 cos(2 pi n/M),  n = 0 ... M-1.
   U    = sum_n w[n]^2.
   ```

   For each segment's DFT `X_j[k]`, use

   ```text
   P_j[0] = |X_j[0]|^2 / (Fs_phase U)
   P_j[k] = 2 |X_j[k]|^2 / (Fs_phase U), 0 < k < M/2
   ```

   and do not double a present Nyquist bin. Average `P_j` and independent
   seeds in linear `rad^2/Hz`. This scaling already includes the window's
   power normalization; do not apply a second ENBW correction. Periodic Hann
   has bin spacing `delta_f = Fs_phase/M` and ENBW `1.5 delta_f`.
   [Welch's method](https://doi.org/10.1109/TAU.1967.1161901) motivates the
   modified-periodogram average; the precise choices above would be owned by
   OpenADA.
7. **Bind requested offsets to bins.** Choose the admissible bin minimizing
   `abs(f_req - k delta_f)`, with the lower-frequency bin winning an exact tie,
   and emit both `f_req` and `f_actual = k delta_f`. This avoids
   language-dependent half-integer rounding. Do not interpolate logarithmic
   values. The owner must ratify a mismatch tolerance and the first usable bin
   `k_min`; initial closure hypotheses are at most 5% mismatch and `k_min = 4`
   for white-PM and `1/f^2` phase tests. Bin zero, bins below `k_min`, and
   offsets at or above the one-event-per-cycle Nyquist limit are invalid.
   Choose the common integer `k` once using
   `delta_f_ref=mean_r(1/T_hat,r)/M` and the same lower-tie rule. Because each
   seed has its own `T_hat`, emit every `f_actual,r = k/(M T_hat,r)`, their
   arithmetic mean and spread, and gate every requested/actual mismatch plus
   the inter-seed spread.
   Never interpolate or average densities from materially different offsets.
   The one-event/cycle observable also aliases phase above `Fs_phase/2`; freeze
   whether that folding is part of the claimed timing observable, the modeled
   source cutoff/update/interpolation, and a cutoff/`NT` convergence gate.
   Discrete tones must be tagged under an explicit spur policy rather than
   silently pooled with random PN.
8. **Report density and uncertainty.** Emit `S_phi`,
   `10 log10(S_phi/2) dBc/Hz`, segment/bin ledger, and uncertainty. For one
   record with `K` 50%-overlapped periodic-Hann segments, use the locally white
   Gaussian approximation

   ```text
   nu_seed = 36 K^2 / (19 K - 1).
   ```

   For `R` independent, equal-weight records having equal `K`, set
   `nu=R nu_seed` and let `S_hat` be their linear PSD mean. At confidence
   `1-alpha`, the approximate interval for the true density is

   ```text
   [nu S_hat / chi2_quantile(1-alpha/2, nu),
    nu S_hat / chi2_quantile(alpha/2, nu)].
   ```

   Convert the estimate and both density bounds to dB only after all linear
   averaging. At `K=8,R=1`, `nu=15.258` and the 95% true-density interval is
   `-2.612/+3.754 dB` relative to the estimate. At `K=8,R=8`, `nu=122.066`
   and it is `-1.024/+1.163 dB`. This is not exact close-in uncertainty for
   phase diffusion: unwrapped oscillator phase is nonstationary, the global fit
   makes the lowest bins Brownian-bridge-like, and red spectra correlate
   windowed estimates. The owner proposal therefore also computes a
   10,000-replicate percentile bootstrap in which each replicate draws `R` of
   the `R` seed-level linear densities with replacement and averages them,
   using NumPy PCG64 seed 0. Take the 2.5th/97.5th percentiles with NumPy's
   `method="linear"`, then use the convex hull of that interval and the
   chi-square interval for the validity-width gate. The bootstrap algorithm,
   library version, and seed are method provenance, not hidden implementation
   choices.
9. **Correlate, do not self-certify.** Use the main block's `a,T_hat`, define
   `x_N=t_guard-(a+N_used T_hat)`, and form `N_used` consecutive period errors
   from the main crossings plus that one following guard crossing. This gives
   the period PSD the same
   `M,K,H` ledger as the direct phase PSD. Apply the exact sampled transfer
   function and freeze the comparison bins/statistic and finite-window
   tolerance. Emit period/cycle jitter and Allan-family rows;
   they become quantitative model gates only when the noisy model supplies a
   corresponding expected statistic and uncertainty. Also compare
   halves/quarters of amplitude, carrier, and density under a frozen stationarity
   rule. A failed quantitative gate makes PN unknown.

### What it measures

This method measures the PSD of event-timing phase for a declared waveform
probe and crossing definition, after removal of one fitted mean frequency. It
can isolate timing phase from a raw carrier spectrum and can represent
free-running phase diffusion over a finite record. It is not automatically the
oscillator's unique isochronal phase, and it is only as physical as the noisy
transient source model.

### Known failure modes

- No or incomplete transient-noise mapping from compact-device mechanisms;
- uncalibrated `TRNOISE` amplitude/update bandwidth or missing source
  correlation;
- non-replayable backend random sequences;
- amplitude noise, DC shift, or waveform distortion converted to crossing
  motion;
- threshold, polarity, probe, interpolation, and solver-timestep sensitivity;
- missed/double events, phase slips, multiple modes, beating, or startup in the
  analysis crop;
- event sampling aliases phase modulation above roughly `f0/2`;
- global detrending and Hann leakage bias the lowest bins of steep red spectra;
- carrier drift/nonstationarity invalidates ordinary PSD confidence formulas;
- an offset assigned to a different DFT bin or a spur smeared by the window;
- a favorable single seed or dB-domain averaging;
- numerical noise floor mistaken for circuit noise.

### Expected cost at 2.4 GHz

The carrier period is `416.667 ps`. With the proposed
`TMAX=T0/40=10.417 ps`, the solver-interval lower bound is 40 per carrier cycle
before Newton iterations, rejected timesteps, noise breakpoints, and startup.
The requested `TSTEP=T0/20` does not loosen this explicit `TMAX` bound.

For 50%-overlapped Hann, `K=8`, and first usable bin `k_min=4`, a target
lowest offset requires

```text
T_segment >= k_min/f_min
T_record  >= (K+1) T_segment / 2 = 18/f_min.
```

| Lowest usable offset | Welch support | Carrier cycles | `T0/40` interval lower bound |
|---:|---:|---:|---:|
| 1 MHz | 18 us | 43,200 | 1.728 million |
| 100 kHz | 180 us | 432,000 | 17.28 million |
| 10 kHz | 1.8 ms | 4.32 million | 172.8 million |
| 1 kHz | 18 ms | 43.2 million | 1.728 billion |

These are continuous-geometry planning bounds; the `T0/80` convergence run
doubles them, and actual adaptive rows may be more numerous still.

The bench context is about 12 seconds for a 500 ns deterministic transient.
Purely linear extrapolation gives about 7.2 minutes per 18 us seed, 72 minutes
per 180 us seed, and 12 hours per 1.8 ms seed. Those are optimistic planning
figures, not measurements: `TRNOISE` adds forced breakpoints and colored-noise
storage, and nonlinear solver cost need not scale linearly. Eight independent
seeds multiply simulator cost again.

The prototype additionally requires power-of-two `M`. At nominal 2.4 GHz,
`K=8`, and `k_min=4`, a 1 MHz request within the candidate 5% bin tolerance
needs `M=16384`, 73,728 analyzed phase events, and 30.72 us of conventional
Welch support, mapping nominally to bin 7 at 1.02539 MHz. A 100 kHz request
needs `M=262144` and 491.52 us to map nominally to 100.708 kHz. A 10 kHz
request needs `M=2097152` and 3.93216 ms to map nominally to 10.300 kHz. Actual
bin spacing is `(1/T_hat)/M`, not `f0/M`. Acquisition must continue until the
fixed block, one following correlation-guard crossing, and their interpolation
brackets exist, so startup, crop phase, endpoint guards, and frequency
variation add simulator time. Exact discrete geometry must replace the
continuous lower bound when scheduling a run.

The captured synthetic analyzer reached 4,718,592 analyzed phase events and a
nominal 9.155 kHz first usable offset in 0.555 s median for the candidate path.
In contrast, the PDK-free behavioral ngspice specimen took 3.45 s for 0.5 us
and 102.05 s for 18 us, including ASCII evidence export; the latter produced
4.47 million adaptive rows and still only supported `M=8192`, or a 1.172 MHz
first usable offset. This behavioral source is cheaper than a transistor
oscillator and is not a physical-PN model. Simulation and ASCII artifact
handling dominated the measured behavioral workflow; that dominance is
expected but remains unmeasured for a transistor VCO.

A 500 ns whole record has only 2 MHz raw bin spacing before averaging. Under
the `K=8`, `k_min=4` policy its first usable offset would be about 36 MHz. The
bench's current 50 ns late crop has 20 MHz raw spacing. Neither can support a
defensible report at 1 MHz.

### Grader validity gate

Candidate A may emit a numeric PN value only when all of these pass:

- exact backend/binary, deck, model, corner, init, and stochastic-source
  manifests are digest-bound;
- requested `TSTEP` and `TMAX`, actual time/value vectors, raw
  encoding/precision, compression policy, crop endpoint rule, saved signal
  expression, and waveform digest are bound;
- every noise mechanism, cutoff/update/interpolation rule, timing-alias policy,
  correlation, and seed is declared, calibrated, replayable, and converged;
- startup crop, probe, signal mode, threshold, polarity, and interpolation are
  fixed;
- amplitude/slope, one-event-per-cycle, no-slip/no-gap, fixed event-block/count,
  interpolation-guard, and phase-sample Nyquist checks pass;
- `tmax`, solver tolerance, threshold/polarity, and analysis-probe sensitivity
  studies remain within a ratified systematic bound;
- the global carrier fit and no-segment-linear-detrend rule are followed;
- segment length, periodic Hann, overlap, normalization, fixed `K`, `k_min`,
  per-seed bin/spread, spur, and seed-aggregation rules exactly match the
  method;
- stationarity and phase-PSD/period-PSD/Allan correlation gates pass;
- the noise-disabled numerical-floor/headroom gate passes;
- the exact parametric and empirical 95% intervals and a separate systematic
  sensitivity budget are emitted and narrower than owner-set maxima;
- integrated phase over the declared interpretation band passes the ratified
  small-modulation/correction policy before `L(f)` is described as a literal RF
  sideband-to-carrier power ratio.

Any failed or missing item yields `unknown`, not a numeric score.

## Candidate B: period jitter and Allan-style time characterization

### Exact candidate recipe

Use the same noisy-model manifest, startup crop, fixed rising crossing, event
validation, and global affine fit as Candidate A. With time errors
`x_k = t_k - a - k T_hat`, form

```text
period_error[k] = x[k+1] - x[k]
cycle_error[k]  = x[k+2] - 2 x[k+1] + x[k].
```

Report RMS period jitter and RMS cycle-to-cycle jitter only over a declared
record and after a declared mean/drift policy. For frequency stability at
`tau = m T_hat`, compute overlapping Allan variance directly from time errors:

```text
sigma_y^2(tau) =
  sum_i (x[i+2m] - 2 x[i+m] + x[i])^2
  / (2 tau^2 (N - 2m)).
```

Emit every accepted `tau`, sample count, estimator kind, and a noise-model-aware
confidence interval. Ordinary Allan variance is sensitive to linear frequency
drift; use a separately frozen Hadamard statistic when drift immunity is the
question rather than silently polynomial-detrending the source data.

Period-error PSD retains more spectral information than a scalar RMS. For
`0 < f < f0/2`, its ideal sampled relationship to timing-error PSD is

```text
S_period(f) = 4 sin^2(pi f T_hat) S_x(f)
L(f) = pi^2 f0^2 S_period(f) / (2 sin^2(pi f T_hat)).
```

That inversion is a correlation route to Candidate A, but a single RMS period
jitter number cannot be inverted into point phase noise.

Allan variance is a broad spectral filter:

```text
sigma_y^2(tau) = 2 integral_0^infinity
  S_y(f) sin^4(pi f tau)/(pi f tau)^2 df.
```

It can be converted to point `L(f)` only after a power-law noise type and
bandwidth/cutoff assumptions are frozen. Examples are

```text
white FM:   L(f) = f0^2 tau sigma_y^2(tau) / f^2
flicker FM: L(f) = f0^2 sigma_y^2(tau) / (4 ln(2) f^3)
random-walk FM:
            L(f) = 3 f0^2 sigma_y^2(tau) / (4 pi^2 tau f^4).
```

White and flicker phase noise cannot be distinguished by ordinary Allan
variance without upper-bandwidth information; modified Allan variance or a
direct spectrum is needed.

### What it measures

- Period and cycle-to-cycle RMS characterize short-term edge dispersion.
- Allan-family statistics characterize fractional-frequency stability through
  broad, tau-dependent weighting functions.
- Period-error PSD can correlate a direct phase PSD under the same sampling
  assumptions.

These are valuable oscillator-quality observables. They are not aliases for
`dBc/Hz at 1 MHz`.

### Known failure modes and assumptions

- Every noisy-model and crossing systematic in Candidate A still applies.
- RMS values conflate spectral shapes and depend on observation bandwidth.
- Dead time, missing cycles, drift, and nonstationarity bias stability
  estimates.
- Allan confidence intervals depend on record length, overlap, `tau`, and
  noise type; there is no universal error bar.
- A power-law conversion is nonunique unless slope, applicable band, and
  cutoff are independently established.
- Ordinary Allan deviation cannot separate white from flicker PM.
- `random-walk phase` means Wiener phase and gives `S_phi ~ 1/f^2` (white FM);
  it must not be confused with random-walk FM, which gives `S_phi ~ 1/f^4`.

### Expected cost at 2.4 GHz

The crossing sequence is compact and the estimator is cheap: a 500 ns record
contains about 1,200 rising events instead of tens of thousands of raw waveform
points. That record can yield a noisy short-term RMS screen and many
small-`tau` overlapping terms, but their uncertainty still depends on noise
type, `tau`, overlap, and effective degrees of freedom. It cannot resolve a
1 MHz point density under Candidate A's window/averaging policy.

The simulation itself is not cheaper if it uses the same physical noisy
transient: every carrier cycle and noise breakpoint must still be solved. The
method becomes cheaper only by accepting a shorter record and a broad
time-domain statistic, or by assuming a power-law model to extrapolate. Those
assumptions change what is measured.

### Grader validity gate

Candidate B requires Candidate A's model, startup, crossing, timestep, and
stationarity gates, plus exact statistic, tau grid, overlap/dead-time, drift,
bandwidth, and confidence rules. A direct jitter/Allan result may be reportable
when valid. Conversion to `dBc/Hz` must remain disabled unless a separately
ratified power-law classification and cutoff model pass. Unknown noise type
means `phase_noise: unknown`, not a guessed conversion.

## Candidate C: PSS/HB plus periodic noise

### Ideal recipe

In a simulator that implements it, find the autonomous periodic steady state,
linearize the time-periodic system about that orbit, solve periodic noise with
the oscillator's neutral phase mode handled explicitly, and report the
single-sideband phase-noise density at declared offsets. Bind convergence,
fundamental/orbit, harmonics, device noise, sideband count, tolerances, and
noise folding. Correlate selected points with a long noisy transient.

### Availability decision

This is normally the most direct and computationally attractive circuit-level
route. It is out for the required backend:

- ngspice-46 documents its `.pss` code as experimental and not publicly
  available;
- this binary rejects `.pss` and `.hb` as unimplemented;
- no PAC or PNoise command is present; and
- a PSS orbit alone would not produce oscillator noise density.

Expected cost and a grader gate cannot be established on ngspice-46 because no
runnable implementation exists. The validity result is
`not evaluated — capability unavailable`. Using a commercial simulator would
be a different backend contract and cannot silently define the ngspice method.

## Candidate D: other native analyses and postprocessing

### `.noise` around DC

Exact native recipe: declare an input source and output, sweep frequency, and
obtain output/input-referred stationary small-signal noise about the DC
operating point. What it measures is linearized driven-circuit noise. An
autonomous oscillator has a time-periodic large-signal orbit and a neutral
phase direction; a DC linearization captures neither. Its execution can be
valid while the oscillator-PN question remains not evaluated. A grader must
reject it for autonomous PN by analysis-kind identity.

### Carrier FFT, `fft`, or `psd ave`

Exact native recipe: uniformly resample/`linearize` a transient, apply the
declared FFT window, and inspect carrier-relative spectral power; `psd ave`
can smooth a waveform density. What this measures is voltage/current spectral
content, mixing AM, PM, distortion, startup leakage, and the carrier window.
It does not define phase extraction or SSB phase-density normalization, and
native smoothing does not expose the complete averaging/uncertainty contract.
A Blackman-window carrier FFT is therefore diagnostic only. A grader must
reject any claim that a carrier-bin ratio is PN in `dBc/Hz`.

### `TRRANDOM` or externally synthesized source sequences

`TRRANDOM` can generate uniform, Gaussian, exponential, or Poisson values at a
declared interval. Behavioral filters or a deterministic PWL/file source can
then implement a chosen colored process. This is runnable and useful for
synthetic closure or for an explicitly calibrated macromodel. It does not
derive noise from a transistor model. A grader gate is the same explicit
source manifest and calibration required by Candidate A. A provenance-bound
external sequence is presently the cleanest way to obtain deterministic replay,
but freezing it would be a new model policy, not a workaround to hide the v46
seed failure.

### Cost and grader gate

DC `.noise` and a short carrier FFT are inexpensive relative to Candidate A
and fit ordinary bench-scale runs, precisely because they do not solve the
autonomous noisy-orbit question. Their grader gate is categorical: the analysis
kind may emit its native diagnostic, but an autonomous-PN field remains
`not_evaluated`. `TRRANDOM` or a file-driven stochastic transient has
Candidate A's carrier-resolution and record-length cost and must pass the same
source-calibration, replay, crossing, PSD, and uncertainty gates. Its native
availability removes no physical-model requirement.

## Comparison for the owner

| Candidate | Produces point `L(f)` without a model conversion? | Physical transistor PN in stock ngspice-46? | Main cost | Present status |
|---|---|---|---|---|
| A. Noisy transient + crossing phase + Welch PSD | Yes, for the declared event phase | No; explicit source mapping/calibration is missing | Very long carrier-resolved transient times seeds | Prototype-worthy; physical-model and seed gates unresolved |
| B. Period jitter / Allan family | No, except period-PSD inversion or ratified power-law conversion | Same model limitation as A | Estimator cheap; noisy simulation still dominant | Useful report/correlation candidate |
| C. PSS/HB + periodic noise | Yes in capable simulators | Not runnable here | Usually much lower than long transient | Out: backend capability unavailable |
| D. `.noise` or carrier FFT/PSD | No | No | Cheap | Diagnostic only; invalid for autonomous PN |

## Prototype and measured evidence

The standalone prototype closes the event extractor and PSD normalization
against two analytically known constructions. It uses eight fixed, distinct
NumPy PCG64 seeds, 32,768 events per seed, a 10 kHz scaled carrier, 64 waveform
samples/cycle, `M=4096`, and 15 Hann segments/seed. Linear seed aggregation has
about 228.17 locally-white effective degrees of freedom.

That closure runner deliberately consumes all complete segments. The separate
cost harness exercises fixed `K=8` for the direct phase path. Neither is a
versioned implementation of the newly proposed fixed-block plus following-guard
correlation contract; that contract remains an explicit implementation and
ratification item rather than being smuggled in as already frozen.

- For white PM at a true `-60 dBc/Hz`, the recovered band-median error was
  `-0.037 dB`, fitted exponent `-0.0066`, and pointwise nominal-95% coverage
  `94.94%`. Extracted and oracle PSDs differed by at most `0.00084 dB` in the
  scored band.
- For Wiener phase/white FM at a true `-80 dBc/Hz` at 97.65625 Hz, the
  recovered band-median error was `-0.038 dB`, fitted exponent `-1.9971`, and
  coverage `95.04%`. Extracted and oracle PSDs differed by at most `0.00715 dB`.
- The same Wiener records recovered period jitter within `-0.281%`, one-period
  Allan variance within `-0.481%`, and period-PSD inversion within `-0.043 dB`
  median. This closes the prototype Candidate-B correlation calculations for
  this Wiener construction while also showing that scalar jitter/Allan values
  are not point-PN estimators.
- Changing from 32 to 64 samples/cycle moved the scored PSD by at most
  `0.0329 dB`, below the prototype's 0.05 dB regression gate.

The 18 focused extraction/closure tests plus four cost-harness tests fail
closed on nonmonotone/nonfinite waveforms, absent or wrong event counts,
missing cycles, invalid Welch shapes, forbidden offsets, duplicate seeds, short Allan records,
and nonfinite simulator output.

The explicitly driven, non-autonomous behavioral ngspice specimen independently
saved `v(phi)` as truth. Across 0.5--18 us records, crossing-extracted phase
agreed within `1.00e-5--1.58e-5 rad` RMS and the PSD within 0.00683 dB. That
closes the ngspice-to-extractor pipeline only. Separate retained capability and
replay probes establish that `.pss` and `.hb` are unavailable, current-source
TRNOISE is functional, and two startup-loaded `setseed 12345` runs do not
replay. See
[`ngspice-capability-probes.json`](../../evaluation/pn-methodology/ngspice-capability-probes.json).

## Ranked recommendation

1. **Select Candidate A as the future point-PN observation and estimator
   architecture, but do not freeze or ship it yet.** It is the only runnable
   ngspice-46 path that measures a declared event-phase density, and synthetic
   plus behavioral closure show that its extraction and normalization are
   implementable. The physical noisy-transient model and deterministic random
   sequence policy remain unresolved methodology gates. Stock TRNOISE must not
   be used for a scored transistor VCO until both are solved.
2. **Make Candidate B mandatory supporting evidence.** Emit period jitter,
   overlapping Allan rows, and period-PSD correlation from the same crossings.
   They are useful screens and consistency checks. Never convert a scalar
   jitter or Allan value to `dBc/Hz` without a separately ratified power-law
   type, applicable band, and cutoff model.
3. **Reconsider Candidate C only when a backend with autonomous PSS and
   periodic noise is actually versioned.** It may ultimately be faster and
   more physical, but ngspice-46 cannot run it. Adding such a backend requires
   a new method/version and transient correlation; it must not silently change
   Candidate A.
4. **Reject Candidate D for scoring.** DC `.noise`, a carrier FFT, native
   `psd ave`, or a Blackman skirt may remain diagnostic plots but cannot emit
   autonomous-oscillator PN.

The immediate owner ruling recommended by this memo is therefore **no numeric
score and no versioned primitive**. Ratify the estimator direction and interim
reporting contract, then commission the physical noise-source model/replay work
below. A deterministic external PWL/file sequence is the recommended replay
mechanism if ngspice-46 remains the backend, but it solves only reproducibility;
the analog owner must still approve how each physical device-noise mechanism is
mapped and calibrated.

## Frozen-method checklist awaiting owner ruling

The following is the exact decision surface. Values labeled **proposed** are
supported by this spike but are not normative until the analog owner signs off.
Items labeled **blocking selection** have no defensible default in stock
ngspice-46.

### Claim and noise model

- Method identifier/version and claim: event-timing phase PSD of one declared
  oscillator probe, not a unique isochronal phase and not signoff.
- One-sided convention `L(f)=S_phi(f)/2`, `10 log10`, units `dBc/Hz`.
- Integrated-phase band, quadrature, and small-modulation policy. **Proposed:**
  sum `S_phi[k] delta_f` for every admitted bin whose center lies in the exact
  closed `[f_lo,f_hi]` band, include separately tagged spur phase power, and
  require the modeled source to bound unresolved phase below/above that band.
  Tie `f_hi` to the source/model bandwidth rather than selecting a favorable
  narrow band. Describe `L` as a literal RF sideband/carrier ratio only when
  all included phase modulation has total variance at most `0.01 rad^2`;
  otherwise report conventional `S_phi/2` with that RF-power interpretation
  disabled.
- Exact ngspice build/binary digest, init files, netlist/model digests, process
  corner, temperature, supplies, bias/load, and startup stimulus.
- **Blocking selection:** complete device-to-transient-noise source manifest:
  thermal/shot/flicker/RTS mechanisms included and excluded, source locations,
  equations, units, bias/temperature dependence, correlations, bandwidth,
  interpolation, and calibration evidence. Compact-model `.noise` parameters
  are not automatically a transient manifest.
- **Blocking selection:** native TRNOISE after a repaired replay test versus a
  provenance-bound deterministic external sequence. If external, freeze PRNG
  family/version, generation algorithm, numeric dtype, file encoding/digest,
  update interval, interpolation, and scaling.
- Noise start time, high-frequency cutoff, update interval/interpolation,
  intended sampled-timing alias folding, number of independent sequences,
  exact seed list, and replay/distinct-seed controls. **Proposed:** exactly
  `R=8` independent equal-weight sequences, a declared seed list, linear PSD
  aggregation, and no dB averaging. The owner must choose whether source
  content above `Fs_phase/2` is intentionally included through timing alias
  folding or excluded by a model/anti-alias bandwidth rule; cutoff/`NT`
  perturbation must keep every scored point within the convergence tolerance.

### Transient acquisition and events

- Exact saved probe/expression, signal mode, units, threshold, and polarity.
  **Proposed:** one rising crossing/cycle of the declared differential output
  at zero volts, using the half-open predicate `v_i < 0 <= v_(i+1)`.
- Startup/settling criterion, noise-on time, crop start (inclusive), crop stop
  (exclusive), post-crop guard, and stationarity test. Acquisition must continue
  until `N_used+1` validated crossings plus the native samples bracketing the
  first and correlation-guard events exist; the final crossing supplies the
  `N_used`th period error and is not included in the global phase fit.
  `N_used/Fs_phase` is conventional Welch support, not a guaranteed simulator
  stop time.
- Requested `.tran` `TSTEP`, explicit `TMAX`, integration method/order,
  tolerances, initial-condition policy, and convergence perturbations.
  **Proposed initial acquisition:** `TSTEP=T0/20`, `TMAX=T0/40`, compression
  disabled, followed by a `TMAX=T0/80` scored-offset check whose change must be
  at most 0.5 dB. These are fidelity settings, not physical-noise bandwidth.
- Authoritative adaptive timestamp/value retention, saved-vector list, binary
  or at least 17-digit ASCII encoding, no decimation/compression, crop endpoint
  rule, artifact byte count, and SHA-256. If crossings are streamed, freeze
  chunk size and boundary carry behavior and retain an auditable source digest.
- Adjacent-sample linear crossing interpolation and minimum amplitude/slope
  gate. The numeric slope/amplitude limits are **awaiting owner selection**.
- Missing/double-event and phase-slip policy. **Proposed:** reject rather than
  repair; require every crossing interval to lie in `0.5--1.5 T_hat`, then
  tighten from measured oscillator behavior if appropriate.
- Event-block policy. **Proposed:** after the inclusive/exclusive crop, analyze
  exactly the earliest consecutive `N_used=M+(K-1)M/2` valid events; fit phase
  only on that block; reserve the next crossing as the period-correlation
  guard; emit observed, leading-discarded, analyzed, guard, and
  trailing-discarded event counts. Extra events never create an unrequested
  ninth segment.
- Threshold, rising/falling polarity, output-probe, solver-tolerance, `TMAX`,
  and noise-update/bandwidth systematic sweeps; **proposed:** each
  scored-offset change no larger than 0.5 dB, reported separately from
  statistical uncertainty.

### Phase and PSD estimator

- One exact-analysis-block OLS affine ephemeris, unwrapped
  `phi_k=-2 pi (t_k-a-k T_hat)/T_hat`, no polynomial detrend and no use of
  discarded events in the fit.
- Segment detrend; **proposed:** subtract the unwindowed arithmetic mean of each
  segment and do no segment-wise linear detrend.
- Even power-of-two segment length `M`, fixed `K`, analyzed count
  `N_used=M+(K-1)M/2`, periodic Hann exactly as defined above, hop `M/2`,
  segment starts `0,M/2,...,(K-1)M/2`, and no opportunistic segment, partial
  segment, or zero padding.
- Welch segment count; **proposed:** `K=8` per seed. Combined with eight
  independent seeds this gives about 122 locally-white effective degrees of
  freedom and an approximate 95% true-density interval near
  `-1.02/+1.16 dB`, before red-noise and systematic qualifications.
- One-sided `rad^2/Hz` scaling, no second ENBW correction, ENBW `1.5 delta_f`,
  linear segment/seed averaging, then the single conversion to dB.
- First usable bin; **proposed:** `k_min=4`, with synthetic closure retained for
  white and `1/f^2` phase and a farther exclusion required for steeper red
  processes if closure fails.
- Offset mapping; **proposed:** nearest admissible bin, exact tie to the lower
  bin, requested/actual frequency both emitted, no dB interpolation, maximum
  mismatch 5%, and event Nyquist excluded.
- Cross-seed grid policy; **proposed:** use the same integer bin `k` in every
  seed, selecting it once from `mean_r(1/T_hat,r)/M` with the lower-tie rule;
  emit all `f_actual,r=k/(M T_hat,r)`, their mean and maximum spread, require
  every requested/actual mismatch to be at most 5%, and reject rather than
  average materially different offsets. The owner must ratify the maximum
  allowed inter-seed frequency spread.
- Target-specific record geometry at 2.4 GHz under `K=8`, `k_min=4`,
  power-of-two `M`; **proposed conventional support:** 1 MHz uses `M=16384`,
  `N_used=73728`, 30.72 us; 100 kHz uses `M=262144`, `N_used=1179648`,
  491.52 us; 10 kHz uses `M=2097152`, `N_used=9437184`, 3.93216 ms. These
  values use nominal `f0`; each run emits actual `(1/T_hat)/M`. Startup, crop
  phase, event/bracket guards, and frequency variation add acquisition time.
- Spur policy: whether each declared tone is included, excluded, or separately
  tagged; guard-bin width and emitted spur ledger are **awaiting owner
  selection**. Never silently call a window-dependent spur peak random PN.

### Validity, uncertainty, and correlation

- Statistical confidence level and model; **proposed:** 95%, equal `K` and
  equal seed weights, `nu_seed=36K^2/(19K-1)`, `nu=R nu_seed`, and true-density
  bounds
  `[nu S_hat/chi2(0.975,nu), nu S_hat/chi2(0.025,nu)]`. Convert bounds to dB
  after linear averaging. Also compute the specified 10,000-replicate PCG64-0
  percentile bootstrap, drawing and averaging `R` seed-level linear densities
  with replacement per replicate and taking 2.5/97.5 percentiles using
  `method="linear"`; use the convex hull of the two intervals for validity, and
  label both approximations for close-in red processes.
- Maximum allowed statistical interval and systematic budget; **proposed:**
  statistical 95% total width at most 2.5 dB at a scored point and each declared
  convergence sensitivity at most 0.5 dB. The owner must decide whether and
  how to combine systematic components.
- Numerical-floor control; **proposed:** run the identical acquisition and
  extraction with all declared stochastic sources disabled, retain its
  waveform/phase PSD, and require at least 10 dB density headroom at every
  scored offset. Repeat the floor at the tighter solver/`TMAX` setting. The
  owner must ratify the headroom and treatment of a zero/upper-bound floor.
- Stationarity rule for amplitude, fitted carrier, and density across
  halves/quarters; exact tolerances are **awaiting owner selection** and must
  account for the declared statistical model rather than compare raw dB
  equality.
- Mandatory same-record checks: extrapolate the main `a,T_hat` to the required
  following guard crossing, form exactly `N_used` period errors, and compute
  their PSD with the same `M,K`, periodic Hann, and segment-mean rule; invert
  its sampled transfer function and compare over bins `k_min...floor(0.1M)`
  and at each scored bin. **Proposed:** band-median absolute disagreement at
  most 0.5 dB after applying a synthetic power-law-specific finite-window bias
  bound; the owner must ratify the scored-bin tolerance and every bias table.
  Emit period/cycle jitter and overlapping Allan rows, but treat them as
  independent validity gates only when the noisy model predicts their value
  and uncertainty; otherwise they are identities/diagnostics. A failed defined
  comparison makes PN unknown and never selects the more favorable estimator.
- Candidate-B details: period/cycle RMS definitions, Allan or modified/Hadamard
  estimator, exact `m`/`tau` grid, overlap, dead time, drift policy, bandwidth,
  noise-type-dependent confidence model, and any power-law/cutoff model. No
  scalar-to-point-PN conversion is the proposed default.
- Invalid-result contract and reason codes. Any missing manifest, failed replay,
  insufficient record/bin, cross-seed grid or alias-policy failure,
  nonstationarity, event defect, numerical-floor/headroom failure, convergence
  failure, excessive uncertainty, or model/correlation failure emits
  `unknown`/`null`, never a numeric fallback.

## Interim report-only bench row

Until both blocking selections and every method parameter and validity gate
above are ratified, implemented, and automated, the existing row should emit
the following semantic payload (field spelling may be adapted to the bench
schema, but meaning must not change):

```json
{
  "metric": "phase_noise_1m",
  "intent": "osc.pnoise_1meg",
  "status": "not_evaluated",
  "value": null,
  "unit": "dBc/Hz",
  "report_only": true,
  "limit": null,
  "requested_offset_hz": 1000000.0,
  "actual_offset_hz": null,
  "uncertainty_db": null,
  "method_id": null,
  "method_candidate": "research.openada.pn-zero-crossing-welch/0",
  "reason_codes": [
    "owner_method_ruling_pending",
    "method_parameters_unratified",
    "validity_gates_unimplemented",
    "physical_transient_noise_model_unavailable",
    "deterministic_trnoise_replay_failed",
    "versioned_openada_capability_unavailable"
  ],
  "paper_context": {
    "value": -100.8,
    "unit": "dBc/Hz",
    "offset_hz": 1000000.0,
    "role": "context_only_not_a_limit"
  }
}
```

A deterministic noiseless bench run may be linked as startup/frequency context,
but its carrier FFT, the behavioral specimen's telemetry, and the paper number
must not populate `value`. This row is neither pass nor fail. It explicitly
states what is missing so a future owner ruling can replace `method_id: null`
with a versioned, reproducible method rather than silently changing semantics.
