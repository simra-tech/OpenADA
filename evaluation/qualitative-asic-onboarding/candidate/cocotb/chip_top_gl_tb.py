# SPDX-FileCopyrightText: 2026 OpenADA Vibe CPU Contributors
# SPDX-License-Identifier: Apache-2.0

"""Pad-level functional test for a routed IHP SG13G2 gate netlist.

This is deliberately a zero-delay check. It proves that the routed logical
netlist, IHP standard-cell models, and IHP I/O-pad models preserve the CPU's
externally visible behavior; it does not annotate an SDF or verify timing.
"""

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
HDL_TOPLEVEL = "chip_top_gl_wrapper"

PROGRAM = [
    0x10, 0x05,  # 00: LDI  0x05
    0x20, 0x03,  # 02: ADDI 0x03       ACC = 0x08
    0x41, 0x0A,  # 04: JZ   0x0a       not taken
    0x30, 0x30,  # 06: STA  [0x30]     MEM[0x30] = 0x08
    0x40, 0x0C,  # 08: JMP  0x0c
    0x10, 0xEE,  # 0a: trap, skipped by JMP
    0x20, 0xF8,  # 0c: ADDI 0xf8       ACC wraps to zero
    0x41, 0x14,  # 0e: JZ   0x14       taken
    0x10, 0xDD,  # 10: trap, skipped by JZ
    0xFF, 0x00,  # 12: trap, skipped by JZ
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

EXPECTED_ADDRESSES = [
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


def bus_status(dut):
    """Return address, write, and halted from the output pads."""
    output = int(dut.output_out.value)
    return output & 0x3F, (output >> 6) & 1, (output >> 7) & 1


async def serve_memory(dut, memory, limit=128):
    """Serve the pad-level memory bus with zero-to-two-cycle wait states."""
    trace = []
    auxiliary_inputs = (1 << 9) | (0xA5 << 1)

    for transaction in range(limit):
        await FallingEdge(dut.clk)
        address, write, halted = bus_status(dut)
        if halted:
            return trace

        dut.bidir_in.value = 0 if write else memory[address]
        dut.bidir_drive.value = 0 if write else 1
        await Timer(1, "ns")

        stable_status = bus_status(dut)
        stable_pad_data = int(dut.bidir_out.value) & 0xFF
        if write:
            write_data = stable_pad_data
        else:
            assert stable_pad_data == memory[address], (
                f"read pad data {stable_pad_data:#04x} != memory "
                f"{memory[address]:#04x} at address {address:#04x}"
            )

        dut.input_in.value = auxiliary_inputs
        for _ in range(transaction % 3):
            await RisingEdge(dut.clk)
            await FallingEdge(dut.clk)
            assert bus_status(dut) == stable_status, (
                "memory request changed while ready was low"
            )
            assert (int(dut.bidir_out.value) & 0xFF) == stable_pad_data, (
                "data pads changed while ready was low"
            )

        dut.input_in.value = auxiliary_inputs | 1
        await RisingEdge(dut.clk)

        if write:
            memory[address] = write_data
            trace.append(("W", address, write_data))
        else:
            trace.append(("R", address, memory[address]))

        await Timer(1, "ns")
        dut.input_in.value = auxiliary_inputs
        dut.bidir_drive.value = 0
        dut.bidir_in.value = 0

    raise AssertionError(f"CPU did not halt within {limit} memory transactions")


@cocotb.test()
async def test_routed_cpu_program(dut):
    """Run the complete CPU program through the physical pad models."""
    memory = [0x00] * 64
    memory[: len(PROGRAM)] = PROGRAM

    dut.rst_n.value = 0
    dut.input_in.value = 0
    dut.bidir_in.value = 0
    dut.bidir_drive.value = 1

    cocotb.start_soon(Clock(dut.clk, 10, "ns").start())
    await ClockCycles(dut.clk, 4)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    trace = await serve_memory(dut, memory)
    assert [(kind, address) for kind, address, _ in trace] == EXPECTED_ADDRESSES

    assert memory[0x30] == 0x08
    assert memory[0x31] == 0x32
    assert memory[0x32] == 0x33
    assert memory[0x33] == 0xA5
    assert memory[0x34] == 0x01

    address, write, halted = bus_status(dut)
    assert (address, write, halted) == (0x2A, 0, 1)

    # Prove that the CPU releases every bidirectional data pad after halt.
    dut.bidir_in.value = 0xA6
    dut.bidir_drive.value = 1
    await Timer(1, "ns")
    assert int(dut.bidir_out.value) == 0xA6


def chip_top_gate_runner():
    project = Path(__file__).resolve().parent
    pdk_root = Path(os.environ["PDK_ROOT"]).resolve()
    pdk = os.getenv("PDK", "ihp-sg13g2")
    gate_netlist = Path(os.environ["GL_NETLIST"]).resolve()
    pdk_dir = pdk_root / pdk
    # Keep evidence from different simulators separate. Mixing Icarus and
    # Verilator products in one directory makes a fresh result ambiguous.
    build_dir = project / "sim_build_gl" / SIM

    sources = [
        pdk_dir / "libs.ref/sg13g2_stdcell/verilog/sg13g2_udp.v",
        pdk_dir / "libs.ref/sg13g2_stdcell/verilog/sg13g2_stdcell.v",
        pdk_dir / "libs.ref/sg13g2_io/verilog/sg13g2_io.v",
        project / "../ip/bondpad_70x70_novias/vh/bondpad_70x70_novias.v",
        gate_netlist,
        project / "chip_top_gl_wrapper.sv",
    ]
    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        raise FileNotFoundError("missing gate-simulation input(s): " + ", ".join(missing))

    build_args = []
    if SIM == "icarus":
        # Cell-library specify paths are not used by this zero-delay target.
        build_args = ["-gno-specify"]
    elif SIM == "verilator":
        build_args = [
            "--timing",
            "--trace-fst",
            "--trace-structs",
            "--Wno-fatal",
            "--Wno-PINMISSING",
        ]

    runner = get_runner(SIM)
    runner.build(
        sources=sources,
        hdl_toplevel=HDL_TOPLEVEL,
        build_dir=build_dir,
        build_args=build_args,
        always=True,
        waves=True,
    )
    runner.test(
        hdl_toplevel=HDL_TOPLEVEL,
        test_module="chip_top_gl_tb",
        build_dir=build_dir,
        waves=True,
    )


if __name__ == "__main__":
    chip_top_gate_runner()
