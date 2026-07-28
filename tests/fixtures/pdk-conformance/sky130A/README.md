# SKY130 native reference

`native.spice` follows `libs.tech/xschem/sky130_tests/test_nmos.sch`: the
standard 1.8 V NFET, W=1, L=0.15, NF=1, both biases at 1.8 V, the `tt`
library, and `.option wnflag=1`. The PDK's own schematic annotates this device
at `5.0094e-4 A`; ngspice 45.2 produces `5.010462008986461e-4 A` in magnitude.

Geometry is deliberately written in plain microns in the native deck because
`libs.tech/ngspice/all.spice` installs `.option scale=1.0u`. The portable deck
uses SI-valued canonical geometry; OpenADA must translate it exactly once.

## Known conformance gap

The native documented experiment states `.option wnflag=1`, which selects
model bins using W/NF. The current binding does not state that option. NF=1
makes the scalar oracle insensitive to the disagreement, so this fixture does
not establish multi-finger/bin conformance. The accompanying strict xfail test
keeps the gap visible until the binding and an NF>1 oracle are addressed.

This is open-tool exploratory evidence, not manufacturing or signoff evidence.
