**Describe the bug**

Any ngspice transient containing a `sg13_hv_svaricap` instance with the well/control terminal biased positive with respect to substrate aborts at its first timepoint once the simulation temperature exceeds ~52.47 °C:

```text
doAnalyses: TRAN: Timestep too small; initial timepoint: trouble with xvar:dsubw-instance d.xvar.dsubw
```

The failing element is not the MOSVAR Verilog-A device — it is the native ngspice `dsubw` substrate diode declared in the SPICE wrapper `sg13g2_svaricaphv_mod.lib`. Its model card specifies `vj = 0.1` and `cta = 1e-6` but omits `TLEVC`, so ngspice's default (TLEVC=0) diode temperature equation applies. For a junction potential as small as 0.1 V, the temperature-adjusted potential crosses **zero at 52.46980 °C**; past that point the depletion-charge expression evaluates `log(1 - vd/VJ(T))` with a negative argument (NaN) for any positive well-to-substrate bias, and the transient dies at its first Newton step.

The equation predicts the measured behavior exactly:

| Temperature | Control bias | Result |
|---:|---:|:---|
| 52.4697998 °C | 0.3 V | pass |
| 52.4698028 °C | 0.3 V | **fail** at initial timepoint on `dsubw` |
| 60 °C | 29.7393513 mV | pass |
| 60 °C | 29.7393514 mV | **fail** (computed \|VJ(60 °C)\| = 29.73935 mV) |

The same card appears in `sg13g2_svaricaphv_mod_mismatch.lib`. DC/OP analyses are unaffected (the OP preceding the transient solves fine); the failure needs the transient depletion-charge path plus positive well bias.

**Suggested Priority**
- [ ] Low
- [x] Medium
- [ ] High
- [ ] Critical

Blocks all hot-temperature (> 52.5 °C) transient simulation of circuits containing the HV svaricap under normal positive tuning bias (e.g. any LC-VCO at an 85 °C corner), well inside the model's declared TMAX. No silicon/characterization impact claimed.

**Environment**

- ngspice-46 (KLU build)
- IHP-Open-PDK `main` (reproduced at 144f811c and current head; `sg13g2_svaricaphv_mod.lib` lines 80–82)
- `mosvar.osdi` compiled per `libs.tech/verilog-a/openvaf-compile-va.sh` (the OSDI is loaded but is not the failing element)
- Linux x86-64

**To Reproduce**

Self-contained one-device deck (models directory linked/`.spiceinit` per the PDK's standard ngspice setup, `ngbehavior=hsa`, `mosvar.osdi` loaded):

```spice
* Minimal sg13_hv_svaricap hot-temperature transient reproducer
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

1. Run as-is (60 °C, 0.3 V): fails at the first transient timepoint with the `dsubw` message above.
2. Change `.temp 60` to `.temp 40` (or `ctrl_v` to 0): completes 20 ns normally.
3. The pass/fail edge bisects to 52.46980 °C (at 0.1/0.2/0.3 V control) — matching where the TLEVC=0 temperature-adjusted `vj = 0.1` crosses zero.

A standalone Python mirror of ngspice-46's `diotemp.c` arithmetic reproducing the zero crossing and both measured edges is available on request (or see the linked PR).

**Expected behavior**

Transient simulation of the HV svaricap completes at any temperature within the model's validity range, at any legal control bias.

**Attached testcase**

Deck above is complete. Proposed one-token fix (PR to follow / linked): add `tlevc = 1` to the `dsubw` model cards in `sg13g2_svaricaphv_mod.lib` and `sg13g2_svaricaphv_mod_mismatch.lib`. TLEVC=1 keeps the specified `vj` positive (`TPB` defaults to 0) and activates the card's already-present `cta` coefficient. With the fix, 60/85/125 °C transients complete, and at the 27 °C reference temperature the small-signal C–V response is byte-identical to the unpatched model (both temperature modes reduce to the specified `vj`/`cjo` there). Maintainers may want to confirm TLEVC=1 + TPB=0 + existing CTA expresses the intended temperature characterization away from 27 °C.
