# Package and bonding plan

The full-chip layout uses the official template's bondpad and pad-ring geometry.
No package, bond-wire map, board connector, decoupling network, or power-up
sequence has been selected. The physical signal names and side placement in
`PINOUT.md` and `librelane/config.yaml` are the current interface contract.

Before submission, select the shuttle-supported package, map every bondpad,
review 1.2 V core and 3.3 V IO power sequencing, confirm clock/reset levels,
budget simultaneous-switching current, and add the accepted bonding drawing.
This open item prevents submission-candidate status but does not prevent a
full-chip layout candidate from being evaluated.
