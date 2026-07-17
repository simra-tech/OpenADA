# SPDX-FileCopyrightText: 2025 LibreLane Template Contributors
# SPDX-FileCopyrightText: 2026 OpenADA Vibe CPU Contributors
# SPDX-License-Identifier: Apache-2.0

"""Self-checking RTL test for the external-memory accumulator CPU."""

import os
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer

try:
    from cocotb_tools.runner import get_runner
except ImportError:  # cocotb 1.9 keeps the preview runner in this module.
    from cocotb.runner import get_runner


SIM = os.getenv("SIM", "verilator")
HDL_TOPLEVEL = "chip_core"


# Two-byte program. It deliberately places trap instructions in the paths that
# should be skipped by JMP and the taken JZ.
PROGRAM = [
    0x10, 0x05,  # 00: LDI  0x05
    0x20, 0x03,  # 02: ADDI 0x03       ACC = 0x08
    0x41, 0x0A,  # 04: JZ   0x0a       not taken
    0x30, 0x30,  # 06: STA  [0x30]     MEM[0x30] = 0x08
    0x40, 0x0C,  # 08: JMP  0x0c
    0x10, 0xEE,  # 0a: LDI  0xee       must be skipped
    0x20, 0xF8,  # 0c: ADDI 0xf8       ACC wraps to zero
    0x41, 0x14,  # 0e: JZ   0x14       taken
    0x10, 0xDD,  # 10: LDI  0xdd       must be skipped
    0xFF, 0x00,  # 12: HLT             must be skipped
    0x10, 0x2A,  # 14: LDI  0x2a
    0x21, 0x30,  # 16: ADD  [0x30]     ACC = 0x32
    0x30, 0x31,  # 18: STA  [0x31]     MEM[0x31] = 0x32
    0x11, 0x31,  # 1a: LDA  [0x31]     ACC = 0x32
    0x20, 0x01,  # 1c: ADDI 0x01       ACC = 0x33
    0x30, 0x32,  # 1e: STA  [0x32]     MEM[0x32] = 0x33
    0x12, 0x00,  # 20: IN   0          ACC = input[8:1] = 0xa5
    0x30, 0x33,  # 22: STA  [0x33]     MEM[0x33] = 0xa5
    0x12, 0x01,  # 24: IN   1          ACC = input[9] = 1
    0x30, 0x34,  # 26: STA  [0x34]     MEM[0x34] = 1
    0xFF, 0x00,  # 28: HLT
]


def snapshot_bus(dut):
    """Return the externally observable bus state."""
    output = int(dut.output_out.value)
    return (
        output & 0x3F,
        (output >> 6) & 1,
        (output >> 7) & 1,
        int(dut.bidir_out.value) & 0xFF,
        int(dut.bidir_oe.value) & 0xFF,
    )


async def serve_memory(dut, memory, limit=128):
    """Serve bus requests with deterministic zero-to-two-cycle wait states."""
    trace = []

    for transaction in range(limit):
        await FallingEdge(dut.clk)
        address, write, halted, write_data, output_enable = snapshot_bus(dut)

        if halted:
            return trace

        expected_enable = 0xFF if write else 0x00
        assert output_enable == expected_enable, (
            f"OE={output_enable:#04x} for write={write} at address {address:#04x}"
        )

        dut.bidir_in.value = 0 if write else memory[address]
        stable_request = snapshot_bus(dut)

        # ready is input_in[0]. The auxiliary input banks remain stable.
        auxiliary_inputs = (1 << 9) | (0xA5 << 1)
        dut.input_in.value = auxiliary_inputs
        for _ in range(transaction % 3):
            await RisingEdge(dut.clk)
            await FallingEdge(dut.clk)
            assert snapshot_bus(dut) == stable_request, (
                "memory request changed while ready was low"
            )

        dut.bidir_in.value = 0 if write else memory[address]
        dut.input_in.value = auxiliary_inputs | 1
        await RisingEdge(dut.clk)

        if write:
            memory[address] = write_data
            trace.append(("W", address, write_data))
        else:
            trace.append(("R", address, memory[address]))

        # Move away from the sampling edge before removing ready.
        await Timer(1, "ns")
        dut.input_in.value = auxiliary_inputs
        dut.bidir_in.value = 0

    raise AssertionError(f"CPU did not halt within {limit} memory transactions")


@cocotb.test()
async def test_cpu_program(dut):
    """Prove arithmetic, load/store, conditional branch, jump, and halt."""
    memory = [0x00] * 64
    memory[: len(PROGRAM)] = PROGRAM

    dut.rst_n.value = 0
    dut.input_in.value = 0
    dut.bidir_in.value = 0

    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    await ClockCycles(dut.clk, 4)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    trace = await serve_memory(dut, memory)

    expected_addresses = [
        ("R", 0x00), ("R", 0x01),
        ("R", 0x02), ("R", 0x03),
        ("R", 0x04), ("R", 0x05),
        ("R", 0x06), ("R", 0x07), ("W", 0x30),
        ("R", 0x08), ("R", 0x09),
        ("R", 0x0C), ("R", 0x0D),
        ("R", 0x0E), ("R", 0x0F),
        ("R", 0x14), ("R", 0x15),
        ("R", 0x16), ("R", 0x17), ("R", 0x30),
        ("R", 0x18), ("R", 0x19), ("W", 0x31),
        ("R", 0x1A), ("R", 0x1B), ("R", 0x31),
        ("R", 0x1C), ("R", 0x1D),
        ("R", 0x1E), ("R", 0x1F), ("W", 0x32),
        ("R", 0x20), ("R", 0x21),
        ("R", 0x22), ("R", 0x23), ("W", 0x33),
        ("R", 0x24), ("R", 0x25),
        ("R", 0x26), ("R", 0x27), ("W", 0x34),
        ("R", 0x28), ("R", 0x29),
    ]
    assert [(kind, address) for kind, address, _ in trace] == expected_addresses

    assert memory[0x30] == 0x08, "ADDI result was not stored"
    assert memory[0x31] == 0x32, "memory ADD result was not stored"
    assert memory[0x32] == 0x33, "LDA/ADDI result was not stored"
    assert memory[0x33] == 0xA5, "eight-bit auxiliary input bank was not read"
    assert memory[0x34] == 0x01, "ninth auxiliary input was not read"

    address, write, halted, accumulator, output_enable = snapshot_bus(dut)
    assert halted == 1
    assert write == 0
    assert address == 0x2A, "halt should expose the next sequential PC"
    assert accumulator == 0x01
    assert output_enable == 0x00, "CPU must release the data bus after halt"


def chip_core_runner():
    project = Path(__file__).resolve().parent
    build_dir = project / "sim_build"
    runner = get_runner(SIM)

    build_args = []
    if SIM == "verilator":
        build_args = ["--timing", "--trace-fst", "--trace-structs"]

    runner.build(
        sources=[project / "../src/chip_core.sv"],
        hdl_toplevel=HDL_TOPLEVEL,
        build_dir=build_dir,
        build_args=build_args,
        always=True,
        waves=True,
    )
    runner.test(
        hdl_toplevel=HDL_TOPLEVEL,
        test_module="chip_top_tb",
        build_dir=build_dir,
        waves=True,
    )


if __name__ == "__main__":
    chip_core_runner()
