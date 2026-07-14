# `circuit.simulate` conformance proof

`fixtures/rc-transient.cir` is the first same-intent proof fixture. It uses
only ideal sources, a resistor, and a capacitor, so both ngspice and Xyce can
run the same authoritative deck without a PDK or compact-model dependency.

The fixture contains exactly one transient analysis and deliberately contains
no `.include`, `.lib`, `.control`, `.measure`, or `.print` directive. The
driver owns fresh native-result selection and evidence capture.

The proof is intentionally narrow: it establishes fresh, structurally valid
transient analysis evidence. It does not establish model fidelity, circuit
requirements, or signoff suitability.
