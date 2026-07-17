# Forward onboarding session report

## Scope and comparison boundary

This report reviews fresh forward-test thread
`019f6cfb-1745-7b63-bd1a-322d6de60bac` after adding the
`bootstrap-asic-project` coordinator. The raw JSONL remains restricted external
evidence and is not copied into this package:

```text
/dev/shm/openada-vibe-cpu-20260716/codex-home/sessions/2026/07/16/
rollout-2026-07-16T22-10-22-019f6cfb-1745-7b63-bd1a-322d6de60bac.jsonl
```

This is not a benchmark or paired result. Unlike the end-to-end task reviewed
in [`first-session-report.md`](first-session-report.md), the forward prompt
explicitly supplied bounded PDK/template roots, prohibited a long P&R run, and
asked the agent to stop at a frozen identity ledger or exact blocker. The
descriptive reductions below therefore demonstrate the changed workflow, not
comparative model performance.

Both captures identify Codex CLI `0.144.5`; the repository's paired-agent
adapter remains pinned to `0.144.3`. Neither trace belongs in the v0alpha1
paired dataset without adapter/schema requalification.

## Outcome

The forward test stopped fail-closed before expensive EDA. It selected the
official [IHP full-chip template](https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template)
commit `0418301723d86133de686ef743cfd668bb3d11d4`, selected the explicitly
installed IHP PDK revision
`3b5a704ba6738aa686b08706187830e6284d2a10`, and inspected an already-present
digest-pinned OCI image without network access.

The image exposed LibreLane `3.1.0.dev1`; the template lock requires LibreLane
3.0.0 at `69b2067bd2b5eb89b84649b76e9edaa9e51e6735`. Nix was unavailable. The
agent correctly rejected compatibility, retained the ledger as `draft`, did
not run OpenADA preflight or RTL/physical engineering operations, and made no
tapeout, submission, or signoff claim.

It copied selected full-chip shell files and wrote an honest CPU integration
plan, but the copied `chip_core` remains the template counter/SRAM demonstrator,
not a CPU.

## Descriptive comparison

| Measure | First end-to-end session | Forward onboarding session |
|---|---:|---:|
| Elapsed time | 1,393,871 ms (23m 13.871s) | 248,910 ms (4m 8.910s) |
| Time to first token | 11,515 ms | 5,051 ms |
| Trace | 544 lines / 850,564 bytes | 86 lines / 275,247 bytes |
| Tool calls | 139 | 15 |
| Shell / patch / web | 50 / 10 / 2 | 13 / 2 / 0 |
| Poll and wait calls | 77 | 0 |
| CLI total-token accounting | 13,838,702 | 712,552 |
| Input / cached input | 13,816,901 / 13,557,760 | 702,796 / 645,120 |
| Output / reasoning output | 21,801 / 3,303 | 9,756 / 2,051 |
| Task workspace | 5.4 GiB | 1.1 MiB |
| New downloads or clones | 787,496,505 retained archive bytes plus ORFS clone | none |
| EDA evidence | 74 MiB plus native build tree | none; onboarding stopped before an engineering gate |

The forward session was 19m 4.961s shorter, used 89.21% fewer tool calls, and
reported 94.85% fewer total tokens. These figures are dominated by the
deliberately narrower forward-test scope and must not be interpreted as a
speedup benchmark.

The plugin cache itself remained 902 MiB, including 715 MiB `.strategy`,
49 MiB `.venv`, and 29 MiB `.git`; the packaging issue observed in the first
session was not improved by the onboarding workflow.

## Forward timeline

- **22:10:22–22:10:54 — instruction and finish-line setup.** The agent read
  the coordinator, its IHP/ledger references, and the OpenADA execution
  contract. It stated that identities must close before costly work.
- **22:10:59 — bounded inspection.** It inspected the empty workspace, only
  the explicitly supplied `PDK_ROOT` and `IHP_TEMPLATE_ROOT` to depth two,
  disk/memory/architecture, and container/Nix availability. There was no host
  or recursive PDK crawl.
- **22:11:22 — source identities.** It verified a clean official template
  checkout at `041830...` and resolved the active Ciel PDK symlink to
  `3b5a704...`.
