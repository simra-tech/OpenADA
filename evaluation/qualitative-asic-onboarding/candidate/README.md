# Accumulator CPU candidate overlay

This directory is the durable source-and-identity engineering delta produced during
the qualitative ASIC-onboarding evaluation. Apply it to the exact official
IHP full-chip template revision recorded in
[`overlay-manifest.json`](overlay-manifest.json); it is not a standalone
project and it intentionally contains no tool installation, PDK, run tree,
generated netlist, report, or manufacturing view.

## Exact source stack

The authoritative base is
`IHP-GmbH/ihp-sg13g2-librelane-template` revision
`0418301723d86133de686ef743cfd668bb3d11d4`. Its unchanged `flake.lock`
selects LibreLane reference `3.0.0` at revision
`69b2067bd2b5eb89b84649b76e9edaa9e51e6735`. Its Makefile selects the
`ihp-sg13g2` open PDK at revision
`3b5a704ba6738aa686b08706187830e6284d2a10`; the overlay preserves that pin.

The template continues to provide the unmodified full-chip shell
(`src/chip_top.sv`), timing constraints (`librelane/chip_top.sdc`), bondpad
LEF/GDS/Verilog collateral, Nix environment, and license. The fifteen files
in the manifest replace template files or add the CPU tests, gate wrapper,
CI gate, project specification, pin/package/license records, and source/runtime
identity. Delete `librelane/pdn_cfg.tcl`: it only described the upstream
example's two SRAM macros, while this design uses LibreLane's standard-cell
PDN.

To reconstruct the engineering tree, first make a clean detached checkout of
the base revision, verify `HEAD`, copy the fifteen overlay files to the same
relative paths, and remove the one declared path. For example, from this
OpenADA repository:

```sh
OVERLAY="$(pwd)/evaluation/qualitative-asic-onboarding/candidate"
(
  cd "$OVERLAY"
  jq -r '.replace[] | "\(.sha256)  \(.path)"' overlay-manifest.json | sha256sum -c -
)
git clone https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template.git /tmp/vibe-cpu
git -C /tmp/vibe-cpu checkout --detach 0418301723d86133de686ef743cfd668bb3d11d4
test "$(git -C /tmp/vibe-cpu rev-parse HEAD)" = 0418301723d86133de686ef743cfd668bb3d11d4
jq -r '.replace[].path' "$OVERLAY/overlay-manifest.json" | while IFS= read -r path; do
  install -Dm644 "$OVERLAY/$path" "/tmp/vibe-cpu/$path"
done
git -C /tmp/vibe-cpu rm -- librelane/pdn_cfg.tcl
(cd /tmp/vibe-cpu && sha256sum -c SOURCE_MANIFEST.sha256)
```

The SHA-256 values in `overlay-manifest.json` bind the copied content. A
reconstruction is not the same candidate if the base, PDK, flow lock, file
hashes, or deletion differs.

## Design represented

`chip_core` is a small 8-bit accumulator CPU with a 6-bit program counter and
a wait-state-capable, 64-byte external memory interface. Every instruction is
two bytes. The instruction set covers immediate and memory load/add, store,
two auxiliary input banks, unconditional and zero/nonzero conditional branch,
halt, and no-op. Unknown opcodes halt.

The inherited padframe exposes the address on `output_PAD[5:0]`, write on
`output_PAD[6]`, halt on `output_PAD[7]`, and data on `bidir_PAD[7:0]`.
`input_PAD[0]` is memory ready; `input_PAD[8:1]` and `input_PAD[9]` are the
auxiliary input banks. The cocotb test exercises both banks so that every
digital input pad is functionally retained. Analog pads are reserved.

The RTL test is intentionally PDK-independent and targets `chip_core` rather
than the padframe:

```sh
cd /tmp/vibe-cpu
python3 -m venv .venv
.venv/bin/pip install -r cocotb/requirements.txt
make sim SIM=verilator
```

## Routed gate simulation

The `sim-gl` target is a zero-delay functional test of a routed `chip_top`
netlist. It compiles the netlist with the exact PDK's standard-cell and I/O-pad
models, adds `chip_top_gl_wrapper.sv` to split bidirectional pad drive/sample
signals for cocotb, runs the complete CPU program with varied wait states, and
checks that the data pads are released after halt. Pass an explicit fresh
netlist path when retaining evidence instead of relying on the latest-run
default:

```sh
make sim-gl \
  PDK_ROOT=/absolute/path/to/pinned/pdk-root \
  GL_NETLIST=/absolute/path/to/fresh/routed/chip_top.nl.v
```

The qualitative evaluation verified this routed pad-level test with Icarus;
the self-checking testcase completed at 896 ns. Its generated xUnit file,
compiled simulator product, waveform, PDK models, and routed netlist are not
copied into this source overlay. The adapted GitHub workflow is included so a
future implementation must cross the same gate after producing its own routed
netlist.

## Scope and limitations

This overlay preserves reproducible design and verification inputs, not a
self-contained physical-flow evidence bundle. The observed routed simulation
supports zero-delay pad-level functional behavior for the exact netlist, PDK
models, simulator, and runtime used by that run. It is not formal equivalence,
does not annotate SDF, and makes no claim about timing closure, route-rule
cleanliness, DRC, LVS, antenna, density, IR drop, generated GDS/OASIS
correctness, or foundry signoff.

The CPU depends on an external memory/host and contains no boot ROM or on-chip
program memory. The RTL test checks the core bus protocol with zero to two wait
cycles; the routed test adds functional I/O models but still does not model
extracted parasitics or package/board electrical behavior. `PACKAGE_PLAN.md`
records the unassigned package and bonding work, so this overlay is not a
submission candidate. The IHP open PDK is preview collateral and must not be
represented as foundry-qualified signoff. Any run performed with a runtime
other than the locked LibreLane stack is a separate tool identity and needs
explicit requalification; `OCI_RUNTIME.json` identifies the separately
evaluated container.

The live integration checkout's frozen `.openada/bootstrap-manifest.json` is
deliberately omitted. Its `/work` and `/foss` paths are valid only inside the
original container mount namespace; its post-integration validation belongs
in the evaluation report, not in a relocatable overlay. The mount paths in
`OCI_RUNTIME.json` are retained only as historical runtime identity and are
not relocation-safe project paths. `SOURCE_PROVENANCE.md` is retained because
`SOURCE_MANIFEST.sha256` binds it, but its `/dev/shm` checkout paths are also
historical provenance rather than reconstruction paths. The presentation-only
live README and `.gitignore` addition remain omitted.
