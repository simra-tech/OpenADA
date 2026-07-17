# Vibe16 IHP SG13G2 full-chip project specification

## Finish line

The intended deliverable is an educational **open-PDK full-chip tapeout
candidate**: synthesized CPU RTL, routed standard-cell core, IHP IO/ESD and
power pads, bondpads, pad ring, seal ring, fill/density, final GDS, powered
reference netlist, and retained timing/DRC/LVS reports. Foundry signoff and
submission acceptance are outside this project and are not claimed.

## Function and interfaces

Vibe16 is a minimal 8-bit accumulator CPU with a 6-bit byte address space and
a wait-state-capable external unified memory bus. Instructions are two bytes.
The exact opcodes and pad mapping are documented in `README.md` and
`PINOUT.md`. There is no on-chip program or data memory; the package-level
environment must provide memory and the `ready` handshake.

The CPU target clock is 50 MHz (`CLOCK_PERIOD: 20` ns). Reset is synchronous,
active-low, and sampled on `clk_PAD`. There is one core supply domain (`VDD` /
`VSS`, nominal 1.2 V) and one IO supply domain (`IOVDD` / `IOVSS`, nominal
3.3 V), matching the selected IHP IO Liberty voltage map. Package sequencing
and board-level electrical review remain open handoff items.

## Physical and verification scope

The official template retains its 1600 um square die, 870 um square core area,
full signal/power pad ring, bondpads, and seal ring. The candidate must pass a
self-checking RTL test with varied memory wait states, strict lint, structural
elaboration, complete Liberty mapping, routed multi-corner timing, route and
antenna checks, stream-out XOR, density, official-deck DRC, and final
GDS-versus-powered-reference LVS. IR-drop is reviewed as evidence, not as a
guaranteed package model.

## Identity and license policy

The base template is pinned to commit
`0418301723d86133de686ef743cfd668bb3d11d4`; its lock pins LibreLane commit
`69b2067bd2b5eb89b84649b76e9edaa9e51e6735`. The PDK is pinned to
`3b5a704ba6738aa686b08706187830e6284d2a10`. The evaluated container is pinned
in `OCI_RUNTIME.json`; its LibreLane 3.1 development build differs from the
template lock, so compatibility is established only by this candidate's
retained full-flow evidence, if that flow finishes successfully.

Only permissively licensed authored/template RTL and collateral are accepted.
The IHP Open PDK remains Preview and is not intended for production use.
