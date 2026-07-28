# SKY130 native reference

`native.spice` follows `libs.tech/xschem/sky130_tests/test_nmos.sch`: the
standard 1.8 V NFET, W=1, L=0.15, NF=1, both biases at 1.8 V, the `tt`
library, and `.option wnflag=1`. The PDK's own schematic annotates this device
at `5.0094e-4 A`; ngspice 45.2 produces `5.010462008986461e-4 A` in magnitude.

Geometry is deliberately written in plain microns in the native deck because
`libs.tech/ngspice/all.spice` installs `.option scale=1.0u`. The portable deck
uses SI-valued canonical geometry; OpenADA must translate it exactly once.

The independently authored NF=2 branch exercises the width normalization that
NF=1 cannot. The PDK's `test_nmos.sch` says `wnflag=1` selects bins using W/NF,
and `libs.tech/xschem/sky130_fd_pr/nfet_01v8_nf.sym` netlists total W as the
entered per-finger width times NF. The fixture therefore uses total W=1 um,
NF=2 (two 0.5 um fingers), L=0.15 um, and the same 1.8 V biases. Native
ngspice 45.2 produces `i(vd_nf2) = -4.969870468164799e-4 A`; the model-free
pipeline must reproduce that native value.

Both native branches are hand-written against the PDK documentation. They are
not generated from OpenADA's binding.

This is open-tool exploratory evidence, not manufacturing or signoff evidence.
