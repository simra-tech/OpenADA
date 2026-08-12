# IHP SG13G2 mosvar hot-temperature transient reproducer

This directory isolates the `sg13_hv_svaricap` transient failure in the pinned
OpenADA simulation image. The deck contains one PDK varicap subcircuit, one DC
well/control source, and one 1 mV pulse on one gate. The other gate and the
substrate are grounded.

Run the known failing point (60 °C, 0.3 V control):

```console
./run.sh
```

Run another temperature/control point:

```console
./run.sh 40 0.3
./run.sh 60 0.2
```

The runner creates an ignored, case-specific rundir under `scratch/`, copies
the deck and `.spiceinit`, symlinks every file from the image's
`/foss/pdks/ihp-sg13g2/libs.tech/ngspice/models/` into that rundir, and invokes
ngspice in the required image. Its Docker invocation always uses `--rm` and is
guarded by `timeout 600`.

To replay a generated rundir directly:

```console
timeout 600 docker run --rm \
  -v "$PWD/scratch/t60-v0.3:/work" \
  localhost/sandboxy-local-simra:latest \
  bash -lc 'cd /work && ngspice -b repro.spice'
```

The local `.spiceinit` loads only `mosvar.osdi`, the one OSDI module used by
this deck, and selects the PDK-required `ngbehavior=hsa` compatibility mode.
The PDK model files and OSDI binary are not copied into this repository.

## Measured pass/fail boundary

A run passes only when ngspice exits successfully and prints the final sample
at 20 ns. A run fails when ngspice exits nonzero with `Timestep too small`.
The boundary was bisected with this runner and the unmodified PDK model:

| Temperature | Control voltage | Result |
|---:|---:|:---|
| 60 °C | 0.0297393513281250 V | pass (235 rows through 20 ns) |
| 60 °C | 0.02973935136718750 V | fail (at 4.52183 ps) |
| 52.46980094909668 °C | 0.1 V | pass |
| 52.46980285644531 °C | 0.1 V | fail at the initial point (`dsubw`) |
| 52.46980094909668 °C | 0.2 V | pass |
| 52.46980285644531 °C | 0.2 V | fail at the initial point (`dsubw`) |
| 52.4697998 °C | 0.3 V | pass |
| 52.4698028 °C | 0.3 V | fail at the initial point (`dsubw`) |

At 60 °C the minimum failing control is therefore bracketed within
`29.739351328125` to `29.739351367188` mV (39.1 pV wide). The first
hot-temperature onset does not measurably move between 0.1, 0.2, and 0.3 V:
all three brackets contain approximately 52.469802 °C. The generated decks
retain every supplied digit even though ngspice rounds the temperature in its
banner.

## Root cause

`dsubw` is not part of the MOSVAR Verilog-A module. It is a built-in ngspice
diode in the IHP wrapper
`libs.tech/ngspice/models/sg13g2_svaricaphv_mod.lib`:

```spice
dsubw W1 W dsubw off area=... pj=...
.model dsubw d is=2.45e-17 ... vj=0.1 ... fc=0.95 cta=1e-6
```

The model omits `TLEVC`, so ngspice-46 selects its default `TLEVC=0`
junction-capacitance temperature law. In `DIOtemp`, that law calculates

```text
DIOtJctPot = pbfact + (T / 300.15 K) * pbo
```

and the small `VJ=0.1 V` makes `DIOtJctPot` cross zero at
52.46980204129827 °C. During transient charge evaluation, `DIOload` then uses

```text
arg = 1 - vd / DIOtJctPot
sarg = exp(-M * log(arg))
```

For `dsubw`, `vd` is approximately `-ctrl_v`. Immediately above the
temperature crossing, while `-ctrl_v < DIOtJctPot < 0`, `arg` is negative and
`log(arg)` is outside its real domain. It becomes positive again only when the
potential is more negative than the applied control. This produces the
timestep failure over a temperature/bias interval; neither saturation-current
overflow nor the MOSVAR OSDI module is on the failing path.

The standalone calculation mirrors the ngspice-46 equations and constants:

