* bhv_opamp_1p_v1 -- single-pole opamp macromodel with slew and rail clamp
*
* Contract summary (authoritative copy in block.json):
*   Differential gain av0 with one dominant pole placed so the unity-gain
*   bandwidth is gbw; large-signal rate of change is limited to slew; the
*   output clamps to [vlo, vhi] against the vss reference behind a thevenin
*   rout. Internal solve: fixed integrator capacitance c1x = 1 nF,
*   gm = 2*pi*gbw*c1x, tail current = slew*c1x, r1 = av0/gm. The tanh input
*   stage makes slew limiting smooth; small-signal gm at balance is exact.
*
.subckt bhv_opamp_1p_v1 inp inn out vss
+ av0=100k gbw=10meg slew=10meg vhi=1.65 vlo=-1.65 rout=100
.param bhv_opamp_1p_c1x=1n
.param bhv_opamp_1p_gmx={6.283185307*gbw*bhv_opamp_1p_c1x}
.param bhv_opamp_1p_itx={slew*bhv_opamp_1p_c1x}
.param bhv_opamp_1p_r1x={av0/bhv_opamp_1p_gmx}
Bgm 0 bhv_opamp_1p_x I = {bhv_opamp_1p_itx}*tanh({bhv_opamp_1p_gmx/bhv_opamp_1p_itx}*(V(inp,vss)-V(inn,vss)))
R1 bhv_opamp_1p_x 0 {bhv_opamp_1p_r1x}
C1 bhv_opamp_1p_x 0 {bhv_opamp_1p_c1x}
Bclamp bhv_opamp_1p_y vss V = max(min(V(bhv_opamp_1p_x),{vhi}),{vlo})
Ro bhv_opamp_1p_y out {rout}
.ends bhv_opamp_1p_v1
