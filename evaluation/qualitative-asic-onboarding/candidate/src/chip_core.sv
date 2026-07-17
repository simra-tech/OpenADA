// SPDX-FileCopyrightText: 2025 LibreLane Template Contributors
// SPDX-FileCopyrightText: 2026 OpenADA Vibe CPU Contributors
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

// Tiny accumulator CPU with a wait-state-capable, 64-byte external memory bus.
// Every instruction is exactly two bytes: an opcode followed by an operand.
module chip_core #(
    parameter integer NUM_INPUT_PADS  = 10,
    parameter integer NUM_OUTPUT_PADS = 8,
    parameter integer NUM_BIDIR_PADS  = 8,
    parameter integer NUM_ANALOG_PADS = 8
) (
    input  logic                         clk,
    input  logic                         rst_n,

    input  wire [NUM_INPUT_PADS-1:0]     input_in,
    output wire [NUM_OUTPUT_PADS-1:0]    output_out,
    input  wire [NUM_BIDIR_PADS-1:0]     bidir_in,
    output wire [NUM_BIDIR_PADS-1:0]     bidir_out,
    output wire [NUM_BIDIR_PADS-1:0]     bidir_oe,
    inout  wire [NUM_ANALOG_PADS-1:0]    analog
);

    localparam logic [7:0] OP_NOP  = 8'h00;
    localparam logic [7:0] OP_LDI  = 8'h10;
    localparam logic [7:0] OP_LDA  = 8'h11;
    localparam logic [7:0] OP_IN   = 8'h12;
    localparam logic [7:0] OP_ADDI = 8'h20;
    localparam logic [7:0] OP_ADD  = 8'h21;
    localparam logic [7:0] OP_STA  = 8'h30;
    localparam logic [7:0] OP_JMP  = 8'h40;
    localparam logic [7:0] OP_JZ   = 8'h41;
    localparam logic [7:0] OP_JNZ  = 8'h42;
    localparam logic [7:0] OP_HLT  = 8'hff;

    typedef enum logic [2:0] {
        FETCH_OPCODE,
        FETCH_OPERAND,
        LOAD_MEMORY,
        ADD_MEMORY,
        STORE_MEMORY,
        HALTED
    } state_t;

    state_t state;
    logic [5:0] program_counter;
    logic [5:0] data_address;
    logic [7:0] accumulator;
    logic [7:0] opcode;

    logic [5:0] bus_address;
    logic [7:0] bus_write_data;
    logic       bus_write;
    logic       halted;

    wire ready = input_in[0];

    // The CPU does not drive the analog pads. The leading zero makes merely
    // observing reserved/floating pads deterministic and synthesis-neutral.
    (* keep *) wire unused_analog_pads = &{1'b0, analog};

    always_comb begin
        bus_address    = program_counter;
        bus_write_data = accumulator;
        bus_write      = 1'b0;

        case (state)
            LOAD_MEMORY,
            ADD_MEMORY: begin
                bus_address = data_address;
            end
            STORE_MEMORY: begin
                bus_address = data_address;
                bus_write   = 1'b1;
            end
            default: begin
                // Opcode and operand fetches use program_counter. HALTED leaves
                // the next sequential address visible for debug.
            end
        endcase
    end

    assign halted     = (state == HALTED);
    assign output_out = {halted, bus_write, bus_address};
    assign bidir_out  = bus_write_data;
    assign bidir_oe   = {NUM_BIDIR_PADS{bus_write}};

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state           <= FETCH_OPCODE;
            program_counter <= 6'h00;
            data_address    <= 6'h00;
            accumulator     <= 8'h00;
            opcode          <= OP_NOP;
        end else begin
            case (state)
                FETCH_OPCODE: begin
                    if (ready) begin
                        opcode          <= bidir_in;
                        program_counter <= program_counter + 6'd1;
                        state           <= FETCH_OPERAND;
                    end
                end

                FETCH_OPERAND: begin
                    if (ready) begin
                        case (opcode)
                            OP_NOP: begin
                                program_counter <= program_counter + 6'd1;
                                state           <= FETCH_OPCODE;
                            end
                            OP_LDI: begin
                                accumulator     <= bidir_in;
                                program_counter <= program_counter + 6'd1;
                                state           <= FETCH_OPCODE;
                            end
                            OP_LDA: begin
                                data_address    <= bidir_in[5:0];
                                program_counter <= program_counter + 6'd1;
                                state           <= LOAD_MEMORY;
                            end
                            OP_IN: begin
                                // Operand bit zero selects all nine auxiliary
                                // input pads without adding another bus phase.
                                accumulator <= bidir_in[0]
                                    ? {7'b0, input_in[9]}
                                    : input_in[8:1];
                                program_counter <= program_counter + 6'd1;
                                state           <= FETCH_OPCODE;
                            end
                            OP_ADDI: begin
                                accumulator     <= accumulator + bidir_in;
                                program_counter <= program_counter + 6'd1;
                                state           <= FETCH_OPCODE;
                            end
                            OP_ADD: begin
                                data_address    <= bidir_in[5:0];
                                program_counter <= program_counter + 6'd1;
                                state           <= ADD_MEMORY;
                            end
                            OP_STA: begin
                                data_address    <= bidir_in[5:0];
                                program_counter <= program_counter + 6'd1;
                                state           <= STORE_MEMORY;
                            end
                            OP_JMP: begin
                                program_counter <= bidir_in[5:0];
                                state           <= FETCH_OPCODE;
                            end
                            OP_JZ: begin
                                if (accumulator == 8'h00) begin
                                    program_counter <= bidir_in[5:0];
                                end else begin
                                    program_counter <= program_counter + 6'd1;
                                end
                                state <= FETCH_OPCODE;
                            end
                            OP_JNZ: begin
                                if (accumulator != 8'h00) begin
                                    program_counter <= bidir_in[5:0];
                                end else begin
                                    program_counter <= program_counter + 6'd1;
                                end
                                state <= FETCH_OPCODE;
                            end
                            OP_HLT: begin
                                program_counter <= program_counter + 6'd1;
                                state           <= HALTED;
                            end
                            default: begin
                                // Unknown opcodes halt instead of causing an
                                // uncontrolled memory transaction.
                                program_counter <= program_counter + 6'd1;
                                state           <= HALTED;
                            end
                        endcase
                    end
                end

                LOAD_MEMORY: begin
                    if (ready) begin
                        accumulator <= bidir_in;
                        state       <= FETCH_OPCODE;
                    end
                end

                ADD_MEMORY: begin
                    if (ready) begin
                        accumulator <= accumulator + bidir_in;
                        state       <= FETCH_OPCODE;
                    end
                end

                STORE_MEMORY: begin
                    if (ready) begin
                        state <= FETCH_OPCODE;
                    end
                end

                HALTED: begin
                    state <= HALTED;
                end

                default: begin
                    state <= HALTED;
                end
            endcase
        end
    end

endmodule

`default_nettype wire
