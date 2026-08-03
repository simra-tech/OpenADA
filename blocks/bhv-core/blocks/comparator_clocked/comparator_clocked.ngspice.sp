* bhv_comparator_clocked_v1 -- clocked comparator with declared decision latency
*
* Contract summary (authoritative copy in block.json):
*   On the clock crossing vth_clk (rising, ambiguity band clk_band), the sign
*   of V(inp,vss)-V(inn,vss)+hysteresis is sampled; out settles toward the new
*   level so that the 50% output crossing occurs td after the clock threshold
*   crossing. Hysteresis vhyst shifts the effective threshold by +/-vhyst/2
*   as positive feedback from the retained decision. dif_band is the declared
*   input ambiguity band of the decision bridge. All output levels are
*   defined against the vss reference port.
*
* Latency arithmetic (event chain, all stage delays explicit):
*   clock adc_bridge event delay 1ps + d_dff clk_delay (ngspice adds the
*   1ps rise/fall delay to clk_delay, leaving a ~1ps residual inside the
*   declared 2ps bound) + dac 50% ramp tedge/2 = td, hence
*   clk_delay = td - tedge/2 - 1ps. This backend needs only td > tedge/2 +
*   2ps; the CONTRACT declares the stricter td > tedge/2 + 7ps so the same
*   parameterization also holds on the xspice-cosim backend (whose compiled
*   core adds a 5ps event-bridge residual).
*
.subckt bhv_comparator_clocked_v1 inp inn clk out vss
+ vhi=1 vlo=0 td=2n tedge=1n vhyst=0 vth_clk=0.5 clk_band=10m dif_band=1u rout=100
* decision input with hysteresis feedback (fb is the retained decision 0..1)
Bdif bhv_comparator_clocked_dif 0 V = V(inp,vss) - V(inn,vss) + {vhyst}*(V(bhv_comparator_clocked_fb)-0.5)
* decision bridge: declared ambiguity band dif_band around zero
Abrgd [bhv_comparator_clocked_dif] [bhv_comparator_clocked_dd] bhv_comparator_clocked_adcd
.model bhv_comparator_clocked_adcd adc_bridge(in_low={-dif_band/2} in_high={dif_band/2} rise_delay=1p fall_delay=1p)
* clock bridge: declared ambiguity band clk_band around vth_clk
Bclk bhv_comparator_clocked_ck 0 V = V(clk,vss)
Abrgc [bhv_comparator_clocked_ck] [bhv_comparator_clocked_dc] bhv_comparator_clocked_adcc
.model bhv_comparator_clocked_adcc adc_bridge(in_low={vth_clk-clk_band/2} in_high={vth_clk+clk_band/2} rise_delay=1p fall_delay=1p)
* decision element
Adff bhv_comparator_clocked_dd bhv_comparator_clocked_dc NULL NULL bhv_comparator_clocked_dq NULL bhv_comparator_clocked_dff
.model bhv_comparator_clocked_dff d_dff(clk_delay={td-(tedge/2)-1p} data_load=0.5e-12 rise_delay=1p fall_delay=1p)
* retained-decision feedback for hysteresis (fast edges, internal only).
* out_undef maps a digital UNKNOWN (input inside dif_band at the clock event)
* deterministically to the LOW decision on both paths, keeping the output and
* the hysteresis feedback binary-valid instead of drifting to a midpoint.
Adacf [bhv_comparator_clocked_dq] [bhv_comparator_clocked_fb] bhv_comparator_clocked_dacf
.model bhv_comparator_clocked_dacf dac_bridge(out_low=0 out_high=1 out_undef=0 t_rise=1p t_fall=1p)
* electrical output stage referenced to vss with thevenin rout
Adaco [bhv_comparator_clocked_dq] [bhv_comparator_clocked_oo] bhv_comparator_clocked_daco
.model bhv_comparator_clocked_daco dac_bridge(out_low={vlo} out_high={vhi} out_undef={vlo} t_rise={tedge} t_fall={tedge})
Bout bhv_comparator_clocked_o1 vss V = V(bhv_comparator_clocked_oo)
Rout bhv_comparator_clocked_o1 out {rout}
.ends bhv_comparator_clocked_v1