```console
python3 junction_temperature.py
```

Its relevant output is:

```text
DIOtJctPot zero: 52.46980204129827 degC
temp_C       DIOtJctPot_V          log_argument          log(argument)
52.4697000  +4.024700302752e-07  +7.453981163888e+05  13.52167...
52.4697998  +8.840101484164e-09  +3.393626299172e+07  17.33999...
52.4698028  -2.992462028173e-09  -1.002518976626e+08  nan
52.4699000  -3.863675168414e-07  -7.764617897618e+05  nan
60.0000000  -2.973935121787e-02  -9.087644407648e+00  nan
```

At 60 °C, the calculated magnitude `29.73935121790 mV` also predicts the
measured control edge at `29.73935135 mV`; the roughly 0.13 nV difference is
consistent with the internal `W1` node not being exactly at zero. The exact
temperature zero lies inside every measured temperature bracket.

## Candidate fix and validation

The candidate patch adds `TLEVC=1` to the `dsubw` model cards in both the
ordinary and mismatch high-voltage svaricap libraries. With that selector,
ngspice uses

```text
DIOtJctPot = VJ - TPB * (T - 300.15 K)
DIOtJctCap = CJO * (1 + CTA * (T - 300.15 K))
```

`TPB` defaults to zero, so the junction potential remains at the specified
positive `VJ=0.1 V`. The library's existing `CTA=1e-6` becomes effective.
This is preferable to an arbitrary numerical floor because it selects a
documented diode temperature mode consistent with the coefficient already on
the model card.

Apply the candidate with separate IHP-Open-PDK and OpenADA checkouts:

```console
patch -d /path/to/IHP-Open-PDK -p1 \
  < /path/to/OpenADA/reproducers/ihp-sg13g2-mosvar-hot-temp/ihp-sg13g2-dsubw-tlevc.patch
```

`run-patched.sh` instead copies the model directory and applies the patch in an
ephemeral container directory; it never changes the installed `/foss` PDK:

```console
./run-patched.sh 60 0.3
./run-patched.sh 125 0.3
```

The unmodified model fails at both points with one retained transient row and
direct `dsubw` trouble. The patched model completes 20 ns at both points with
232 rows.

No OSDI rebuild is required: the changed `dsubw` card is parsed by ngspice at
run time, outside `mosvar.osdi`. For completeness, the PDK's OSDI rebuild path
in this image is:

```console
cd /path/to/IHP-Open-PDK/ihp-sg13g2/libs.tech/verilog-a
./openvaf-compile-va.sh
# mosvar command selected by that script:
openvaf-r -D__NGSPICE__ -o ../ngspice/osdi/mosvar.osdi mosvar/mosvar.va
```

The compiler available in the runtime reports
`OpenVAF-reloaded 20260616-2-gc592eed-dirty`, but no rebuild is needed and the
compiler is not on the defect path. Use a writable checkout because the build
script overwrites the OSDI outputs.

### 27 °C small-signal behavior

`compare-cv.sh` runs the original and patched libraries at 27 °C, a 0.3 V
well bias, and 1 GHz. It measures
`-imag(I(VEXC))/(2*pi*frequency)` at six gate biases:

```console
./compare-cv.sh
```

| Gate DC | Original | Patched | Difference |
|---:|---:|---:|---:|
| -3 V | 3.38256482194147288 fF | 3.38256482194147288 fF | 0 F |
| -1 V | 3.63859470090025178 fF | 3.63859470090025178 fF | 0 F |
| 0 V | 4.99091624602253584 fF | 4.99091624602253584 fF | 0 F |
| 0.3 V | 5.82635999645297004 fF | 5.82635999645297004 fF | 0 F |
| 1 V | 6.67836778773348438 fF | 6.67836778773348438 fF | 0 F |
| 3 V | 7.04878532230716015 fF | 7.04878532230716015 fF | 0 F |

Every original/patched `wrdata` file is byte-identical at 17-digit output
precision. At the 27 °C reference temperature both temperature modes reduce
algebraically to the same `VJ` and `CJO`, so this exact match is expected.
