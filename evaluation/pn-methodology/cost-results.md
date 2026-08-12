# Phase-noise prototype cost evidence

Status: research evidence for OpenADA#7, not a primitive benchmark or a
physical oscillator phase-noise result

Captured: 2026-08-12

The machine-readable records are [`cost-scaling.json`](cost-scaling.json),
[`ngspice-behavioral-cost.json`](ngspice-behavioral-cost.json), and
[`ngspice-capability-probes.json`](ngspice-capability-probes.json). Their
embedded digests bind the Python estimator, benchmark script, ngspice binary,
and behavioral-deck template where applicable. Large transient waveforms were
hashed and summarized, then deleted; they are explicitly marked not retained.

## Frozen planning geometry

These measurements use a 2.4 GHz carrier, periodic Hann segments, 50% overlap,
`K=8`, and first usable bin `k_min=4`. For segment length `M`, the exact phase
event count is

```text
N = M + (K-1) M/2 = 4.5 M
record support = N/f0
first usable offset = 4 f0/M.
```

Power-of-two `M` matters. The continuous lower bound of `18/f_min` gives an
18 us record at 1 MHz, but 43,200 events support only `M=8192`; its first usable
bin is 1.171875 MHz. Under this prototype's power-of-two rule, a 1 MHz request
within 5% requires `M=16384`, 73,728 events, and 30.72 us. It maps to bin 7 at
1.025390625 MHz (`+2.539%`). Similarly, 100 kHz needs `M=262144` and 491.52 us
to map within 5% (bin 11, 100.708 kHz). The measured `M=1048576` row resolves
to 9.155 kHz, but that is `-8.447%` from 10 kHz; `M=2097152` and 3.93216 ms
would be needed for a within-5% 10 kHz bin.

## Synthetic Python scaling

Every row was run in three fresh Python processes with PCG64 seeds 1000--1002
and one numerical thread requested for common BLAS/OpenMP runtimes. The timed
candidate path is crossing extraction (waveform mode), one global phase fit,
and one Welch PSD. Fixture generation and the second oracle fit/PSD are
reported separately. Maximum RSS is for the entire validation fixture and
must not be read as isolated estimator memory.

| `M` | phase events | record support | first usable offset | event-path median | fixture peak RSS median |
|---:|---:|---:|---:|---:|---:|
| 4,096 | 18,432 | 7.68 us | 2.34375 MHz | 1.31 ms | 102.6 MB |
| 16,384 | 73,728 | 30.72 us | 585.94 kHz | 4.02 ms | 109.7 MB |
| 131,072 | 589,824 | 245.76 us | 73.24 kHz | 34.4 ms | 166.9 MB |
| 262,144 | 1,179,648 | 491.52 us | 36.62 kHz | 79.4 ms | 233.2 MB |
| 1,048,576 | 4,718,592 | 1.96608 ms | 9.155 kHz | 555 ms | 604.5 MB |

The descriptive fit over the largest five event rows was proportional to
`N^1.227` (`R^2=0.987`); it is not a simulator forecast. The waveform fixture
was bounded at `M=32768`: 147,456 events became about 2.949 million samples at
20 intervals/cycle, and crossing-plus-analysis took 31.5 ms median. The fit
over its largest five rows was approximately linear in retained samples
(`N^0.989`, `R^2=0.996`). Process launch/import and synthetic construction make
the full process times much larger and are preserved in the JSON.

## ngspice-46 behavioral scaling

The PDK-free deck is an explicitly driven carrier with an authored TRNOISE
frequency-error source and a saved phase integrator. It has no oscillator
startup, amplitude dynamics, compact-model device noise, ISF, AM-to-PM, or
physical calibration. These numbers measure only ngspice transient generation,
portable ASCII export, parsing, and the candidate pipeline.

Each duration was run once because the 18 us point itself took 102 s. The
ngspice/export interval scaled as follows:

| transient | adaptive rows | ASCII bytes | ngspice + export | first usable offset |
|---:|---:|---:|---:|---:|
| 0.5 us | 152,789 | 11.6 MB | 3.45 s | 37.50 MHz |
| 1 us | 291,886 | 22.2 MB | 6.63 s | 18.75 MHz |
| 2 us | 579,389 | 44.0 MB | 13.06 s | 9.375 MHz |
| 4 us | 1,006,364 | 76.5 MB | 22.51 s | 4.688 MHz |
| 8 us | 2,040,793 | 155.1 MB | 45.45 s | 2.344 MHz |
| 18 us | 4,465,725 | 339.4 MB | 102.05 s | 1.172 MHz |

The descriptive linear fit is 5.610 seconds per simulated microsecond plus
0.869 s (`R^2=0.99975`) for this behavioral deck, host, and ASCII output path.
It must not be extrapolated into a transistor VCO runtime: Newton iterations,
rejected steps, device/model count, startup, and a physically complete noise
manifest are absent. Even this cheap deck is roughly 8.5 times the existing
12 s/500 ns grading-row context at 18 us, before reaching a power-of-two-valid
1 MHz record.

The carrier extractor closed against saved ngspice `v(phi)` on every row. RMS
phase disagreement was `1.00e-5` to `1.58e-5 rad`; the largest PSD difference
over bins 4 through `M/10` was 0.00683 dB. Those are behavioral pipeline
closure numbers, not phase-noise accuracy for a physical oscillator.

## Capability and replay probes

The exact ngspice-46 binary returned `unimplemented dot command` for both
`.pss` and `.hb`. A current-source TRNOISE smoke deck produced nonzero output,
resolving contradictory v46 manual wording only for source functionality. Two
isolated processes both proved that their startup `.spiceinit` loaded
`setseed 12345`, yet emitted different waveform hashes. A third process using
seed 54321 was also distinct. Evidence capture therefore passed, while the
deterministic-replay methodology gate failed.

## Interpretation

- Analyzer cost is small after crossings exist; carrier-resolved noisy
  transient generation and evidence retention dominate.
- Streaming crossings could avoid retaining hundreds of megabytes, but cannot
  reduce simulator generation cost.
- The current 500 ns grading context has 2 MHz whole-record spacing before
  averaging and cannot produce a valid 1 MHz, `K=8`, `k_min=4` score.
- The physical noisy-transient model and reproducible-seed policy remain the
  blockers, not FFT implementation speed.
