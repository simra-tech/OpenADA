# First session report

## Outcome

The session created a genuine educational 16-bit Harvard CPU and produced
routed IHP SG13G2 GDS, DEF, SPEF, and Verilog. It did not produce a tapeout
candidate. DRC and LVS failed, the physical run retained antenna and fanout
violations, timing covered only one corner, and no full-chip padframe or
submission package was assembled.

The agent's final conclusion was appropriately conservative: a core-only
physical candidate, not tapeout-ready or foundry signoff.

## Run facts

| Item | Observed value |
|---|---|
| Codex CLI | `0.144.5` |
| Repository paired adapter | pinned to `0.144.3`; incompatible without requalification |
| OpenADA | `0.4.0`, resolved through the plugin-local launcher because it was absent from `PATH` |
| Session duration | 1,393,871 ms (23m 13.871s) |
| Time to first token | 11,515 ms |
| Raw trace | 544 lines, 850,564 bytes; restricted external evidence only |
| Actions | 139: 50 shell, 10 patch, 2 web, 27 process-poll, and 50 wait calls |
| CLI token accounting | 13,838,702 total; 13,557,760 cached input tokens |
| Workspace footprint | 5.4 GiB: 3.7 GiB tools, 1.6 GiB flow source, 97 MiB build, 74 MiB evidence |
| Retained downloaded archives | 6 files, 787,496,505 bytes |
| Plugin cache footprint | 902 MiB, including 715 MiB `.strategy`, 49 MiB `.venv`, and 29 MiB `.git` |

## Timeline and recovery cost

- **20:55:16–20:56:06 — startup.** The agent read the installed skill,
  resolved the prescribed plugin-local launcher, and ran the correct scoped RTL
  preflight. It correctly treated the pass as Yosys readiness rather than a
  design result. It found only 483 MiB free on the root filesystem and moved
  disposable work to `/dev/shm`, which had about 15 GiB free.
- **20:55:50 — discovery defect.** Docker was found at `/usr/bin/docker`, but
  local images were not inventoried. The same probe recursively searched
  `/usr`, `/opt`, and `/eda` for IHP names, contrary to the skill's no-crawl
  rule.
- **20:56:21–21:02:07 — version lottery.** The agent cloned latest
  OpenROAD-flow-scripts commit `f255c15...`, downloaded an older Precision
  OpenROAD Debian package, repaired its loader with several Debian 11 packages,
  and downloaded the latest 2026-07-16 OSS CAD Suite. It then rewound ORFS to
  commit [`8ae3ae3...`](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/commit/8ae3ae362e2e9be94ab1d27b3dd647c8328a783f)
  to match OpenROAD `v2.0-17598-ga008522d8`.
- **20:57:54–20:58:43 — CPU and RTL gates.** The agent authored Vibe16, a
  compact CPU with eight registers, ALU, load/store, branch, jump, and halt. A
  self-checking add/store/halt program passed. OpenADA structural checking
  passed. Strict lint found a real 17-to-16-bit jump assignment; the agent fixed
  it and the next fresh lint run passed.
- **20:59:38–21:02:07 — six failed flow starts.** Failures were, in order:
  wrong source paths, host Yosys 0.23 incompatibility, unsupported SDC collection
  syntax, mutually exclusive floorplan controls, current ORFS versus old
  OpenROAD command incompatibility, and missing GNU `time`. The seventh start
  used the back-pinned stack and succeeded.
- **21:02:07–21:09:22 — native implementation.** The ORFS stage logs total
  411 seconds, including 353 seconds for detailed routing. Floorplan, PDN,
  placement, CTS, routing, extraction, reporting, and GDS merge produced final
  artifacts. Detailed routing reached zero route markers, but antenna checking
  still reported two violating nets and two violating pins.
- **21:09:37 — normalized synthesis.** OpenADA/Yosys 0.67+40 produced and
  independently validated a Liberty-mapped netlist with 1,450 cells and
  19,268.739 µm² reported cell area.
