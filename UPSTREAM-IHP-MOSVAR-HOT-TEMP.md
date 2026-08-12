# `sg13_hv_svaricap` hot-temperature transient failure

## Summary

The IHP SG13G2 `sg13_hv_svaricap` model can make ngspice transient analysis
enter a sharply defined failure interval when the well/control terminal is
positive with respect to substrate. A one-device test case fails with:

```text
Timestep too small; initial timepoint: trouble with
xvar:dsubw-instance d.xvar.dsubw
```

The failing element is not the MOSVAR Verilog-A device. It is the native
ngspice `dsubw` diode in the high-voltage svaricap SPICE wrapper. The wrapper
specifies `VJ=0.1 V` and `CTA=1e-6` but leaves `TLEVC` at ngspice's default
zero. The resulting default temperature equation drives the bottom-junction
potential through zero at 52.4698020413 °C. The transient depletion-charge
equation then enters a temperature/bias interval where it evaluates a
logarithm with a negative argument.

The proposed PDK fix is to add `TLEVC=1` to `dsubw` in the normal and mismatch
high-voltage svaricap model files. This keeps the specified junction potential
positive, activates the already-present linear `CTA` coefficient, makes the
failing 60 °C and 125 °C transients complete, and leaves a six-point 27 °C
small-signal C-V comparison byte-identical at 17-digit output precision.

The defect is owned by the IHP-Open-PDK SPICE wrapper, not by MOSVAR's
Verilog-A source or the Verilog-A-to-OSDI compiler.

## Environment

- Runtime image: `localhost/sandboxy-local-simra:release-a20a119e36d9`
  (`localhost/sandboxy-local-simra:latest` resolved to this release during the
  investigation), Linux/amd64.
- Release-lock SHA-256:
  `a20a119e36d958dde2a9d4dcd1b52eafa308492a9406d5c9d7345db48624f5d3`.
- Base image:
  `hpretl/iic-osic-tools@sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0`.
- ngspice: `ngspice-46`, KLU build, creation date
  `Mon Jun 22 10:09:10 UTC 2026`.