- **22:11:33 — capabilities and local image.** It resolved OpenADA 0.4.0
  through the plugin-local launcher, ran `openada capabilities`, inspected the
  local image, and selected content-addressed reference
  `hpretl/iic-osic-tools@sha256:7371bae55da486f492cc270ea6137c4fcf3b11971de7a4506a74f62be143537a`.
- **22:11:49 — closed runtime probe.** One `docker run` used `--network none`
  and read-only PDK/template mounts. It recorded linux/amd64 image ID
  `sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb`
  and bounded tool versions: LibreLane 3.1.0.dev1, OpenROAD
  `26Q2-2270-g4c26918f5`, OpenSTA 3.1.0, KLayout 0.30.9, Netgen 1.5.321,
  Magic 8.3.664, Yosys 0.66, and Verilator 5.048.
- **22:12:28 — compatibility stop selected.** The agent classified the
  LibreLane/template-lock skew and unavailable Nix runtime as a project-freeze
  blocker rather than downloading another tool distribution.
- **22:13:04–22:13:14 — bounded project shell.** It wrote the finish-line and
  CPU-integration documents, then copied selected source, LibreLane, bondpad,
  cocotb, Nix-lock, and license files from the reviewed local template. No
  repository was cloned.
- **22:13:39–22:13:52 — ledger.** It initialized a draft ledger, recovered
  from invalid gap-enum arguments, recorded six open gaps, validated current
  paths/hashes, and observed the expected freeze rejection.
- **22:14:15–22:14:31 — handoff.** It retained a gate record and principal
  hashes, named `rtl-structural-check-passes` as the next scoped assertion only
  after project freeze, and stopped.

The longest user-visible update gap was about 2m03s, improved from the first
session's 8m01s and 4m59s gaps but still longer than the desired one-minute
progress cadence.

## Tool-call and failure accounting

The trace contains 15 tool calls: 13 shell calls and two patches. It contains
no web request, clone, download, background process, poll, or wait call.

Two shell-call batches contained unsuccessful subcommands:

1. `python` was not installed; the agent retried successfully with `python3`.
2. Ledger initialization succeeded, but four `add-gap` invocations guessed
   invalid stage/kind enum values. The same batch then deliberately attempted
   `freeze`, which correctly rejected the incomplete ledger. The next call
   corrected all gap enums and validated the draft.

Thus five command failures were avoidable interface/reasoning retries (one
Python command and four gap arguments); the sixth negative outcome was the
intended fail-closed freeze gate. The batch used `|| true` around validation and
freeze, so the outer shell status did not represent those subcommand outcomes;
the agent relied on their structured output instead.

## Identity-ledger outcome

| Ledger row | Result |
|---|---|
| Structure/current path hashes | `valid` |
| Lifecycle | `draft`, revision 6 |
| Template | official origin, commit `041830...`, copied `flake.lock` hashed |
| PDK | canonical active version path, revision `3b5a704...` |
| Flow intent | LibreLane commit `69b206...` |
| Runtime | OCI digest/platform/identity JSON hashed; runtime qualification rejected |
| Tool bindings | 0 |
| Collateral bindings | 0 |
| Open gaps | 6 |
| Freeze | `invalid`: missing `klayout`, `librelane`, `magic`, `netgen`, `openroad`, `opensta`, `verilator`, and `yosys` identities |
| Final ledger SHA-256 | `31c21f326087bbafae8d45447cc42094e0b414b7b5f5f18ca97b838ca00a03a1` |

The six retained gaps cover runtime/flow compatibility, unselected CPU and
firmware identities, unresolved package/pinout, missing OpenADA physical-flow
semantics, routed MCMM timing, and behavioral HDL simulation.

`validate` proved only that the draft's declared paths and hashes were
consistent. It did not prove freeze readiness, runtime compatibility, CPU
identity, collateral completeness, or any engineering result. The evidence root
remained empty, and no OpenADA preflight was run because project freeze was the
current stop condition.

## What improved from the first session

- Discovery stayed inside explicit roots and bounded image metadata instead of
  crawling the host.
- The already-installed OCI image was found before provisioning; no network
  fetch or version lottery occurred.
- Template, PDK, flow, image, platform, and lock identities were considered as
  one coupled stack before EDA.
