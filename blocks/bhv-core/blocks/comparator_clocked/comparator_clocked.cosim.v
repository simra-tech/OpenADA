// bhv_comparator_clocked_v1_core -- sampled decision core for the
// xspice-cosim backend. Compiled by Verilator into the d_cosim shared
// object (openada.cosim_compile); NEVER simulated by any other path.
//
// The core is deliberately parameter-free: every declared block parameter
// (levels, latencies, thresholds, bands) lives in the analog wrapper where
// ngspice parameter expansion works. This module is only the retained
// sampled decision: q takes the decision input on the rising clock event.
//
// Port order note: the a-device binds ports in the order the Verilated
// header emits them, which the contract declares (core_inputs/core_outputs)
// and the compile re-verifies. `timescale is required by the event bridge.
`timescale 1ps/1ps
module bhv_comparator_clocked_v1_core(
  input clk,
  input din,
  output reg q
);
  // Deterministic default decision 0 (out at vlo) until the first rising
  // clock event, exactly as the contract's state clause declares.
  initial q = 1'b0;
  always @(posedge clk) q <= din;
endmodule
