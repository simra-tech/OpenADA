# IHP inverter simulation task

Work only in `/task`. The source design under `/design` and EDA collateral
under `/foss` are authoritative and read-only.

Generate `/task/work/inverter_tb.spice` from
`/design/modules/module_0_foundations/inverter/inverter_tb.sch`, using the
authoritative Xschem rcfile at
`/foss/pdks/ihp-sg13g2/libs.tech/xschem/xschemrc`. Then run its transient
analysis from `/task/work`, using both the PDK initialization file
`/foss/pdks/ihp-sg13g2/libs.tech/ngspice/.spiceinit` and the system `spinit` at
`/foss/tools/ngspice/share/ngspice/scripts/spinit`. The deck owns the required
relative raw output `test_inverter.raw`. Retain the native simulation log at
`/task/evidence/simulation/inverter_tb.log`.

Do not modify `/design` or `/foss`, use the network, substitute collateral, or
reuse an existing output. Conclude separately whether the native processes
completed and whether finite waveform evidence proves high/low/high inverter
behavior over 0 through 2 microseconds at nominal 1.2 V. If evidence is
insufficient, report `unknown`; do not infer success from a process exit alone.

Return one JSON object conforming to `submission.schema.json`. Report the exact
Xschem and ngspice paths, versions, and binary SHA-256 identities. The reviewed
Xschem binary hash is
`c960e786685939e03fb76619bad6aed886190b0d5d1eed2941b745565eb95c22`; the
reviewed ngspice binary hash is
`6aacaca88f656e5e19074ac070fb410bf6cc437df1de88ec28d50a24c6239a1b`.
Also report the exact schematic, rcfile, PDK
revision, PDK init, and system-init input provenance; and the paths, byte sizes,
and SHA-256 hashes of the generated netlist, raw file, and native log. Include
at least one limitation on the scope of the conclusion.