- The official IHP full-chip shell replaced the first session's blank core-only
  ORFS starting point.
- “CPU” was not silently equated with the template demonstrator: loader,
  memories, reset, firmware, pad allocation, package, license, and verification
  decisions were explicitly listed.
- The compatibility mismatch and incomplete ledger remained failures rather
  than being repaired by speculative downloads or promoted to a pass.
- The next OpenADA preflight was deferred until its prerequisites were frozen.
- Native capability gaps and the [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK)
  Preview/signoff boundary remained explicit.

## Bugs and remaining UX gaps

1. **Interpreter mismatch.** The coordinator's main example uses `python`,
   while its reference examples use `python3`; the host only supplied
   `python3`. Use one portable interpreter convention or probe it once.
2. **Ledger enum discoverability.** The valid gap stages/kinds exist deep in
   the ledger reference, but the agent still guessed four human-facing gate
   names that the CLI rejected. Put the exact enums adjacent to the main
   coordinator example or expose them in concise machine-readable help.
3. **Masked exit status.** `validate ... || true` and `freeze ... || true`
   make a mixed shell batch appear successful. Provide a helper audit command
   that returns a single structured draft/freeze-readiness result without
   suppressing subcommand exits.
4. **Draft `valid` is easy to overread.** A draft with zero tool and zero
   collateral bindings returns `outcome: valid`. The claim text is scoped, but
   a prominent `freeze_ready: false` and complete missing-requirements array
   would reduce ambiguity.
5. **OCI tool-identity namespace is unresolved.** The image probe records
   versions, but the ledger requires separately hashed executable paths and
   validates filesystem paths in one namespace. A documented container-side
   binding/freeze workflow or OCI-native tool identity record is needed.
6. **No compatible turnkey runtime.** The official template is Nix-locked,
   while the available OCI image carries LibreLane 3.1.0.dev1. Provide a
   digest-pinned template-compatible image or a bounded, explicit
   requalification procedure; otherwise fresh users stop correctly but cannot
   continue.
7. **Runtime probe provenance is manual.** `OCI_RUNTIME.json` summarizes a
   real closed probe but is agent-authored; the raw capabilities/probe output
   was not retained in the empty evidence root. A deterministic helper should
   emit and hash this identity record directly.
8. **The shell copy is selective.** Source, flow, IP, cocotb, locks, and license
   were copied, but the complete template repository/Makefile was not. The final
   wording “official full-chip template copied” should instead say “selected
   full-chip shell files copied” unless the complete template is frozen.
9. **Template hardening gaps were not ledgered.** The copied config retains
   `GRT_ALLOW_CONGESTION: true` and disables `Checker.IllegalOverlap`. The IHP
   onboarding reference explicitly requires these to be reviewed; they should
   be open evidence/configuration gaps before freeze.
10. **Plugin payload remains oversized.** The forward plugin cache is still
    902 MiB because development material is installed with the skill bundle.
11. **Progress cadence remains loose.** The final two minutes had no visible
    update even though several short onboarding actions completed.

## Disposition from this evaluation

The same change set now standardizes every helper example on `python3`, places
the exact gap enums beside the coordinator workflow, and makes `validate`
return both `freeze_ready` and the complete missing tool/collateral/template
sets. Exercising the helper on the real IHP candidate also replaced an
impossible pre-run `seal-ring.gds` requirement with the immutable
`seal-ring.config` generator input. Focused tests cover these behaviors.

The masked multi-command status, lack of a template-compatible turnkey image,
automatic OCI probe provenance, oversized development-plugin payload, and
progress cadence remain product/workflow gaps. The integrated candidate uses a
container-side bind/freeze workflow and retains the LibreLane lock skew as an
explicit compatibility gap; that is candidate-specific requalification, not a
general compatibility claim.

## Honest engineering conclusion

Project identity onboarding is **fail**, in the useful fail-closed sense: the
draft is hash-consistent but not frozen. Template and PDK selection are
identified; runtime compatibility, exact tool bytes, required full-chip
collateral, CPU/firmware, package/pinout, and template-hardening decisions are
not closed. RTL, function, synthesis, P&R, timing, DRC, LVS, full-chip
completion, handoff, submission readiness, and foundry signoff are all **not
evaluated**.
