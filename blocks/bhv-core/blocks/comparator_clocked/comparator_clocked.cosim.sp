* bhv_comparator_clocked_v1 -- clocked comparator, mixed-signal (d_cosim) backend
*
* Contract summary (authoritative copy in block.json): identical to the
* ngspice-native backend -- on the rising clock crossing of vth_clk the sign
* of V(inp,vss)-V(inn,vss)+hysteresis is sampled and out settles toward the
* decided level so the 50% output crossing occurs td after the clock
* threshold crossing.
*
* Mixed-signal realization (reviewed deviations from the native event graph):
*   - The sampled decision is the compiled digital core
*     bhv_comparator_clocked_v1_core, bound by the composer-generated model
*     bhv_comparator_clocked_cosim (a d_cosim card this source deliberately
*     does NOT define; no reviewed source ever carries a file path).
*   - The decision bridge is CRISP (in_low = in_high = +dif_band/2): the
*     compiled core is 2-state and holds its last value on a digital
*     UNKNOWN, so an in-band UNKNOWN reaching it would violate the declared
*     deterministic-LOW resolution. Resolving the band in the analog domain
*     keeps the same observable boundary as native (HIGH iff the effective
*     differential exceeds +dif_band/2; everything below decides LOW).
*
* Latency arithmetic (event chain, all stage delays explicit, measured on
* ngspice-45.2 and the prod image's ngspice-46 with identical results):
*   clock adc_bridge event delay 1p + d_cosim model delay 1p + Verilator
*   event-bridge residual 5p + d_buffer delay + dac 50% ramp tedge/2 = td,
*   hence the buffer delay is td - tedge/2 - 7p. The cosim backend therefore
*   requires td > tedge/2 + 7 ps (the native bound is 2 ps).
*
.subckt bhv_comparator_clocked_v1 inp inn clk out vss
+ vhi=1 vlo=0 td=2n tedge=1n vhyst=0 vth_clk=0.5 clk_band=10m dif_band=1u rout=100
* decision input with hysteresis feedback (fb is the retained decision 0..1)
Bdif bhv_comparator_clocked_dif 0 V = V(inp,vss) - V(inn,vss) + {vhyst}*(V(bhv_comparator_clocked_fb)-0.5)
* crisp decision bridge at +dif_band/2 (see deviation note above)
Abrgd [bhv_comparator_clocked_dif] [bhv_comparator_clocked_dd] bhv_comparator_clocked_adcd
.model bhv_comparator_clocked_adcd adc_bridge(in_low={dif_band/2} in_high={dif_band/2} rise_delay=1p fall_delay=1p)
* clock bridge: declared ambiguity band clk_band around vth_clk. Inside the
* band the 2-state core holds its previous clock level, so the rising event
* fires at the in_high crossing -- a deterministic choice within the band
* the contract declares as unspecified.
Bclk bhv_comparator_clocked_ck 0 V = V(clk,vss)
Abrgc [bhv_comparator_clocked_ck] [bhv_comparator_clocked_dc] bhv_comparator_clocked_adcc
.model bhv_comparator_clocked_adcc adc_bridge(in_low={vth_clk-clk_band/2} in_high={vth_clk+clk_band/2} rise_delay=1p fall_delay=1p)
* compiled sampled-decision core: ports bind in the declared core order
* (clk, din) -> (q); the compile refuses any Verilated reordering.
Acore [bhv_comparator_clocked_dc bhv_comparator_clocked_dd] [bhv_comparator_clocked_dq] bhv_comparator_clocked_cosim
* declared decision latency (see arithmetic above)
Adly bhv_comparator_clocked_dq bhv_comparator_clocked_dqd bhv_comparator_clocked_dly
.model bhv_comparator_clocked_dly d_buffer(rise_delay={td-(tedge/2)-7p} fall_delay={td-(tedge/2)-7p})
* retained-decision feedback for hysteresis (fast edges, internal only).
* out_undef covers only the pre-first-event digital state; the 2-state core
* never emits UNKNOWN afterwards.
Adacf [bhv_comparator_clocked_dqd] [bhv_comparator_clocked_fb] bhv_comparator_clocked_dacf
.model bhv_comparator_clocked_dacf dac_bridge(out_low=0 out_high=1 out_undef=0 t_rise=1p t_fall=1p)
* electrical output stage referenced to vss with thevenin rout
Adaco [bhv_comparator_clocked_dqd] [bhv_comparator_clocked_oo] bhv_comparator_clocked_daco
.model bhv_comparator_clocked_daco dac_bridge(out_low={vlo} out_high={vhi} out_undef={vlo} t_rise={tedge} t_fall={tedge})
Bout bhv_comparator_clocked_o1 vss V = V(bhv_comparator_clocked_oo)
Rout bhv_comparator_clocked_o1 out {rout}
.ends bhv_comparator_clocked_v1
