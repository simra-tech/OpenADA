Fixes #1098 (sg13_hv_svaricap hot-temperature transient failure).

**What**: adds `tlevc = 1` to the `dsubw` substrate-diode model cards in `sg13g2_svaricaphv_mod.lib` and `sg13g2_svaricaphv_mod_mismatch.lib` (one token per file, no other changes).

**Why**: the cards specify `vj = 0.1` and `cta = 1e-6` but leave `TLEVC` at ngspice's default 0. The default diode temperature equation drives a junction potential this small through zero at 52.46980 °C; beyond that, the transient depletion-charge expression evaluates `log(1 - vd/VJ(T))` with a negative argument for any positive well-to-substrate bias, and every transient containing the device aborts at its first timepoint ("Timestep too small ... trouble with ...dsubw"). The equation predicts the measured pass/fail edges to five significant figures: temperature edge 52.46980 °C, and at 60 °C a control-bias edge of 29.7394 mV (= computed |VJ(60 °C)|).

`TLEVC=1` keeps the specified junction potential positive (`TPB` defaults to 0) and activates the already-present `cta` coefficient — selecting a defined temperature mode rather than adding an arbitrary numerical floor.

**Validation** (ngspice-46, one-device transient testcase from the linked issue):

| Check | Unpatched | Patched |
|---|---|---|
| 60 °C / 0.3 V transient, 20 ns | fails at first timepoint | completes |
| 85 °C / 0.9 V transient, 20 ns | fails at first timepoint | completes |
| 125 °C / 0.3 V transient, 20 ns | fails at first timepoint | completes |
| 27 °C small-signal C–V, 6 gate biases (−3…+3 V) | — | byte-identical to unpatched (0 F difference at 17-digit precision) |

The 27 °C invariance is expected: both temperature modes reduce to the specified `vj`/`cjo` at the reference temperature, so nominal-temperature results are untouched. Away from 27 °C the fix intentionally changes the `dsubw` depletion-capacitance temperature law from the (previously non-evaluable) default mode to the linear `cta` law — please confirm this matches the intended device characterization.

No Verilog-A / OSDI change is involved; `mosvar.va` is not the failing element.
