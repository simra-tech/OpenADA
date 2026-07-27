# `testbench.simulate` published-artifact dispatch fixtures

`model-free-differential/`, `nmos-common-source/` and `ihp-sg13g2-inverter/`
are unmodified output of the Simra schematic compiler (`simra-schematic 0.4.0`
over `ordec 0.7.0.dev88+g00277225e`), copied byte-for-byte with the digests
their descriptors publish. They are not hand-written stand-ins: the point of
this operation is to consume what a publisher actually emits.

`portable-inverter/` is the one exception and is labelled as such below.

| Fixture | `parameters` | `simulation_ready` | `simulation_handoff` | Declared analyses |
| --- | --- | --- | --- | --- |
| `model-free-differential/` | `resolved` | `true` | `split_required` | `op`, `tran` |
| `nmos-common-source/` | `resolved` | `false` | `split_required` | `op`, `dc` |
| `ihp-sg13g2-inverter/` | `resolved` | `false` | `direct` | `tran` |
| `portable-inverter/` | `resolved` | `false` | `direct` | `tran` |

## What each fixture proves

`model-free-differential/` is the case the shared simulation profile cannot
accept. Its deck carries both `.OP` and `.TRAN 100p 20n`, so
`circuit.simulate/v1alpha2` refuses it with
`simulation.analysis.unsupported`. Deriving one deck per declared analysis
makes both runnable without touching any other line of the published deck.

`nmos-common-source/` is the case the publisher marks not self-contained. Its
parameters are fully resolved, yet `simulation_ready` is `false` because the
deck names a device model and model collateral is deliberately outside the
schematic contract. It is dispatchable only with an explicit
`spice-model-library` configuration reference; `nmos_lv.models` is one
self-contained BSIM4 card supplied for exactly that purpose.

## Observed native evidence

ngspice 45.2 on linux/amd64, batch mode, no `.spiceinit`.

`model-free-differential/`, operating point — the differential pair is driven
by two complementary pulse sources through a 50 Ω / 100 Ω / 50 Ω divider, so
the 1.0 V differential drive gives 5 mA and a ±250 mV split about the midpoint:

```
v(d)=0.1  v(db)=1.1  v(outp)=0.35  v(outn)=0.85
```

`model-free-differential/`, transient — 232 points to exactly 20 ns, with the
outputs swapping on each pulse edge:

```
     time[s]     v(d)    v(db)   v(outp)   v(outn)
  0.0000e+00   0.1000   1.1000    0.3500    0.8500
  3.2200e-09   1.1000   0.1000    0.8500    0.3500
  6.1200e-09   0.1000   1.1000    0.3500    0.8500
  1.2520e-08   1.1000   0.1000    0.8500    0.3500
  2.0000e-08   0.1000   1.1000    0.3500    0.8500
```

`nmos-common-source/`, operating point and 37-point DC sweep of `V_G` from 0 V
to 1.8 V in 50 mV steps, with `VTH0=0.45` and a 10 kΩ drain load:

```
   v(vg)   v(vout)
    0.00    1.80000     device off, output at the supply
    0.45    1.79989     at threshold
    0.90    0.972905    mid-transition
    1.35    0.155270
    1.80    0.091925
```

The operating point independently reproduces the sweep's 0.9 V sample
(`v(vout)=0.972905`), which is a cross-check that both derived decks describe
the same circuit.

## `portable-inverter/`: one deck, four PDKs

This fixture is **derived, not published**: it is `ihp-sg13g2-inverter/` with
its device models replaced by canonical roles (`nmos.core`, `pmos.core`), a
10 fF load added, and its digests recomputed. Everything else about it is the
shape Simra emits — `M` cards, SI geometry, `W`/`L`/`M`/`NF`.

It exists to hold the portability claim honest. The same bytes were bound to
four installed technologies with nothing but `--pdk` changing. ngspice 45.2,
linux/amd64, `VDD = 1.2 V`, `W/L = 2 µm / 0.5 µm` (n) and `4 µm / 0.5 µm` (p),
input edge 200 ps, 820 points to 8 ns, typical corner:

| `--pdk` | corner | prefix | scale | model bound | V<sub>OL</sub> | V<sub>OH</sub> | t<sub>pHL</sub> |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ihp-sg13g2` | `mos_tt` | `x` | 1 | `sg13_lv_nmos` | 0.00 mV | 1.2000 V | 81.3 ps |
| `sky130A` | `tt` | `x` | 1e-6 | `sky130_fd_pr__nfet_01v8` | −0.00 mV | 1.2000 V | 147.1 ps |
| `gf180mcuD` | `typical` | `x` | 1 | `nfet_03v3` | 0.00 mV | 1.2000 V | 251.8 ps |
| `freepdk45` | `nom` | `m` | 1 | `NMOS_VTG` | 0.09 mV | 1.1997 V | 119.6 ps |

The delay ordering follows the nodes (130 nm, 130 nm at a 1.8 V device driven
at 1.2 V, 180 nm, 45 nm at a 500 nm drawn length), which is a weak but real
cross-check that four different model sets were actually exercised rather than
one being silently reused.

None of this is a signoff, correlation or fidelity claim. It establishes that
one canonical deck binds and converges on four technologies; nothing more.

## What this bundle does not establish

These fixtures establish digest-bound dispatch, correct deck derivation, and
fresh structurally valid native evidence per declared analysis. They do not
establish model fidelity, PDK correctness, silicon correlation, signoff
suitability, or portability across arbitrary published artifacts. The
`nmos_lv.models` card is a plausible BSIM4 card chosen to exercise the
collateral path; it is not a foundry model and no measurement derived from it
carries physical meaning.
