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
temperature crossing, `DIOtJctPot` is negative and arbitrarily close to zero,
so `arg` becomes negative and `log(arg)` is outside its real domain. This
produces the timestep failure; neither saturation-current overflow nor the
MOSVAR OSDI module is on the failing path.

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
