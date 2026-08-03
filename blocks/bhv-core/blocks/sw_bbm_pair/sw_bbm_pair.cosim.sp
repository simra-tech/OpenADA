* bhv_sw_bbm_pair_v1 -- synchronous BBM switch pair, mixed-signal (d_cosim) backend
*
* Contract summary (authoritative copy in block.json): identical to the
* ngspice-native backend -- ctl high (against ctlref, threshold vth) closes
* nhi<->nsw and opens nsw<->nlo; every turn-ON is delayed by tdead after the
* opposite switch's turn-OFF command. Conduction is the smooth
* log-interpolated aswitch resistance between ron and roff; the declared
* antiparallel body diodes carry inductive commutation (see the native
* source's topology note, which applies unchanged).
*
* Mixed-signal realization (reviewed deviations from the native event graph):
*   - The complementary drive split (chi = ctl, clo = NOT ctl) is the
*     compiled digital core bhv_sw_bbm_pair_v1_core, bound by the
*     composer-generated model bhv_sw_bbm_pair_cosim (a d_cosim card this
*     source deliberately does NOT define).
*   - The 2-state core holds its last value while the control bridge output
*     is UNKNOWN, so inside the declared band (vth*0.9 .. vth*1.1) the drive
*     commands switch at the band-exit crossing instead of ramping through a
*     midpoint. Both behaviors lie inside the band the contract declares
*     unspecified; outside the band the realizations agree.
*   - The core contributes a fixed ~6 ps event delay to BOTH drive commands
*     symmetrically (measured on ngspice-45.2 and the prod image's
*     ngspice-46), which cancels in the BBM interval arithmetic; turn-ON
*     remains delayed by exactly the d_buffer's tdead against the opposite
*     turn-OFF.
*
.subckt bhv_sw_bbm_pair_v1 ctl ctlref nhi nsw nlo
+ vth=0.5 tdead=10n tedge=2n ron=50m roff=100meg vdio=0.7 body_diodes=1
* control conditioning against the ctlref reference
Bctl bhv_sw_bbm_pair_ci 0 V = V(ctl,ctlref)
Abrg [bhv_sw_bbm_pair_ci] [bhv_sw_bbm_pair_dc] bhv_sw_bbm_pair_adc
.model bhv_sw_bbm_pair_adc adc_bridge(in_low={vth*0.9} in_high={vth*1.1} rise_delay=1p fall_delay=1p)
* compiled complementary drive core: ports bind in the declared core order
* (c) -> (chi, clo); the compile refuses any Verilated reordering.
Acore [bhv_sw_bbm_pair_dc] [bhv_sw_bbm_pair_dhc bhv_sw_bbm_pair_dlc] bhv_sw_bbm_pair_cosim
* break-before-make: every turn-ON is delayed by tdead, turn-OFF is immediate
Adhi bhv_sw_bbm_pair_dhc bhv_sw_bbm_pair_dhi bhv_sw_bbm_pair_dly
Adlo bhv_sw_bbm_pair_dlc bhv_sw_bbm_pair_dlo bhv_sw_bbm_pair_dly
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
