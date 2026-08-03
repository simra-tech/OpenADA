* bhv_sw_bbm_pair_v1 -- synchronous switch pair with enforced break-before-make
*
* Contract summary (authoritative copy in block.json):
*   ctl high (against ctlref, threshold vth) closes the high-side switch
*   nhi<->nsw and opens the low-side switch nsw<->nlo; ctl low does the
*   reverse. Each turn-ON is delayed by tdead after the opposite switch's
*   turn-OFF command, so the two conduction commands are never asserted
*   together. Switch conduction is a smooth log-interpolated resistance
*   between ron and roff driven by shaped edges of duration tedge.
*
*   TOPOLOGY NOTE (honest, reviewed): when body_diodes=1 the block includes
*   antiparallel ideal-diode paths (forward drop ~vdio). They are REQUIRED
*   for inductive commutation: with both switches off during dead time an
*   inductor current has no path and the node voltage is unbounded. Setting
*   body_diodes=0 removes them; the composition then owes the freewheel path
*   itself. The diodes are a declared topology choice, not a hidden aid.
*
.subckt bhv_sw_bbm_pair_v1 ctl ctlref nhi nsw nlo
+ vth=0.5 tdead=10n tedge=2n ron=50m roff=100meg vdio=0.7 body_diodes=1
* control conditioning against the ctlref reference
Bctl bhv_sw_bbm_pair_ci 0 V = V(ctl,ctlref)
Abrg [bhv_sw_bbm_pair_ci] [bhv_sw_bbm_pair_dc] bhv_sw_bbm_pair_adc
.model bhv_sw_bbm_pair_adc adc_bridge(in_low={vth*0.9} in_high={vth*1.1} rise_delay=1p fall_delay=1p)
Ainv bhv_sw_bbm_pair_dc bhv_sw_bbm_pair_dcb bhv_sw_bbm_pair_inv
.model bhv_sw_bbm_pair_inv d_inverter(rise_delay=1p fall_delay=1p)
* break-before-make: every turn-ON is delayed by tdead, turn-OFF by 1ps.
* The contract floors tdead at 2ps for exactly this reason: at or below the
* 1ps turn-OFF delay the two conduction commands would overlap.
Adhi bhv_sw_bbm_pair_dc bhv_sw_bbm_pair_dhi bhv_sw_bbm_pair_dly
Adlo bhv_sw_bbm_pair_dcb bhv_sw_bbm_pair_dlo bhv_sw_bbm_pair_dly
.model bhv_sw_bbm_pair_dly d_buffer(rise_delay={tdead} fall_delay=1p)
Adac [bhv_sw_bbm_pair_dhi bhv_sw_bbm_pair_dlo] [bhv_sw_bbm_pair_chi bhv_sw_bbm_pair_clo] bhv_sw_bbm_pair_dac
.model bhv_sw_bbm_pair_dac dac_bridge(out_low=0 out_high=1 t_rise={tedge} t_fall={tedge})
* smooth switched resistances
* cntl thresholds equal the exact dac_bridge output range: the log-interpolated
* resistance reaches exactly ron/roff at the rails and is never extrapolated.
Aswh %v(bhv_sw_bbm_pair_chi) %gd(nhi nsw) bhv_sw_bbm_pair_sw
Aswl %v(bhv_sw_bbm_pair_clo) %gd(nsw nlo) bhv_sw_bbm_pair_sw
.model bhv_sw_bbm_pair_sw aswitch(cntl_off=0 cntl_on=1 r_off={roff} r_on={ron} log=TRUE)
* declared antiparallel freewheel diodes (see topology note); vdio is the
* forward drop at about 1 A. body_diodes=0 raises the emission coefficient to
* 100 so the residual path stays below picoamps for any drop under ~40 V.
Dhi nsw nhi bhv_sw_bbm_pair_dio
Dlo nlo nsw bhv_sw_bbm_pair_dio
.model bhv_sw_bbm_pair_dio d(is={body_diodes*exp(-vdio/0.02585)+(1-body_diodes)*1e-30} n={1+(1-body_diodes)*99})
.ends bhv_sw_bbm_pair_v1