- **21:09:56–21:10:47 — four timing attempts.** The first used the unknown
  tool alias `opensta`; two requests then failed the narrow declarative SDC
  grammar. A fourth request parsed and launched OpenSTA 2.6, but the OpenADA
  driver emitted unsupported `report_checks -group_path_count`. The normalized
  engineering status correctly remained `unknown`.
- **21:11:10–21:12:27 — DRC.** OpenADA/KLayout 0.28.5 ran for 75.740 seconds.
  Native execution completed with exit zero, while normalized engineering status
  was correctly `fail`: 105 markers, with no waivers.
- **21:13:38–21:17:23 — LVS.** The first ORFS wrapper invocation used bindings
  incompatible with the selected deck. A corrected, explicitly native KLayout
  run completed its comparison in 182.929 seconds and reported that the
  netlists did not match.
- **21:18:23 — conclusion.** The agent retained principal hashes, omitted a
  padframe, and withheld tapeout and signoff claims.

The visible provisioning footprint was avoidable. The OSS CAD archive was
732,611,637 bytes and expanded to 2.5 GiB; the OpenROAD package was 52,642,368
bytes; the ORFS checkout occupied 1.6 GiB and its fetched Git data later occupied
513 MiB. A digest-pinned IIC-OSIC-TOOLS 2026.06 image was already present on the
host at `sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0`.
It should have been inspected and qualified before unrelated downloads.

## Engineering gate review

| Gate | Verdict | Evidence and limitation |
|---|---|---|
| Project freeze | **Fail** | No upfront project manifest, independent PDK revision, runtime digest, complete source manifest, or coherent tool lock |
| RTL structure | **Pass** | OpenADA/Yosys 0.23 elaborated the exact top and completed structural checks |
| Strict lint | **Pass after repair** | First normalized run found the jump-width warning; fresh second run passed |
| Function | **Partial** | One add/store/halt smoke test; load, branch, jump, shifts, logic, loading, firmware, reset boundaries, and architectural coverage were absent |
| Synthesis | **Pass** | OpenADA/Yosys 0.67+40, 1,450 mapped SG13G2 cells, no remaining processes or memories |
| Physical implementation | **Fail as a gate** | GDS/DEF/SPEF completed and detailed routing reached zero markers, but antenna retained two violating nets and two pins |
| Timing | **Partial / unknown** | Native typical-corner setup/hold counts were zero with 12.92 ns/0.34 ns worst slacks, but max fanout had two violations, no MCMM was run, and OpenADA timing was `unknown` |
| Full-chip assembly | **Not evaluated** | No IO/ESD cells, power pads, bondpads, pad ring, seal ring, package plan, or full-chip density structure |
| DRC | **Fail** | 105 markers: 84 pin-enclosure, 16 density-window, and 5 global density/boundary markers |
| LVS | **Fail** | Native KLayout completed and reported `Netlists don't match` |
| Handoff | **Fail** | Preview PDK, minimal public DRC deck, no complete checklist, full-chip reference netlist, pinout, package plan, or waiver ledger |

The session's final “PASS WITH LIMITS” timing row was too generous because it
omitted the two max-fanout violations. “Zero routing violations” was narrowly
true for detailed routing but omitted the two antenna violations.

## OpenADA boundary assessment

What worked well:

- The CLI was resolved exactly through the plugin fallback and the first
  preflight selected only the next RTL assertion.
- OpenADA result envelopes retained exact tool identity, input and artifact
  records, diagnostics, and separate execution and engineering status.
- The agent did not turn a DRC process exit of zero into an engineering pass.
- Lint failure, timing `unknown`, DRC failure, and LVS mismatch were preserved
  rather than upgraded.
- Behavioral simulation, P&R, routed timing, and KLayout LVS were explicitly
  described as native capability gaps, not OpenADA results.
- Preview collateral was not described as foundry signoff, and the missing
  padframe was not concealed.

Contract and evidence weaknesses:

- The broad host crawl violated the explicit inspection policy.
- `openada capabilities` and a compatible local-runtime inventory were skipped.
- Strict lint did not receive its own preflight. Because the combined shell did
  not stop on failure, native simulation continued after the first lint fail.
- An attempted deletion of an evidence directory was rejected; a new output
  path should have been selected immediately.
