# IHP SG13G2 native reference

`native.spice` is hand-written from the installed IHP collateral. It does not
import or call OpenADA's PDK binding. The PDK's `dc_lv_nmos.sch` supplies the
device, dimensions, corner and bias range; `sg13g2_moslv_mod.lib` supplies the
`d g s b` terminal order; and the shipped `.spiceinit` supplies all four OSDI
modules. Loading only PSP103 would leave valid IHP resistor, NQS and varicap
models unavailable.

The signed oracle is the current through `Vd_ref`. A negative value means that
the voltage source supplies current to the NMOS. `portable.spice` asks the same
question with the canonical `nmos.core` role and adds isolated circuits for
unit spelling, a semantics-preserving wrapper-pin permutation, source-current
sign reversal, width scaling and drain-supply monotonicity.

This is exploratory open-tool evidence at the pinned PDK revision, not foundry
signoff.
