// bhv_sw_bbm_pair_v1_core -- complementary drive core for the xspice-cosim
// backend. Compiled by Verilator into the d_cosim shared object
// (openada.cosim_compile); NEVER simulated by any other path.
//
// The core is deliberately parameter-free: the break-before-make dead time
// (tdead) and every other declared parameter live in the analog wrapper
// where ngspice parameter expansion works. This module carries only the
// complementary drive split; the asymmetric turn-on delays that enforce BBM
// are the wrapper's d_buffer stages.
`timescale 1ps/1ps
module bhv_sw_bbm_pair_v1_core(
  input c,
  output chi,
  output clo
);
  assign chi = c;
  assign clo = ~c;
endmodule
