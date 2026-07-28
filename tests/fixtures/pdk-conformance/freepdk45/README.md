# FreePDK45 native reference

FreePDK45 selects nominal, fast and slow models by directory
(`models_nom`, `models_ff`, `models_ss`). `NMOS_VTG.inc` is a flat level-54
model card and has no `.lib` sections, so `native.spice` deliberately uses
`.include`.

## Documentation/model disagreement

The manual reports nominal VTG NMOS Ion=`975.5 uA/um` at 1.0 V but does not
state the channel length. Direct ngspice 45.2 evaluation gives:

- L=45 nm: `1.15074 mA`, about 18% above the table.
- L=50 nm: `0.979180565 mA`, about 0.38% above the table.

The fixture uses and explicitly records 50 nm. That is not an assertion that
the kit's nominal node is 50 nm; it preserves the ambiguity instead of
adjusting an oracle silently.

The installed kit describes these as HSPICE predictive models. Successful
ngspice execution is compatibility evidence, not manufacturing or signoff
evidence.
