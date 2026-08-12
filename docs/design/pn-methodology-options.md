# Autonomous-oscillator phase-noise methodology options

Status: decision input, not an OpenADA operation or a frozen method

Issue: simra-tech/OpenADA#7

Backend examined: ngspice-46

Last evidence review: 2026-08-12

## Decision to be made

The analog owner must choose and ratify a complete observation model before an
autonomous-oscillator phase-noise primitive can be versioned. This memo surveys
the methods that are defensible, conditionally useful, or unavailable in the
required ngspice-46 environment. Prototype closure and cost evidence will be
added in later commits; this first revision deliberately does not turn a
candidate into a recommendation.

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
still produced different samples—for example the last value at `50 ps` was
`-85.596 uV` in one process and `-9.45917 uV` in the other. This is stronger
than a control-block placement test: it exercises the documented startup
placement. Until a retained automated replay test passes, the documented
control must not be represented as a deterministic seed contract.

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
   `NT` controls noise bandwidth and forced breakpoints. Initial planning
   values are `tmax <= T0/20` and a source-specific `NT`, followed by a
   convergence study. They are not calibration facts. Bind the actual adaptive
   timestamps and saved native vector values as authoritative—do not infer them
   from requested steps. Freeze the saved vectors and exact differential
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
5. **Remove only nuisance carrier terms.** Index accepted crossings
   `k = 0 ... N-1` and fit one affine ephemeris over the whole analysis record:

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
6. **Estimate a one-sided phase density.** Require an even, power-of-two `M` and
   define the integer hop `H = M/2`. For `N` phase events use exactly
   `K = 1 + floor((N-M)/H)` complete segments starting at event indices
   `0, H, ... (K-1)H`; reject `N < M`, ignore only the emitted trailing count
   `N - (M + (K-1)H)`, and never zero-pad a partial segment. Before windowing,
   subtract the arithmetic mean of the unwindowed phase values in each segment.
   Freeze the *periodic* Hann window exactly:

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
   Discrete tones must be tagged under an explicit spur policy rather than
   silently pooled with random PN.
8. **Report density and uncertainty.** Emit `S_phi`,
   `10 log10(S_phi/2) dBc/Hz`, segment/bin ledger, and uncertainty. For `K`
   independent averages a locally white Gaussian PSD estimate has `2K`
   chi-square degrees of freedom. With 50%-overlapped periodic Hann, a useful
   approximation is

   ```text
   nu_eff = 36 K^2 / (19 K - 1).
   ```

   At `K=8`, this is about `15.26` degrees of freedom and an approximate 95%
   interval of `-2.61/+3.75 dB` around an estimate. This is not an exact
   close-in uncertainty for phase diffusion: unwrapped oscillator phase is
   nonstationary, the global fit makes the lowest bins Brownian-bridge-like,
   and red spectra correlate windowed estimates. Synthetic red-noise closure
   plus empirical across-seed spread are required.
9. **Correlate, do not self-certify.** On the same crossings, compare the
   direct phase PSD to period-increment PSD and overlapping Allan deviation,
   and compare halves/quarters of the record. Agreement within a declared
   statistical band supports the record; disagreement makes PN unknown.

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

The carrier period is `416.667 ps`. With `tmax = T0/20 = 20.833 ps`, the
retained solver-interval lower bound is 20 per carrier cycle before Newton
iterations, rejected timesteps, noise breakpoints, and startup.

For 50%-overlapped Hann, `K=8`, and first usable bin `k_min=4`, a target
lowest offset requires

```text
T_segment >= k_min/f_min
T_record  >= (K+1) T_segment / 2 = 18/f_min.
```

| Lowest usable offset | Post-startup record | Carrier cycles | `T0/20` interval lower bound |
|---:|---:|---:|---:|
| 1 MHz | 18 us | 43,200 | 864,000 |
| 100 kHz | 180 us | 432,000 | 8.64 million |
| 10 kHz | 1.8 ms | 4.32 million | 86.4 million |
| 1 kHz | 18 ms | 43.2 million | 864 million |

