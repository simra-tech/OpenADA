# IHP SG13G2 full-chip route

Evaluated against official public sources on 2026-07-16. Recheck current IHP
submission dates, required category, PDK/deck revision, and participation terms
before a real submission.

## Contents

- [Frozen reference stack](#frozen-reference-stack)
- [Why this template](#why-this-template)
- [Reproducible native route](#reproducible-native-route)
- [CPU integration choices](#cpu-integration-choices)
- [Verification hardening](#verification-hardening)
- [Submission boundary](#submission-boundary)

## Frozen reference stack

- [IHP LibreLane full-chip template](https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template)
  commit `0418301723d86133de686ef743cfd668bb3d11d4`.
- Template-pinned LibreLane `3.0.0`, commit
  `69b2067bd2b5eb89b84649b76e9edaa9e51e6735`; keep the template's
  `flake.lock` unchanged.
- Template-pinned IHP Open PDK commit
  `3b5a704ba6738aa686b08706187830e6284d2a10`.
- The template's lock also freezes OpenROAD, OpenSTA, ABC, Yosys, KLayout,
  Magic, Netgen, Nix packages, and their transitive inputs. Treat the lock as
  the authority rather than restating a partial tool list in project prose.

The [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK) remains Preview
and explicitly not production-intended. Passing the public decks supports an
open-PDK candidate, not proprietary foundry signoff.

## Why this template

The template is a coupled full-chip shell, not just a core flow. It contains:

- SG13G2 standard-cell, IO, SRAM, timing, LEF, GDS, Verilog, and CDL views;
- core and IO power pads plus signal and analog pads;
- pad placement, corner/filler insertion, abutment, bondpad placement, PDN,
  seal ring, metal fill and density;
- OpenROAD implementation, routed timing/RCX/IR evidence, Magic and KLayout
  stream/check steps, extraction, Netgen LVS, and RTL/gate-level cocotb hooks.

Bondpads are not yet part of the PDK at this pin. The template carries
Apache-2.0 `bondpad_70x70` and `bondpad_70x70_novias` LEF/GDS/Verilog views.
Bind those exact files as project collateral.

The default die is 1600 µm × 1600 µm and the example couples two SRAM
placements to its macro PDN. When removing or changing SRAMs, update the macro
list, locations, PDN connections, PDN Tcl, simulation models, and reference
netlist together. Shrinking the logic does not automatically shrink a
pad-limited die.

## Reproducible native route

Use the official Nix installation documented by
[LibreLane](https://librelane.readthedocs.io/en/latest/installation/nix_installation/installation_linux.html),
then run from a clean detached template checkout:

```bash
nix-shell
librelane --smoke-test
make clone-pdk
make sim
make librelane
make sim-gl
```

`make librelane` uses Ciel to enable the pinned PDK and saves final views to
`final/`. The pinned README mentions `make copy-final`, but that target is not
present in the pinned Makefile; do not treat it as a required gate.

The template has no separately frozen container recipe. A container is an
acceptable evaluated runtime only when it preserves the exact Nix lock or when
the complete alternative tool/PDK set is frozen and requalified. Record a
pre-existing IIC-OSIC image by manifest digest, not a mutable tag. Its
`iic-osic-tools` OpenADA profile recognizes `/foss`; the profile does not launch
the image.

## CPU integration choices

For a small but submission-oriented RISC-V CPU, prefer a fully pinned
Apache-2.0 Ibex `small` configuration with its reviewed source manifest and
verification collateral. Integrating Ibex is not a one-file replacement:

- map instruction/data interfaces to the SRAM macros or a documented external
  bus;
- provide a boot/loading path because hard SRAM power-up is unknown;
- hold and release reset safely around loading;
- update pad types/counts/placement, SDC, macro locations, and PDN together;
- run architectural, loader/firmware, RTL, formal-equivalence, and gate-level
  tests.

A deliberately tiny educational CPU is acceptable for evaluating the agent
experience, but label whether it is programmable, what memory model it uses,
and which behavioral coverage is absent. A fixed self-test program alone is
not a general-purpose CPU submission.

Audit every dependency license against the current IHP participation agreement.
Do not infer that a permissive subcomponent license automatically satisfies the
submission terms.

## Verification hardening

The default template is a starting point. For a tapeout candidate:

- enable formal equivalence (`RUN_EQY`) after confirming the supported flow;
- disallow unresolved global-routing congestion;
- keep full KLayout/Magic DRC, antenna, density, XOR, extraction, LVS, routed
  timing/RCX, and IR steps enabled;
- review the template's disabled illegal-overlap checker instead of inheriting
  it as an unexplained waiver;
- bind package-aware `VSRC_LOC_FILES` before interpreting IR-drop numbers as a
  manufacturing power model;
- review IO Liberty maximum-slew checks, output loads, unannotated pad nets,
  and the intended package/board model at every declared corner;
- run a routed gate test with the final powered netlist and either annotate the
  retained SDF or label the result explicitly as zero-delay;
- retain the complete run tree, not only `final/`;
- run all declared slow/typical/fast timing contexts; and
- generate a powered final reference netlist independently of layout
  extraction.

Do not compare the final GDS against its own extracted SPICE and call that LVS.
Final LVS needs a separately generated powered reference netlist with resolved
standard-cell, IO, bondpad, and macro subcircuits.

Re-run the final GDS with the selected official PDK DRC and LVS entry scripts,
deep mode, density and extra rules enabled unless IHP explicitly directs
otherwise. Retain entry scripts, generated decks, logs, report databases,
extracted circuits, reference netlist, and hashes. OpenADA's current KLayout and
Netgen operations may normalize exact caller-supplied checks when their
contracts fit, but OpenADA does not generate the layout or reference netlist.

Do not silently waive markers merely because they occur inside PDK library
cells. At the frozen PDK revision, KLayout may report `metal1_pin_Offgrid` in
the `sg13g2_Clamp_P15N15D` and `sg13g2_Clamp_N15N15D` IO subcells; this is
tracked in [IHP-Open-PDK issue 683](https://github.com/IHP-GmbH/IHP-Open-PDK/issues/683).
Retain the exact cells/categories and require a reviewed PDK update or explicit
submission disposition. IHP recommends KLayout for final DRC interpretation,
but that guidance does not convert an unrelated Magic rule marker into a
waiver; preserve any Magic/KLayout disagreement for review.

## Submission boundary

Use the current official
[IP development steps](https://github.com/IHP-GmbH/Open-Silicon-MPW/blob/main/IP-development-steps.md)
and [submission process](https://github.com/IHP-GmbH/Open-Silicon-MPW/blob/main/Submission-process.md)
as authoritative external gates. At minimum preserve seal ring, IO/power and
bonding plan, fillers/fill/density, precheck and full DRC, final-reference LVS,
timing/power evidence, pinout, package assumptions, waiver ledger, and the
prescribed GDS save settings.

Before IHP review, say **IHP SG13G2 open-PDK tapeout candidate** or **submission
candidate** according to the completed checklist. Successful IHP/foundry review
may make it **accepted for fabrication**; signoff: not claimed.