- Native stages retained logs and principal hashes, but not one closed record
  of exact argv, environment, all inputs, runtime identity, and output hashes.
- ORFS `.git/HEAD` was used as DRC provenance even though it is not an
  independent IHP PDK revision attestation.
- Progress was silent for 8m01s during implementation and 4m59s during DRC/LVS,
  despite 77 polling calls.

OpenADA 0.4.0 still lacks semantic operations for HDL simulation,
floorplan/place/CTS/route, padframe assembly, extraction/PEX, routed MCMM timing,
power integrity, KLayout LVS, and final submission assembly. The default result
for those rows is not evaluated unless an explicitly authorized native gap run
is preserved separately.

## UX findings and improvement mapping

| Observed problem | New bootstrap-skill response | Remaining product work |
|---|---|---|
| “CPU tapeout candidate” silently narrowed to a core experiment | Define `core`, `full-chip candidate`, or `submission candidate` before provisioning | Surface the selected finish line prominently in user updates |
| Docker was detected but an existing pinned runtime was ignored | Safe-order inspection checks compatible local OCI/Nix runtimes before downloads | Add deterministic runtime inventory and launch support; the `iic-osic-tools` profile currently recognizes `/foss` but does not launch Docker |
| Latest ORFS, old OpenROAD, and latest Yosys were mixed | Select one coherent stack and freeze it before the first expensive run | Publish machine-readable compatible runtime recipes |
| No independent PDK/tool/collateral identity | Bootstrap manifest binds revisions, image digest/platform, tool binaries, Liberty/LEF/GDS/CDL, decks, constraints, and source manifest | Extend automated validation as new collateral roles become necessary |
| Blank core flow lost padframe, power, seal-ring, density, and final-LVS structure | Start IHP work from the pinned official full-chip template, not a core-only example | Maintain and requalify the official-template integration path |
| Educational CPU had only one smoke program and no loading path | Require programmability, memory/loading, reset, firmware identity, architectural tests, and license review | Provide a small reviewed CPU/template fixture for onboarding evaluations |
| Antenna and fanout defects were omitted from the headline | Gate table stops on congestion, route, antenna, clock, power, timing, DRC, or LVS defects | Add report summarizers that cannot omit nonzero native checks |
| Repairs changed flow, SDC, and tools without an upfront invalidation model | Classify one failure class, update the frozen manifest intentionally, and write fresh downstream evidence | Add resumable native-run metadata linked to manifest revisions |
| Native work was labeled but incompletely captured | Native-gap policy requires executable, version, argv, environment, cwd, timeout, exit, and hashes | Add a bounded native-gap evidence envelope distinct from `openada.result` |
| OpenSTA alias and SDC grammar required trial and source inspection | Capabilities-first routing and the timing skill limit OpenADA to supported synthesis-stage timing | Improve alias diagnostics, document the exact SDC subset, and feature-probe OpenSTA flags |
| Plugin installation cached development material | None; this is packaging rather than project coordination | Exclude `.strategy`, `.venv`, `.git`, caches, build trees, and unrelated evaluation data from installed plugin payloads |
| Long operations produced no user-visible progress | Stage-by-stage gates provide natural update points | Add periodic progress summaries while native jobs are polled |

The new coordinator is
[`skills/bootstrap-asic-project/SKILL.md`](../../skills/bootstrap-asic-project/SKILL.md).
Its IHP route freezes the official full-chip template at
`0418301723d86133de686ef743cfd668bb3d11d4`, LibreLane at
`69b2067bd2b5eb89b84649b76e9edaa9e51e6735`, and the IHP PDK at
`3b5a704ba6738aa686b08706187830e6284d2a10`. Those pins must still be
rechecked against the current IHP shuttle and submission requirements before a
real handoff.

## Evaluation-status limitation

This session cannot be admitted to the current paired-agent dataset. Its raw
metadata identifies Codex CLI `0.144.5`, while
[`adapters/codex_jsonl.py`](../paired-agent/adapters/codex_jsonl.py) and the
v0alpha1 trace schema require exactly `0.144.3` and fail closed on other
versions. There was also no paired control or campaign manifest. This report is
therefore a qualitative onboarding observation only.
