// SPDX-FileCopyrightText: 2026 OpenADA Vibe CPU Contributors
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

// Present the physical chip's bidirectional pads to cocotb as separate drive
// and sample signals. The chip_top instantiated here comes from LibreLane's
// routed gate-level netlist, not from src/chip_top.sv.
module chip_top_gl_wrapper (
    input  logic       clk,
    input  logic       rst_n,
    input  logic [9:0] input_in,
    input  logic [7:0] bidir_in,
    input  logic       bidir_drive,
    output wire  [7:0] output_out,
    output wire  [7:0] bidir_out
);
    tri clk_pad;
    tri rst_n_pad;
    tri [9:0] input_pad;
    tri [7:0] output_pad;
    tri [7:0] bidir_pad;
    tri [7:0] analog_pad;

    assign clk_pad   = clk;
    assign rst_n_pad = rst_n;
    assign input_pad = input_in;
    assign bidir_pad = bidir_drive ? bidir_in : 8'bz;

    assign output_out = output_pad;
    assign bidir_out  = bidir_pad;

    chip_top dut (
        .clk_PAD    (clk_pad),
        .rst_n_PAD  (rst_n_pad),
        .input_PAD  (input_pad),
        .output_PAD (output_pad),
        .bidir_PAD  (bidir_pad),
        .analog_PAD (analog_pad)
    );
endmodule

`default_nettype wire
