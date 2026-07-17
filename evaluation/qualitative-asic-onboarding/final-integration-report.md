# Final integration report

## Executive outcome

The integration reached the last LibreLane stage and saved a complete set of
full-chip views for an educational CPU in the IHP SG13G2 padframe: routed
netlist, DEF, extracted SPICE, SPEF, three-corner SDF and Liberty, sealed and
filled GDS, and a powered reference netlist. Routing, connectivity, antenna,
density, stream-out XOR, setup/hold timing, and native LVS all closed within
their stated scopes.

The result is **not tapeout-ready or submission-ready, and it does not
constitute foundry signoff**. Final DRC is a hard failure: Magic reports 220
`CntB.h1` markers and KLayout reports
24 `metal1_pin_Offgrid` markers. Post-route STA also retains maximum-slew
violations at I/O pad pins, the IR result has no package-aware source placement,
the routed gate test is zero-delay, the final LVS extraction is not independent
of the implemented DEF/LEF representation, and package/bonding and IHP
acceptance remain unresolved. The open PDK and open-source reports are preview
engineering evidence, not a manufacturing waiver or acceptance decision.

## Exact design and stack

The implemented `chip_core` is a small 8-bit accumulator CPU with a 6-bit
program counter and a wait-state-capable 64-byte external unified memory
interface. Instructions are two bytes. The tested instruction set covers
immediate and memory load/add, store, two auxiliary input banks, unconditional
and zero/nonzero branch, halt, and no-op. It has no boot ROM, on-chip program
memory, cache, interrupt controller, debug port, or operating system; an
external host must provide memory and `ready`.

The inherited full-chip shell maps the six address bits, write and halt to
eight output pads, maps the eight data bits to bidirectional pads, retains ten
auxiliary/ready inputs, reserves eight analog pads, and supplies separate
1.2 V core and 3.3 V I/O domains. The target clock is 50 MHz and reset is
synchronous active-low. This is deliberately an educational minimum, not a
general-purpose CPU or a complete packaged product.

