# Candidate pad allocation

| Pad | Direction | Function |
| --- | --- | --- |
| `clk_PAD` | input | 50 MHz target clock |
| `rst_n_PAD` | input | synchronous active-low reset |
| `output_PAD[5:0]` | output | external byte address |
| `output_PAD[6]` | output | external-memory write request |
| `output_PAD[7]` | output | CPU halted state |
| `bidir_PAD[7:0]` | bidirectional | external-memory read/write data |
| `input_PAD[0]` | input | external-memory ready handshake |
| `input_PAD[8:1]` | input | auxiliary input bank zero |
| `input_PAD[9]` | input | auxiliary input bank one, bit zero |
| `analog_PAD[7:0]` | analog | reserved and undriven by Vibe16 |
| `VDD_PAD[0]`, `VSS_PAD[0]` | power | core supply and return |
| `IOVDD_PAD[0]`, `IOVSS_PAD[0]` | power | IO supply and return |

The physical side placement is authoritative in `librelane/config.yaml`.
Package pin numbers and a bonding diagram have not been assigned.