- IHP-Open-PDK commit:
  [`144f811cdffda49b71d28f64e8a92b697b61cf06`](https://github.com/IHP-GmbH/IHP-Open-PDK/commit/144f811cdffda49b71d28f64e8a92b697b61cf06).
- `sg13g2_svaricaphv_mod.lib` SHA-256:
  `22747f4bec39c3934049a64bdd80ef5beb88cb699a46556e68e76685dd91630c`.
- MOSVAR source: `/foss/pdks/ihp-sg13g2/libs.tech/verilog-a/mosvar/mosvar.va`,
  SHA-256
  `146eec0c2a9c3c437c53109f8ec42af68f75bd9d341e79797d2feda0340a19f6`.
- OSDI compiler available in the runtime:
  `OpenVAF-reloaded 20260616-2-gc592eed-dirty`. The installed OSDI does not
  embed compiler provenance, so this identifies the pinned rebuild tool rather
  than proving which executable produced the installed binary.
- `mosvar.osdi` SHA-256:
  `f8a14171208baf33fd08e617ea093833ee874e4f3125572901d16a9d54db3e39`.
- PDK build script:
  `/foss/pdks/ihp-sg13g2/libs.tech/verilog-a/openvaf-compile-va.sh`.
  Its MOSVAR command is
  `openvaf-r -D__NGSPICE__ -o ../ngspice/osdi/mosvar.osdi mosvar/mosvar.va`.
- Startup: `ngbehavior=hsa`, with only `mosvar.osdi` loaded.

The reproducer uses `timeout 600 docker run --rm` for every container run.
No installed PDK file is modified; patched tests copy the model directory to
an ephemeral container directory first.

## Reproduction

The reproducer is in
`reproducers/ihp-sg13g2-mosvar-hot-temp/`. Run the known failing case from
that directory:

```console
./run.sh 60 0.3
```

The complete test circuit is:

```spice
* Minimal IHP SG13G2 sg13_hv_svaricap hot-temperature transient reproducer
.param ctrl_v=0.3
.temp 60

.lib cornerMOShv.lib mos_tt

VCTRL ctrl 0 {ctrl_v}
VEXC gate 0 PULSE(0 1m 1n 100p 100p 5n 10n)
XVAR gate ctrl 0 0 sg13_hv_svaricap l=300n w=3.74u Nx=1 Ny=1

.tran 100p 20n
.print tran v(gate)
.end
```

Pass means ngspice exits zero and prints the final 20 ns sample. Fail means it
exits nonzero with `Timestep too small`. The measured boundary is:

| Temperature | Control voltage | Result |
|---:|---:|:---|
| 60 °C | 0.0297393513281250 V | pass; 235 rows through 20 ns |
| 60 °C | 0.02973935136718750 V | fail at 4.52183 ps |
| 52.46980094909668 °C | 0.1 V | pass |
| 52.46980285644531 °C | 0.1 V | fail at initial point on `dsubw` |
| 52.46980094909668 °C | 0.2 V | pass |
| 52.46980285644531 °C | 0.2 V | fail at initial point on `dsubw` |
| 52.4697998 °C | 0.3 V | pass |
| 52.4698028 °C | 0.3 V | fail at initial point on `dsubw` |

Thus the 60 °C control edge is bracketed within
29.739351328125-29.739351367188 mV, only 39.1 pV wide. The first hot onset
does not measurably move between 0.1, 0.2, and 0.3 V; all three temperature
brackets contain approximately 52.469802 °C.

## Root cause

The installed and upstream model source contains the following topology and
model card at
[`sg13g2_svaricaphv_mod.lib:80-82`](https://github.com/IHP-GmbH/IHP-Open-PDK/blob/144f811cdffda49b71d28f64e8a92b697b61cf06/ihp-sg13g2/libs.tech/ngspice/models/sg13g2_svaricaphv_mod.lib#L80-L82):

```spice
dsubw W1 W dsubw off area = '...' pj = '...'
rsubw W1 bn r = '...'
.model dsubw d is = 2.45E-17 jsw = 5.959E-10 n = 4 ns = 1.029 cjo = 1.444E-15 vj = 0.1 m = 0.1052 cjp = 1.117E-09 php = 0.457 mjsw = 0.2595 fc = 0.95 cta = 1E-06
```

`W` is the well/control terminal. `W1` is connected to substrate through
`rsubw`, so at the reported bias the `dsubw` diode voltage is approximately
`vd = V(W1)-V(W) = -ctrl_v`.

`dsubw` is a built-in ngspice diode. It does not appear in
`libs.tech/verilog-a/mosvar/mosvar.va`; that OSDI model represents the two
intrinsic MOS varactors in the wrapper. Because the diode card omits `TLEVC`,
ngspice-46's diode setup selects `TLEVC=0`. Its temperature preprocessing in
`src/spicelib/devices/dio/diotemp.c` computes:

```text
fact2       = T / 300.15 K
pbo         = (VJ - pbfact_nominal) / fact1
DIOtJctPot  = pbfact(T) + fact2 * pbo
```

For `VJ=0.1 V`, `DIOtJctPot` crosses zero at
52.46980204129827 °C. The transient charge path in
`src/spicelib/devices/dio/dioload.c` subsequently evaluates:

```text
arg   = 1 - vd / DIOtJctPot
sarg  = exp(-M * log(arg))
```

With positive control bias, `arg` becomes negative immediately after the sign
change while `-ctrl_v < DIOtJctPot < 0`, making the real logarithm undefined.
It becomes positive again only after the potential is more negative than the
applied control. The standalone mirror of the ngspice-46 arithmetic is
`reproducers/ihp-sg13g2-mosvar-hot-temp/junction_temperature.py`:

```console
python3 junction_temperature.py
```

It produces:

```text
DIOtJctPot zero: 52.46980204129827 degC
temp_C       DIOtJctPot_V          log_argument          log(argument)
52.4697000  +4.024700302752e-07  +7.453981163888e+05  13.521673739043633
52.4697998  +8.840101484164e-09  +3.393626299172e+07  17.339994705394734
52.4698028  -2.992462028173e-09  -1.002518976626e+08  nan
52.4699000  -3.863675168414e-07  -7.764617897618e+05  nan
60.0000000  -2.973935121787e-02  -9.087644407648e+00  nan
```

The computed zero lies inside each measured temperature bracket. At 60 °C,
the computed magnitude is 29.73935121787 mV, predicting the measured control
edge at approximately 29.73935135 mV; the internal `W1` node accounts for the
sub-nanovolt difference. Saturation-current terms remain finite. This is a
depletion-charge logarithm domain error caused by a nonpositive
temperature-adjusted junction potential, not an exponential overflow.

## Proposed fix

Add `tlevc = 1` to the `dsubw` model cards in both:

- `ihp-sg13g2/libs.tech/ngspice/models/sg13g2_svaricaphv_mod.lib`
- `ihp-sg13g2/libs.tech/ngspice/models/sg13g2_svaricaphv_mod_mismatch.lib`

The resulting card ends with:

```spice
... fc = 0.95 cta = 1E-06 tlevc = 1
```

The ready-to-apply candidate is
`reproducers/ihp-sg13g2-mosvar-hot-temp/ihp-sg13g2-dsubw-tlevc.patch`.
With separate IHP-Open-PDK and OpenADA checkouts:

```console
patch -d /path/to/IHP-Open-PDK -p1 \
  < /path/to/OpenADA/reproducers/ihp-sg13g2-mosvar-hot-temp/ihp-sg13g2-dsubw-tlevc.patch
```

With `TLEVC=1`, ngspice uses:

```text
DIOtJctPot = VJ - TPB * (T - 300.15 K)
DIOtJctCap = CJO * (1 + CTA * (T - 300.15 K))
```

`TPB` defaults to zero, preserving the positive `VJ=0.1 V`; the card's
existing `CTA=1e-6` becomes active. This selects a defined diode temperature
mode instead of introducing an arbitrary numerical floor.

No OSDI rebuild is needed because this is a SPICE model-card change outside
the Verilog-A module. If MOSVAR Verilog-A itself is changed for a separate
reason, the image's rebuild commands are:

```console
cd /path/to/IHP-Open-PDK/ihp-sg13g2/libs.tech/verilog-a
./openvaf-compile-va.sh
# Equivalent MOSVAR command:
openvaf-r -D__NGSPICE__ -o ../ngspice/osdi/mosvar.osdi mosvar/mosvar.va
```

Validation commands, which patch only an ephemeral model copy, are:

```console
./run-patched.sh 60 0.3
./run-patched.sh 125 0.3
./compare-cv.sh
```

The original model fails both hot transients with one retained row and direct
`dsubw` trouble. The patched model completes 20 ns at both temperatures with
232 rows.

## Impact

The defect prevents transient simulation of a valid-dimension
`sg13_hv_svaricap` under ordinary positive well bias at temperatures well
inside the model's declared `TMAX=500 °C`. The same `dsubw` card appears in
the mismatch library, so both files require the fix. Circuits with zero or
sufficiently small well-to-substrate bias can avoid the invalid logarithm,
which explains the sharp bias-dependent boundary rather than establishing
that the model is safe.

At the 27 °C reference temperature, original and patched small-signal input
capacitances at 1 GHz and `V(W)=0.3 V` were:

| Gate DC | Original | Patched | Difference |
|---:|---:|---:|---:|
| -3 V | 3.38256482194147288 fF | 3.38256482194147288 fF | 0 F |
| -1 V | 3.63859470090025178 fF | 3.63859470090025178 fF | 0 F |
| 0 V | 4.99091624602253584 fF | 4.99091624602253584 fF | 0 F |
| 0.3 V | 5.82635999645297004 fF | 5.82635999645297004 fF | 0 F |
| 1 V | 6.67836778773348438 fF | 6.67836778773348438 fF | 0 F |
| 3 V | 7.04878532230716015 fF | 7.04878532230716015 fF | 0 F |

Every original/patched `wrdata` pair was byte-identical at 17-digit precision.
This is expected because both modes reduce to the specified `VJ` and `CJO` at
the 27 °C reference temperature. Away from 27 °C, the candidate intentionally
changes the `dsubw` depletion-capacitance temperature law; maintainers should
confirm that `TLEVC=1`, zero-default `TPB`, and the existing `CTA` express the
intended characterization. The change does not alter MOSVAR OSDI equations or
the diode saturation-current temperature law.

The primary upstream for this defect is IHP-Open-PDK. A simulator-side warning
for a nonpositive temperature-adjusted junction potential could be useful as
a separate diagnostic, but no ngspice or OpenVAF compiler defect is needed to
explain or fix this PDK failure. This evidence is simulation-only and is not a
silicon-correlation or signoff claim.
