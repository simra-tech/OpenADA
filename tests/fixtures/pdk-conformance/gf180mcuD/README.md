# GF180MCU D native reference

`native.spice` follows the PDK's `test_nfet_03v3.sch`. Most importantly,
`design.ngspice` is included before the `typical` section of
`sm141064.ngspice`; the former defines `fnoicor` and the statistical switches
which the latter evaluates while parsing every model card.

The native oracle is the 3.3 V NMOS at the PDK minimum W/L. The portable deck
uses the same physical dimensions in SI form and adds isolated metamorphic
circuits. The full core-device grid extends through L=`50.001 um` and
W=`100.001 um`; treating the third bin's 10 um limit as the grid maximum is a
false refusal.

Doubling W from 0.22 um to 0.44 um produces a current ratio of
`1.6990672278783523`. An additional hand-written native two-device run produced
the exact same two currents as the bound pipeline, so this is compact-model
behavior at minimum W rather than a binding error. The first test draft used a
`1.7` lower bound and missed by `0.0009327721`; that result is preserved here.
The committed cross-PDK meaning of “roughly double” is the deliberately broad
physics-backed engineering band 1.5–2.5, while GF's exact independently
observed ratio is also pinned in
`expected.json`.

The PDK treats omitted junction geometry as zero. This OP fixture is therefore
not evidence for parasitic capacitance, transient delay, poles or switching
energy. It is also not foundry signoff.
