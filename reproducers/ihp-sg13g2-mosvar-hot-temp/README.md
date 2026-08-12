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
