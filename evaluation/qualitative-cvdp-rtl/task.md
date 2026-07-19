# CVDP RTL pilot task

## Source

- Benchmark: NVIDIA CVDP v1.1.0
- Repository commit: `8e894cf74414ab1eaea1e2b4e80a02f123df07b6`
- Public example dataset:
  `cvdp_v1.1.0_example_agentic_code_generation_no_commercial.jsonl`
- Datapoint: `cvdp_agentic_fixed_arbiter_0001`
- Categories: `cid003`, `easy`

The public no-solution record supplied the prompt, specification, and
verification context. Its `patch` object named an empty expected output path;
that object was not exposed to the participant.

## Sanitized task text

> Design a `fixed_priority_arbiter` module in SystemVerilog within a file
> `fixed_priority_arbiter.sv` at the location
> `rtl/fixed_priority_arbiter.v`. Refer to `docs/specification.md` and implement
> the described fixed-priority arbitration and external priority override.

The contradiction between the requested `.sv` filename and `.v` location is
retained because it is present in the public benchmark input and materially
affected the outcome.

## Treatment instruction

The fresh Codex turn also received this treatment instruction:

> Use `$openada:review-rtl-architecture` and `$openada:openada` as appropriate.
> Inspect the specification and verification context, implement the requested
> RTL, and validate it with the strongest supported local evidence. Do not
> access the internet or any reference solution. Keep generated evidence under
> `rundir/openada`.

No CVDP harness source, reference output, golden patch, credentials, private
repository content, or prior session was placed in the participant workspace.
