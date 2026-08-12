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
