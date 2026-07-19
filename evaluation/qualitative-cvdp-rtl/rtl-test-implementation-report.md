# CVDP-derived `rtl-test` implementation report

Date: 2026-07-18

The first CVDP pilot showed that lint and structural checks were useful but
could not execute a self-checking RTL test. This follow-up adds the alpha
`openada.operation/rtl.test/v1alpha1` contract and CLI surface with two fixed
native mappings: Icarus Verilog plus `vvp`, and Verilator `--binary`.

The assertion is deliberately narrow. Pass means that the exact ordered HDL
inputs compiled and elaborated for the declared self-checking top and that the
generated test executable exited zero. It does not claim broad functional
correctness, test adequacy, coverage closure, synthesis correctness, or timing.
Compile/elaboration errors and nonzero test exits are fail. Missing tools,
timeouts, truncated or invalid output, stale artifacts, input changes,
dependency-closure changes, and tool-identity changes are unknown.

## Pinned CVDP-derived replay

Runtime image:
`nvidia/cvdp-sim:v1.0.0`, image ID
`sha256:4840e540467d9f23cb811b0ab91d7634f1dfda5b557b9c5508b3e1147595588f`.

Inputs were the unchanged Codex-produced
`rtl/fixed_priority_arbiter.v` and participant-visible
`verif/fixed_priority_arbiter_tb.sv`. The benchmark artifact-path failure was
not repaired or reclassified.

- Icarus 13.0 plus `vvp`: pass; compile 16 ms, run 8 ms. Compile-log SHA-256
  `fda5b8653649e474b25da3d6a0ed6de5dffb90b1282786b60e649646d71fce38`;
  run-log SHA-256
  `827b4c668af22a62fddb053291a239aa8729a67cdd5d1fcea52cbc99cca10c1f`;
  generated executable SHA-256
  `90ed64a505f23934c4deadfd6ea84d2625a2570e4ba52bfd3249b69ac84028ed`.
- Verilator 5.038: pass; compile 8,689 ms, run 4 ms. Compile-log SHA-256
  `1f0cba8b02dc3b15a5cfd54563b6d1f9360b2b2ce0a87e9233ea33668ddd6b58`;
  run-log SHA-256
  `443dd02e2feaf39782ace9f398605b1ab76c680447fe6e22f12bab0b1dfa1726`;
  generated executable SHA-256
  `e21f0350fd48938343d1658a00a5fe09874752f5f02c45295d39b0ba42e83a36`.
- Controlled negative replay with declared top `missing_tb`: engineering fail;
  Icarus compile exit 1; compile-log SHA-256
  `367267c48f5936e9c42d99cde5c6721673231a20b8d6e6c97ae5b163b1fdcf0c`.

This closes the concrete observability gap exposed by the pilot: an agent can
now distinguish “lint/structure looks plausible” from “this declared HDL test
actually ran.” The profile and catalog intentionally remain structured alpha;
a release-maturity claim requires a source-frozen semantic-chain replay and
the existing seven release receipts must be refreshed after this semantic
subject change.
