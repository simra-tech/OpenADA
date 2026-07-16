# ORFS Ibex synthesis and timing semantic conformance

This chain closes OpenADA's `synthesize` and `timing-analyze` semantic commands
on the public Ibex core vendored by OpenROAD-flow-scripts at commit
`bea7dcd7be7f26d1328f6058b01cf42bf4352aa2`. The vendored Ibex revision is
`77d801001554cce8fe69e742e96539eecbe74425`.

The chain covers exactly seven synthesis rows and seven timing rows: each CLI
surface, preflight assertion, operation profile, assertion profile, feature,
native mapping, and built-in provider mapping declared in `semantic.py`. It
claims no place-and-route, extraction, power, physical-verification, or signoff
surface.

## Pinned engineering context

- 21 ordered Ibex RTL sources and two resolved literal include dependencies
- the ORFS Nangate45 typical Liberty, latch techmap, Ibex SDC, and flow config
- a 39-byte ABC constraint derived from the pinned Nangate driver/load settings
- Yosys 0.66 with the Slang frontend, a version- and SHA-256-bound external
  ABC 1.01 executable selected through `abc -exe`, and OpenSTA 3.1.0
- linux/amd64 IIC-OSIC-TOOLS by manifest and config digest
- network disabled, read-only OpenADA/design mounts, dropped capabilities, and
  a fresh isolated evidence directory during every EDA operation
- closed non-inheriting tool environments shared by each version probe and
  native execution

The setup SDC uses a 2.2 ns clock period. Timing is deliberately and explicitly
limited to one Liberty/SDC corner with ideal interconnect and no SPEF. It is not
routed or MCMM signoff timing.

## Reproduce

Setup is the only network-enabled stage:

```bash
python3 conformance/orfs-ibex-synthesis-timing/setup.py
```

After source freeze, run from a clean checkout and request a release receipt:

```bash
python3 conformance/orfs-ibex-synthesis-timing/run.py \
  --evidence-dir /tmp/openada-orfs-ibex-evidence \
  --receipt-class release
```

The runner performs three real native operations with network disabled:

1. `synthesize` must produce a complete flattened Liberty-mapped `ibex_core`
   netlist, with parsed inference/mapped statistics and no unmapped cell type.
2. `timing-analyze` must produce complete setup/hold path evidence. The pinned
   design currently has a real setup violation, so the command must return
   engineering `fail` while the native OpenSTA process completes successfully.
3. A real `synthesize` request for `missing_ibex_core` must fail natively.

Verify retained evidence independently:

```bash
python3 conformance/orfs-ibex-synthesis-timing/verify.py \
  /tmp/openada-orfs-ibex-evidence
```

The verifier does not import OpenADA. It validates both normalized data schemas,
the exact source/configuration/input closure, scripts, commands, transcript
completeness, raw Yosys statistics, mapped JSON cell histogram and interface,
the mapped-netlist handoff, the hash-identical validated SDC snapshot read by
OpenSTA, OpenSTA metrics/path reports, checkout/source
attestations, and all hashes.

Publish only after the independent verifier succeeds:

```bash
python3 conformance/orfs-ibex-synthesis-timing/semantic.py --publish \
  --evidence-dir /tmp/openada-orfs-ibex-evidence
```

Publication retains two real fail replays (missing synthesis top and negative
setup timing) and two reconciled tamper replays. The tamper probes alter native
mapped statistics or setup-path slack, update the normalized artifact and
run-level hashes, and still must be rejected by the independent oracle as
`unknown` evidence.

Verify a checked-in publication without Docker or the external design checkout:

```bash
python3 conformance/orfs-ibex-synthesis-timing/semantic.py
python3 -m pytest -q tests/test_orfs_ibex_synthesis_timing_conformance.py
```

## Agent decision boundary

The agent-facing result deliberately separates the two conclusions:

- synthesis: `pass`; the mapped netlist is suitable evidence for the next
  implementation iteration;
- timing: `fail`; negative setup WNS/TNS blocks timing-closure and signoff claims.

No IEEE measurement standard is claimed for these STA metrics. IEEE 1800-2023
is recorded only as SystemVerilog language context; the native request selects
1800-2017 and the chain certifies the pinned implementations, not complete
language-standard compliance. Liberty and SDC are bound as exact industry-format
inputs without an IEEE conformance claim.