| Identity | Exact value used |
|---|---|
| OpenADA | `0.4.0` from this checkout/plugin |
| Top cell | `chip_top` |
| Official template | [`IHP-GmbH/ihp-sg13g2-librelane-template`](https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template) at `0418301723d86133de686ef743cfd668bb3d11d4` |
| Template flow lock | LibreLane `3.0.0`, revision `69b2067bd2b5eb89b84649b76e9edaa9e51e6735` |
| PDK | Ciel-installed [`IHP-GmbH/IHP-Open-PDK`](https://github.com/IHP-GmbH/IHP-Open-PDK) `ihp-sg13g2` at `3b5a704ba6738aa686b08706187830e6284d2a10` |
| OCI invocation reference | `hpretl/iic-osic-tools@sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0` |
| Equivalent repository digest | `hpretl/iic-osic-tools@sha256:7371bae55da486f492cc270ea6137c4fcf3b11971de7a4506a74f62be143537a` |
| Image ID / platform | `sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb`, `linux/amd64` |
| Actual flow | IIC-OSIC-TOOLS 2026.06 environment, LibreLane `v3.1.0.dev1` |
| Runtime tools | OpenROAD `26Q2-2270-g4c26918f5`; OpenSTA `3.1.0`; KLayout `0.30.9`; Netgen `1.5.321`; Magic `8.3.664`; Yosys `0.66`; Verilator `5.048` |
| EDA isolation | project mounted read/write at `/work`, PDK read-only at `/foss/pdks`, network disabled |
| Authored core hash | `e113f5a52682ca2ba32177d4c833afa8b4c001f18c09bb5ed067ed3a2212c4d4` |

The actual LibreLane version is not the template-locked version. Completing one
candidate run records what happened; it does not establish general
compatibility between LibreLane `3.1.0.dev1` and the template's `3.0.0` lock.
That skew remains open in the identity ledger.

## Retry and run chronology

1. The first blank-workspace session built a different core-only prototype and
   exposed the failure mode this evaluation was intended to study: broad host
   discovery, avoidable downloads, a mixed tool stack, six failed physical-flow
   starts, no padframe, and failed DRC/LVS. Its generated core evidence is not
   evidence for the final full-chip candidate.
2. The bounded forward session selected the exact official template, PDK, and
   local OCI image before EDA. It stopped fail-closed because the template lock
   and installed LibreLane differed, retained a draft manifest, and performed no
   engineering run. That behavior validated the new coordinator's safe order.
3. The integration then adapted the exact official full-chip template rather
   than extending the earlier core-only flow. OpenADA RTL lint and structural
   checking passed, native RTL cocotb passed, and OpenADA Liberty-mapped
   synthesis passed before full-chip implementation.
4. `cpu_full_1` started at 21:23:48 UTC and failed at step 16 after synthesis
   optimized away `inputs[1]`; the pad instance then could not connect to
   `IOVDD`. The CPU and test were repaired to consume both auxiliary input
   banks, making every digital input pad functionally observable.
5. `cpu_full_2` ran from 21:26:17 to 22:47:12 UTC. It traversed all 78 flow
   positions, including the configured skips, saved final views, and returned
   exit 2 only when the two deferred final-DRC failures were surfaced. The 76
   top-level step runtimes sum to 4,854.568 seconds. The largest costs were
   Magic DRC (3,529.930 s), KLayout DRC (596.296 s), KLayout filler
   (304.632 s), and KLayout antenna (172.910 s).
6. The routed pad-level program then passed under Icarus. Independent OpenADA
   replays normalized final KLayout DRC and Netgen LVS. The DRC replay retained
   a fail; the first LVS replay exposed a Netgen-JSON parser defect, which was
   repaired and verified by a fresh replay.

The live identity ledger was exercised and frozen after the expensive run.
That validates post-run identity consistency, but it does **not** prove that
this particular run obeyed the coordinator's intended pre-run freeze ordering.

## End-to-end gate disposition

“Pass” below is limited to the exact check and evidence named in its row. It is
not a roll-up tapeout verdict.

| Gate | Status | Evidence and boundary |
|---|---|---|
| Source and project identity | **Partial** | Exact template, PDK, source, OCI image, tools, and collateral are hash-bound; the final ledger is frozen, but it was frozen post-run and the runtime/flow-lock skew remains open |
| RTL lint and structure | **Pass** | Strict lint has zero errors/warnings; OpenADA/Yosys elaboration and structural checking pass |
| Behavioral function | **Pass, scoped** | RTL cocotb completed the complete 42-byte test program at 895.001 ns with varied memory wait states and both auxiliary banks; this is not architectural coverage or formal verification |
| OpenADA core synthesis | **Pass** | Typical-corner SG13G2 Liberty mapping produced 262 cells, 3,751.7634 µm², 34 flops, no memories/processes, and no unmapped cells |
| Full-chip synthesis and assembly | **Complete** | All 40 functional/power/analog pad cells were retained; pad ring, bondpads, PDN, seal ring, filler, and density stages produced final views |
| Placement, CTS, and route | **Pass, scoped** | Detailed route DRC 0, route antenna 0, KLayout antenna 0, disconnected pins 0, critical disconnected pins 0; ten unsupported `LEF58_ENCLOSURE` warnings remain an explicit compatibility gap |
| Extracted multi-corner setup/hold | **Pass** | Fast, typical, and slow corners each report zero setup/hold violations and zero TNS at a 20 ns clock |
| Electrical timing acceptance | **Fail** | Maximum slew has 8 fast, 8 typical, and 18 slow violations, all at I/O pad pins; package load and I/O selection are not closed |
| Routed gate function | **Pass, scoped** | Zero-delay Icarus pad-level simulation completed the full program at 896 ns; no SDF, extracted parasitics, package, board, or formal-equivalence model was applied |
| Power grid / IR | **Partial** | VDD/VSS grids are connected and reported drops are small, but `VSRC_LOC_FILES` is absent, so the result is not package-aware |
| Stream-out, XOR, antenna, density | **Pass, scoped** | XOR differences 0, KLayout antenna 0, density errors 0, and final sealed/filled GDS was saved |
| Final DRC | **Fail** | Magic 220 `CntB.h1`; KLayout 24 `metal1_pin_Offgrid`; no waiver applied or inferred |
| LVS | **Pass with extraction/provenance limits** | Native LibreLane and repaired OpenADA normalization report a unique 623/623-device, 462/462-net match with zero mismatches; extraction was from the implemented DEF/LEF representation, not independently from final streamed GDS |
| Package, handoff, and submission | **Fail / incomplete** | Package, bond map, board electrical behavior, source locations, power sequencing, DRC disposition, preview-PDK review, and IHP acceptance are open |

## Physical and timing facts

The final die is 1,600 × 1,600 µm (2,560,000 µm²); the snapped core area is
752,466 µm². The implementation contains 403 standard cells occupying
5,729.88 µm², including 34 sequential cells, plus 59,330 fill cells and nine
antenna cells. The padframe metrics contain 176 pad cells: 12 input, eight
output, 16 bidirectional/analog, four power, and 136 pad spacers. Forty
bondpad/cover instances are present. Final connectivity reports zero
disconnected and zero critical pins.

| Corner | Worst hold slack | Worst setup slack | Setup/hold TNS | Max-slew violations |
|---|---:|---:|---:|---:|
| `nom_fast_1p32V_m40C` | 0.109098 ns | 12.519457 ns | 0 / 0 ns | 8 |
| `nom_typ_1p20V_25C` | 0.289137 ns | 10.834669 ns | 0 / 0 ns | 8 |
| `nom_slow_1p08V_125C` | 0.620887 ns | 7.837551 ns | 0 / 0 ns | 18 |

Maximum fanout and capacitance violations are zero. At the slow corner, the
worst bidirectional-pad transition is 3.475866 ns against a 1.2 ns limit; the
worst listed output-pad transition is 1.223757 ns. Each corner also reports 74
unannotated drivers, principally top/pad/clock-load structures, and zero
partially annotated drivers. Consequently, positive setup/hold slack does not
close the I/O electrical timing gate.

The flow estimates total power at 0.00416624 W. It reports worst VDD drop of
0.000873947 V and worst VSS rise/drop of 0.0007845 V, with both grids connected.
Because the run supplied no `VSRC_LOC_FILES`, those numbers model neither bond
wires nor package source locations and must not be used as a package-level IR
claim. The resizer's four floating-net warnings correspond to the top supply
nets (`VDD`, `VSS`, `IOVDD`, `IOVSS`); the independent PDN/connectivity and LVS
checks are clean, but the warning is retained rather than silently discarded.

The routed test's xUnit result and waveform hash to
`cbda10ff1aedb30b0e3a794c065a3fd2a82cc3e2a13487a74866c0207c8e006c`
and `a0063ccc2c97a2f3eefbc818285d50f8f898b8070d31d7db83b4b168eb39550b`.
The final routed netlist used by that test hashes to
`ce6c9f18b6016575caa62354b5f4a37b753439ecd48729eeb40c8e11022268c5`.
Icarus emitted nonfatal specify-model warnings. A generated SDF exists, but it
was not annotated; the test proves only zero-delay digital behavior at the
pad-model boundary.

## Final views and hashes

These files remain in the restricted external run tree under
`/dev/shm/openada-vibe-cpu-20260716/candidate/librelane/runs/cpu_full_2/final/`.
They are not checked into this evaluation package.

| Final view | SHA-256 |
|---|---|
| `gds/chip_top.gds` (49,269,736 bytes) | `5da75c547797f7c5ca167fabaed489a6ecf0024f1f9e709430b23eb82b194e4d` |
| `def/chip_top.def` | `6073647158259da38ae23c0e561bb8e63ad475d2e57e90ff868329c1f332967c` |
| `nl/chip_top.nl.v` | `ce6c9f18b6016575caa62354b5f4a37b753439ecd48729eeb40c8e11022268c5` |
| `pnl/chip_top.pnl.v` | `772423c1e1cf9351566d66842f6e16ced302e586d9903f6b1694600571c744b6` |
| `spice/chip_top.spice` | `fb26aa3520b82bfac774e5150ef9223a09f0058c56980f8ad433d059c38c161f` |
| `spef/nom/chip_top.nom.spef` | `a3bceda8a73d01fd52b267c7355529b545cdce763cea1ef3cc70284034689a52` |
| `sdf/nom_typ_1p20V_25C/chip_top__nom_typ_1p20V_25C.sdf` | `9c42f2ef7409009d7118fd36c279a70395a744acc54a9f55a9ad05478e55a29d` |

Hashes identify the observed bytes; they do not promote them to signed-off
manufacturing data.

## DRC disposition

Native Magic reports exactly 220 instances of one category: “Metal enclosure
of ContBar < 0.05um (`CntB.h1`)”. The retained hierarchy review attributes 108
markers to `sg13g2_RCClampResistor` leaf geometry and 112 to
`sg13g2_SecondaryProtection`. Native KLayout reports exactly 24 instances of
`metal1_pin_Offgrid`, all in the PDK subcells `sg13g2_Clamp_P15N15D` and
`sg13g2_Clamp_N15N15D`.

The independent OpenADA replay used KLayout `0.30.9`, the pinned PDK's
`ihp-sg13g2.drc`, `topcell=chip_top`, `run_mode=deep`, four threads, recommended
rules disabled as in the flow, no waiver file, and the PDK `SOURCES` record as
explicit provenance. Native execution completed in 558,206 ms;
`execution.status` is `completed` and `engineering.status` is `fail` with 24
unwaived markers. The retained LYRDB and transcript hashes are
`f6ebe3fda1bcdf376317b61f040f23c905b896c08cf5ca2ade098326fcff9307`
and `ce7d17b48f63b7103e24a3566975931b4d2643e4af47563962a30daa0266c070`.

[IHP Open PDK issue #683](https://github.com/IHP-GmbH/IHP-Open-PDK/issues/683)
tracks off-grid shapes in the same I/O GDS class and remains open. The official
template previously recorded a related occurrence in
[template issue #16](https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template/issues/16).
Those issues support classifying the markers for PDK-owner review; they do not
contain a blanket foundry waiver for this design. Conversely,
[IHP Open PDK issue #922](https://github.com/IHP-GmbH/IHP-Open-PDK/issues/922)
discusses a different Magic illegal-overlap class and cannot justify ignoring
this run's `CntB.h1` markers. The Magic/KLayout disagreement is preserved as two
open findings rather than resolving the gate with the more favorable deck.

## LVS parser defect, repair, and boundary

LibreLane's native Netgen report concludes “Circuits match uniquely”: 623
devices versus 623, 462 nets versus 462, equivalent pins, and no reported
device, net, property, or unmatched-object differences.

The first OpenADA replay executed Netgen `1.5.321` successfully against the
final extracted SPICE and powered PNL using the pinned PDK's
`ihp-sg13g2_setup.tcl`. Its native report and JSON agreed with that unique
match, but OpenADA correctly returned `engineering.status: unknown`: Netgen's
hierarchical JSON contained 44 exact pin-only auxiliary records plus one named
`chip_top` comparison, while the parser required every record to carry two cell
names. The retained report, JSON, and initial transcript hash to
`998fb204b62d8874694f5adb51bc84c1c9d5fe8c0a676fc1ca6a70c4499713b5`,
`08d46910489a77f303957d6660804f5d9a1de0ab81fa8ef6ce929b35e4a36a9e`,
and `001b642e95b576aac701e830829e7cf47b1a7529fb846e27e0e56b10d69a792f`.

The parser now accepts only an exact `{"pins": [left, right]}` auxiliary shape
when both bounded string lists are identical. Unequal pin lists, partial known
shapes, unknown fields, duplicate requested tops, and all existing malformed
cases still fail closed. Focused contract tests cover the passing and rejecting
forms.

A fresh replay after the repair completed and normalized to
`engineering.status: pass`: one requested `chip_top` comparison, 623/623
devices, 462/462 nets, and zero mismatches. The report, JSON, and fresh
transcript hashes are respectively
`998fb204b62d8874694f5adb51bc84c1c9d5fe8c0a676fc1ca6a70c4499713b5`,
`08d46910489a77f303957d6660804f5d9a1de0ab81fa8ef6ce929b35e4a36a9e`,
and `493126f8d053dfc3abc40c0cc2652dea8398a06b370caa8e8493afb2a42324c4`.
This closes the parser-compatibility gap without changing the native
engineering evidence.

The repair is tested but is not represented as a published release receipt.
Seventy affected parser/driver/conformance selections pass, while the complete
repository suite deliberately leaves 15 release-integrity tests failing: the
runtime change moves OpenADA's global semantic-subject hash from the value
bound by the seven checked-in release chains. Project policy requires the exact
source to be committed and all seven native chains to be replayed from an
unchanged clean checkout before their receipts and release index can be
refreshed. This evaluation neither changed Git history nor rewrote those
receipts, so the release-coverage gate remains fail-closed rather than treating
unit coverage as publication evidence.

One important LVS gap remains: LibreLane ran Magic extraction with
`MAGIC_EXT_USE_GDS=false`, so the matching SPICE was extracted from the
implemented DEF/LEF representation, not independently from the final streamed
GDS. OpenADA also cannot enumerate arbitrary transitive Tcl access in the
Netgen setup. The pass is therefore a bounded netlist-comparison result, not a
claim that final GDS-versus-source signoff is independently closed.

## Identity ledger and retained gaps

The final container-side bootstrap manifest is frozen at revision 67 with
`freeze_ready: true`, 32 collateral records, eight hashed tool identities, and
SHA-256
`6c392aeb3d55bce71417d7213b27960c85c47d244c2b841595448e0fc972070c`.
It contains 15 gap records: two resolved and 13 open. Detailed-routing evidence
resolved the template's allowed-congestion gap, and the repaired replay
resolved the Netgen JSON compatibility gap.

The open records preserve:

- both final DRC categories and the disabled illegal-overlap checker;
- I/O maximum slew, 74 unannotated drivers, absent package-aware voltage-source
  locations, and zero-delay/no-SDF gate simulation;
- final LVS extraction from DEF/LEF rather than streamed GDS;
- unsupported OpenROAD `LEF58_ENCLOSURE` forms;
- LibreLane runtime/template-lock skew;
- missing OpenADA semantics for HDL simulation and the native physical chain;
- unresolved package, bonding, board, and power-sequencing collateral; and
- the IHP Preview-PDK, shuttle-checklist, and external-acceptance boundary.

`freeze_ready` means the manifest's required identity records and hashes are
internally complete in the original `/work` and `/foss` mount namespace. It
does not mean that its open engineering gaps pass, and it does not make those
container paths portable.

## Session UX and product changes

The three qualitative sessions produced a useful progression:

- the baseline revealed version lottery, accidental core-only scope, incomplete
  native evidence, and long silent operations;
- the forward test selected local, pinned inputs first and stopped at an honest
  compatibility blocker without downloads or EDA; and
- a post-change smoke session used `python3`, exact stage/kind enums,
  `seal-ring.config` as the pre-run generator input, and explicit
  `freeze_ready`/missing-requirement output. It created and validated a draft
  but did not freeze, access the network, or run EDA.

The resulting repository changes add a `bootstrap-asic-project` coordinator,
an atomic draft/frozen manifest helper, a reviewed IHP full-chip route, exact
tool/collateral/gap identities, fail-closed freeze readiness, and regression
tests. The IHP reference now calls out package-aware source locations, I/O
slew/load and unannotated-driver review, SDF versus zero-delay gate testing, the
known off-grid issue, and preservation of Magic/KLayout disagreement. The
Netgen parser fix was driven by retained real-tool output and remains strict on
ambiguous shapes.

Important product gaps remain:

- OpenADA has no semantic operation for behavioral or gate-level HDL
  simulation, padframe assembly, floorplan, PDN, placement, CTS, routing, PEX,
  routed MCMM timing, IR analysis, or final submission assembly;
- the available digest-pinned OCI runtime does not match the official
  template's LibreLane lock, and there is no turnkey compatible runtime recipe;
- native-gap work still needs one bounded envelope for executable, argv,
  environment, cwd, timeout, exit status, inputs, and output hashes;
- package-aware electrical modeling and an independent streamed-GDS LVS route
  need explicit onboarding gates;
- long native operations need regular user-visible progress summaries; and
- the isolated installed plugin cache remains about 902 MiB because development
  material such as `.strategy`, `.venv`, and `.git` is copied into the payload.

## Durable package boundary

The durable [`candidate/`](candidate/) directory is a source overlay, not a run
archive. Its manifest binds 15 replacements and one deletion against the exact
official template commit; `SOURCE_MANIFEST.sha256` binds the 12 authoritative
candidate inputs. The package audit verified those hashes and found no
generated binaries, reports, symlinks, or undeclared files in the overlay.

Generated GDS, DEF, SPEF, SDF, netlists, simulator products, waveforms, native
logs, OpenADA envelopes, and the container-path ledger remain under the
ephemeral external `/dev/shm/openada-vibe-cpu-20260716/` tree and are deliberately
excluded. The hashes in this report describe that retained observation; the
source-only package cannot by itself prove those artifacts exist or reproduce
them without reconstructing the exact pinned base, PDK, OCI runtime, and flow.
It is not a submission archive.

The appropriate next engineering work is not to relabel the present markers.
It is to obtain explicit IHP disposition or corrected PDK collateral, rerun both
final DRC engines, perform independent extraction from the final stream,
resolve pad slew against an actual package/load model, add SDF-aware gate
testing, close the package/bond plan, and then re-evaluate the current
[IHP IP development](https://github.com/IHP-GmbH/Open-Silicon-MPW/blob/main/IP-development-steps.md)
and [submission](https://github.com/IHP-GmbH/Open-Silicon-MPW/blob/main/Submission-process.md)
requirements. Nothing in this evaluation substitutes for that review.