The bench context is about 12 seconds for a 500 ns deterministic transient.
Purely linear extrapolation gives about 7.2 minutes per 18 us seed, 72 minutes
per 180 us seed, and 12 hours per 1.8 ms seed. Those are optimistic planning
figures, not measurements: `TRNOISE` adds forced breakpoints and colored-noise
storage, and nonlinear solver cost need not scale linearly. Eight independent
seeds multiply simulator cost again.

A 500 ns whole record has only 2 MHz raw bin spacing before averaging. Under
the `K=8`, `k_min=4` policy its first usable offset would be about 36 MHz. The
bench's current 50 ns late crop has 20 MHz raw spacing. Neither can support a
defensible report at 1 MHz.

### Grader validity gate

Candidate A may emit a numeric PN value only when all of these pass:

- exact backend/binary, deck, model, corner, init, and stochastic-source
  manifests are digest-bound;
- requested output step and `tmax`, actual time/value vectors, raw
  encoding/precision, compression policy, crop endpoint rule, saved signal
  expression, and waveform digest are bound;
- every noise mechanism, bandwidth/update interval, correlation, and seed is
  declared, calibrated, and replayable;
- startup crop, probe, signal mode, threshold, polarity, and interpolation are
  fixed;
- amplitude/slope, one-event-per-cycle, no-slip/no-gap, and phase-sample
  Nyquist checks pass;
- `tmax`, solver tolerance, threshold/polarity, and analysis-probe sensitivity
  studies remain within a ratified systematic bound;
- the global carrier fit and no-segment-linear-detrend rule are followed;
- segment length, periodic Hann, overlap, normalization, `K`, `k_min`, bin
  mismatch, spur, and seed-aggregation rules exactly match the method;
- stationarity and phase-PSD/period-PSD/Allan correlation gates pass;
- a 95% statistical interval and separate systematic sensitivity budget are
  emitted and are narrower than an owner-set maximum;
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

The edge artifact is small and the estimator is cheap: a 500 ns record contains
about 1,200 rising events instead of tens of thousands of raw waveform points.
That record can yield a noisy short-term RMS screen and a few high-confidence
small-`tau` stability values. It still cannot resolve a 1 MHz point density
under Candidate A's window/averaging policy.

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

## Comparison for the owner

| Candidate | Produces point `L(f)` without a model conversion? | Physical transistor PN in stock ngspice-46? | Main cost | Present status |
|---|---|---|---|---|
| A. Noisy transient + crossing phase + Welch PSD | Yes, for the declared event phase | No; explicit source mapping/calibration is missing | Very long carrier-resolved transient times seeds | Prototype-worthy; physical-model and seed gates unresolved |
| B. Period jitter / Allan family | No, except period-PSD inversion or ratified power-law conversion | Same model limitation as A | Estimator cheap; noisy simulation still dominant | Useful report/correlation candidate |
| C. PSS/HB + periodic noise | Yes in capable simulators | Not runnable here | Usually much lower than long transient | Out: backend capability unavailable |
| D. `.noise` or carrier FFT/PSD | No | No | Cheap | Diagnostic only; invalid for autonomous PN |

## Evidence still required before a ruling

The next commits must provide, in order:

1. a standalone phase/crossing/PSD prototype with deterministic synthetic
   closure against known `S_phi` and `L(f)`, including white PM and diffusive
   phase;
2. negative tests for ambiguous crossings, insufficient records, forbidden
   bins, and nonstationarity-sensitive choices;
3. measured analysis runtime and memory scaling versus segment/record length;
4. if feasible, a PDK-free behavioral or ideal-element ngspice noisy-transient
   specimen, explicitly labeled a pipeline/cost test rather than physical VCO
   PN; and
5. a ranked recommendation, interim report-only payload, and exact owner
   ratification checklist informed by those measurements.

Until those artifacts exist and the owner rules, the bench must continue to
represent phase noise as report-only and method-unavailable, never as a passing
or failing scalar.
