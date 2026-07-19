# First CVDP RTL session report

## Outcome

The OpenADA-treated Codex session produced functionally plausible RTL, and its
fresh OpenADA lint and structural evidence both passed. A separate native
Verilator run against the participant-visible testbench also passed all seven
cases. Those results do not establish CVDP completion.

The participant created `rtl/fixed_priority_arbiter.v`, following the location
spelled out in the task. The same task also called the requested file
`fixed_priority_arbiter.sv`, and the CVDP harness configuration resolves
`/code/rtl/fixed_priority_arbiter.sv`. The first-attempt benchmark outcome must
therefore remain a harness failure. A replay in the pinned CVDP image confirmed
that cocotb invoked Icarus on the absent `.sv` path and pytest exited 1 before
simulation. No post-outcome rename or copy was made.

This is one qualitative treatment observation, not a CVDP score, paired
OpenADA-performance claim, or training dataset.

## Run facts

| Item | Observed value |
|---|---|
| Codex CLI | `0.144.5` |
| Model request | `gpt-5.4`, high reasoning effort |
| OpenADA | `0.4.0`, installed plugin-local launcher |
| Benchmark revision | `8e894cf74414ab1eaea1e2b4e80a02f123df07b6` |
| CVDP image | `nvidia/cvdp-sim:v1.0.0`, local image ID `sha256:4840e540467d9f23cb811b0ab91d7634f1dfda5b557b9c5508b3e1147595588f` |
| CVDP tools | Icarus 13.0, Yosys 0.40, Verilator 5.038, cocotb 2.0.1, pytest 8.3.2 |
| Datapoint | `cvdp_agentic_fixed_arbiter_0001` |
| Participant workspace | Fresh Git repository; specification, public verification file, prompt, empty `rtl/` and `rundir/` |
| Network request | Disabled through the Codex workspace-write sandbox configuration |
| Session duration | Approximately 356 seconds from external file observations; no trusted monotonic launcher record |
| Raw Codex trace | 123 lines, 139,965 bytes, SHA-256 `d976eee6ea8be32af94cdd45412b6b46f7223fd0f80e13edd8bcedb80ea5267e` |
| CLI token snapshot | 1,175,652 input; 1,113,216 cached input; 13,636 output; 5,403 reasoning output |
| Participant RTL | 46 lines, SHA-256 `7b160e1d8d16f0f4f71ce8e1c15f22eb11329457938e6eb67800442d95a5e2c1` |

The raw event stream and participant workspace remain restricted external
evidence. They are not vendored into OpenADA.

## Engineering evidence

| Check | Status | Evidence |
|---|---|---|
| OpenADA scoped lint preflight | `pass` | Verilator was usable; the preflight did not inspect the RTL |
| OpenADA strict RTL lint | `pass` | Verilator 5.006, zero normalized warnings/errors; result-envelope SHA-256 `7036043d42c48da7d000bf28349bc1555d47bc3507d3be2558793ba35fb2b3af` |
| OpenADA structural check | `pass` | Yosys 0.23 elaborated the declared top and completed structural checks; result-envelope SHA-256 `56dd39a268edf909ca81a855c5af1d31837a3f53a932bc9186750370c8d79fc3` |
| Participant-visible native simulation | `pass` | Verilator testbench cases 1 through 7 passed; native log SHA-256 `5cd4f2abbe06e1561c5febe90976f4065ef77b0a281f333ce5ec3d66afea375c` |
| CVDP pinned harness replay | `fail` | Pytest exited 1; cocotb invoked Icarus on `/code/rtl/fixed_priority_arbiter.sv`, while the participant produced only `.v`; harness log SHA-256 `d4aa320969a402c6640770872e8b970dea8a92623f7df507f78febd46e40231a` |
| Official CVDP score | Not claimed | The original test code and pinned image were replayed, but the complete benchmark runner/reporting path was not used |

The OpenADA rows establish diagnostic cleanliness and structural elaboration
only for the exact `.v` source selected by the participant. The native
Verilator run is not an OpenADA-normalized result. Neither can be transferred
to the missing `.sv` harness input by inference.

## Trace assessment

The packaged paired-agent adapter and v0alpha1 campaign contract intentionally
pin Codex CLI `0.144.3`. Supplying the observed `0.144.5` identity failed closed
with `unsupported_codex_cli_version` and emitted a schema-valid rejected trace,
SHA-256 `2ba35b9b5503e51864a983c06a0462ae9a9cd0d6ab58e2bfc7b7445657fd9e22`.

For compatibility investigation only, an external copy of the parser was
changed mechanically to name `0.144.5`. It consumed the stream without an
unknown event variant or lifecycle conflict and observed:

- 69 unique actions and 120 lifecycle action records;
- 44 command executions, 20 agent messages, four file changes, and one todo
  list;
- one complete turn with a zero process exit; and
- the cumulative token snapshot listed above.

That output is unqualified, violates the published v0alpha1 source identity,
and is not an admissible evaluation row. A future adapter revision needs its
own reviewed Codex fixture corpus, bounds, schemas, negative cases, and campaign
identity rather than editing v0alpha1 in place.

The session also started an unrelated configured MCP server, which emitted an
authorization error on stderr. It did not affect the completed turn, but it
shows that this development-host session did not isolate the OpenADA treatment
from ambient Codex configuration. A claim-eligible runner needs a fresh
`CODEX_HOME` containing only the frozen treatment plus a credential-owning
supervisor outside the untrusted EDA workspace.

## OpenADA findings

What worked:

- The agent froze review manifests and retained exact OpenADA result envelopes,
  tool identities, inputs, artifacts, and hashes.
- It separated preflight readiness from the RTL assertions.
- It repaired and reran evidence into fresh `-v2` directories instead of
  overwriting the first results.
- Its final response explicitly withheld functional proof and signoff claims
  from lint and structural passes.

Gaps exposed:

1. **No normalized HDL simulation operation.** Functional evidence fell back
   to native Verilator. CVDP makes an `rtl.simulate`/`rtl.test` semantic surface
   the highest-value OpenADA addition.
2. **Artifact selection is outside the evidence contract.** OpenADA proved the
   file it was given, but could not establish that it was the benchmark's
   required deliverable. A future mutation/submission layer should bind the
   declared write set and evaluator-visible output paths.
3. **Prompt/configuration conflicts need an explicit decision.** The RTL-review
   skill already says not to guess source configuration, but an autonomous
   benchmark turn cannot ask for clarification. An evaluation policy must
   predeclare how conflicting output paths are resolved.
4. **Current Codex traces are not covered.** The content-free reducer needs a
   new, versioned qualification target for CLI `0.144.5`; v0alpha1 must remain
   immutable.
5. **A treatment-only session is not causal evidence.** The raw condition must
   run in an environment from which the complete OpenADA distribution, skills,
   prior outputs, and ambient configuration are absent.

## Recommended next experiment

Freeze a new qualitative or paired CVDP task bundle that exposes one
unambiguous required output path to both conditions, uses the exact CVDP
simulation image by digest, and scores with the original cocotb harness. Keep
the first ambiguous datapoint as a negative artifact-selection case rather
than repairing or dropping it. Qualify a new Codex 0.144.5 adapter separately,
then run at least five preassigned raw/OpenADA pairs before making a comparative
claim.

`signoff: not claimed`
