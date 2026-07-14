# Paired agent evaluation kit

This directory defines a repeatable, condition-neutral way to compare a raw
terminal agent with the same agent plus OpenADA. It does not contain benchmark
results and does not make a performance claim. Historical traces selected the
metrics; only fresh, planned, independently scored trials can populate them.

The first task is the pinned public IHP inverter Xschem-to-ngspice workflow.
Both conditions receive the same neutral evaluation task card and submission
schema, native binaries, design, PDK, startup files, writable task directory,
time budget, and network policy. Those common evaluation bytes are not an
OpenADA agent-layer treatment. The raw condition must contain no OpenADA
distribution, CLI, package, result schema, shared skill, repository, prior
output, or injected OpenADA context. The treatment condition adds only the
exact bundle declared by the campaign's treatment manifest. Ignoring that
treatment remains an intention-to-treat outcome; it is not a reason to delete
or rerun a poor result.

## What v0alpha1 includes

- a precommitted paired AB/BA campaign plan with at least five blocks;
- a strict supervisor record for each planned assignment, including the only
  attempt, observed dispatch order, one campaign-local monotonic clock domain,
  and both intervals in its pair;
- a content-free reducer for a pinned Codex `exec --json` event stream;
- a condition-blind native netlist, log, and binary-waveform scorer;
- one trial assembler which binds the plan, supervisor record, reduced trace,
  final response, and independent score, then seals the row with the
  campaign's Ed25519 key;
- an intention-to-treat summarizer which accounts for every planned trial and
  emits fixed condition-level accounting even when it refuses a comparison;
  and
- a public verifier which either authenticates the signed publisher summary or
  independently recomputes it from the exact plan and every sealed row.

The kit is offline and bring-your-own-trace. It deliberately does not launch a
model or handle credentials. Agent- and provider-specific live runners belong
in adapters outside `src/openada` after their isolation and identity boundaries
are independently proven.

## Trust boundary

The participant-visible workspace is untrusted. Keep the plan, raw event
stream, final-response capture, supervisor record, and score output outside it.
Use a fresh workspace whose required outputs do not exist initially. Mount the
design, PDK, startup inputs, task card, and condition bundle read-only; permit
writes only to a fresh `/task` and isolated temporary directory. Run the scorer
later with the completed task directory read-only and without the condition
label.

Keep the campaign signing seed outside the participant workspace and readable
only by its owner. A valid seal lets the summarizer detect a modified assembled
row, revalidate its reduced-trace and score semantics, and recompute its
protocol and metrics. It does not make the campaign operator independent,
prove a supervisor attestation, or prove which native process generated
retained bytes.

Generate a fresh random `execution_clock.domain_id` for each campaign and set
its origin to `first_dispatch_zero_ms`. The trusted supervisor must subtract
the first dispatch's host-monotonic value before publishing any interval, so
the first planned dispatch starts at zero. Never use a boot ID, machine ID,
host-monotonic absolute value, or a reusable clock-domain identifier.

Do not run an API-connected agent inside the network-disabled EDA container by
mounting its credentials. That gives model-generated commands access to the
credential and gives EDA processes an outbound path. A claim-eligible live
runner needs an API-connected supervisor which owns credentials and brokers
actions into a separate network-none EDA executor. It must not expose a host
shell or container socket to the participant.

## Trial protocol

1. Freeze exact harness/model intent, runtime/image, design, PDK, tool binaries,
   startup files, treatment bundle, prompt, policy, budget, scorer, adapter, and
   trial-signing identities plus the fresh execution-clock nonce in a campaign
   document. The model release must be immutable. The validator cross-checks
   the task and runtime declarations against canonical task bytes. The
   treatment manifest records every
   participant-visible treatment file with its role, path, size, SHA-256, mode,
   and media type, and binds the wheel, one CLI, and canonical shared-skill tree.
2. Run `plan.py` before any outcome is observed. It creates opaque trial IDs
   and an interleaved randomized raw/OpenADA order. Preserve the plan bytes and
   private seed file; reveal the seed only after every outcome is fixed. The
   plan records the fixed `hmac-sha256-fisher-yates-v1` algorithm so schedule
   reproduction does not depend on a language-runtime pseudorandom generator.
3. Give each fresh agent only the byte-identical task card and its declared
   condition. Disable web search, memories, prior sessions, subagents, user
   intervention, and EDA-executor network access in both conditions.
4. Capture the agent stream outside the workspace. Raw content remains
   restricted. Reduce it to fixed action/status buckets; never publish prompts,
   reasoning, commands, paths, output, tool arguments/results, search text,
   native IDs, or errors.
5. Independently score the fixed native outputs. Do not infer engineering
   success from agent completion, a zero process exit, the final answer, or an
   OpenADA result.
6. Launch each assignment once and never select a rerun based on its outcome.
   Timeouts, agent errors, adapter rejection, malformed or absent final JSON,
   unknown evidence, and treatment non-adoption remain outcomes. A trusted
   supervisor—not the participant—must bind capture files, enforce the budgets
   and pair-span policy, record dispatch and both pair intervals on one fresh
   monotonic clock domain, and attest the raw-absence/treatment-exact boundary.
   Finalize both supervisor rows only after both assignments in their pair have
   finished so they carry the same complete pair observation.
7. Summarize only after all assignments are present. The minimum is five paired
   blocks; six gives an exactly balanced AB/BA order. Publish counts, medians,
   ranges, and paired deltas without a significance or universal-performance
   claim. Missing or duplicate rows and schedule or protocol conflicts refuse
   comparison but still appear in fixed condition accounting; invalid or
   unplanned rows are rejected safely.

## Run the offline validators

Use a source checkout or sdist and install the conformance dependencies first:

```bash
python3 -m pip install -e '.[conformance]'
```

Generate a private trial-signing seed first. The command creates a new `0600`
file without overwriting an existing path and prints the public identity to
standard output:

```bash
python3 evaluation/paired-agent/keygen.py \
  private-trial-signing-seed.txt > trial-signing-public.json
```

Copy the three public fields into the campaign's `trial_signing` object; never
put the private seed in the campaign bundle. Author the campaign, treatment
manifest, and one supervisor record per assignment against
[`campaign-v0alpha1.schema.json`](schemas/campaign-v0alpha1.schema.json),
[`treatment-manifest-v0alpha1.schema.json`](schemas/treatment-manifest-v0alpha1.schema.json),
and
[`supervisor-v0alpha1.schema.json`](schemas/supervisor-v0alpha1.schema.json).
Task-file and treatment-manifest paths are normalized paths relative to the
campaign bundle. Every script provides `--help`.

The treatment manifest must contain unique participant paths and at least the
`openada-cli`, `openada-package`, `openada-schema`, and `openada-skill` roles.
The campaign separately pins the manifest hash, wheel hash, sole CLI hash, and
domain-separated canonical skill-tree hash. The trusted supervisor observes
the matching manifest hash in the treatment condition and its absence in raw.

Create the randomized plan and its separate private reveal this way:

```bash
python3 evaluation/paired-agent/plan.py campaign.json \
  --generate-seed-file private-seed.txt > plan.json
```

`plan.py` creates the reveal file atomically with mode `0600` and refuses to
overwrite it. Do not pass a real campaign seed with `--seed-hex`: command-line
arguments can appear in shell history and process listings. Keep the seed and
plan outside participant-visible files. The randomization seed and Ed25519
trial-signing seed are different secrets with different purposes.
`plan.json` records
`"randomization_algorithm":"hmac-sha256-fisher-yates-v1"`; validators reject
another or missing algorithm rather than silently interpreting its schedule.

A typical per-trial reduction and assembly is:

```bash
python3 evaluation/paired-agent/adapters/codex_jsonl.py \
  --input restricted/codex.jsonl \
  --codex-cli-version 0.144.3 \
  --process-exit-code 0 \
  --fresh-thread \
  --adapter-duration-ms 1234 > restricted/reduced-trace.json

python3 evaluation/paired-agent/assemble.py \
  --campaign campaign.json \
  --plan plan.json \
  --supervisor restricted/supervisor.json \
  --trace restricted/reduced-trace.json \
  --submission restricted/final-response.json \
  --workspace completed-task \
  --signing-key private-trial-signing-seed.txt \
  --trial-id trial-000000000000000000000000 > trial.json
```

On safely rejected adapter input, `codex_jsonl.py` exits 2 but still emits one
schema-valid fixed-shape rejected trace on standard output and a bounded
content-free reason code on standard error. Retain that trace, record
`adapter_failed` in the supervisor row, assemble the planned outcome, and do
not rerun or omit it.

After all planned rows exist, repeat `--trial` for every row and reveal the
saved seed:

```bash
python3 evaluation/paired-agent/summarize.py \
  --campaign campaign.json \
  --plan plan.json \
  --signing-key private-trial-signing-seed.txt \
  --seed-file private-seed.txt \
  --trial trial-1.json \
  --trial trial-2.json > summary.json
```

A refused comparison exits 1 and still emits complete accounting. Unsafe or
invalid input exits 2. No script in this directory launches a participant or
handles provider credentials. The summarizer verifies every Ed25519 seal and
revalidates reduced-trace and native-score semantics, then recomputes protocol
and metric relationships before using a row. It then adds deterministic,
plan-ordered evidence records containing each supplied trial ID, canonical
trial SHA-256, and trial-signature SHA-256, and seals the entire summary under
a summary-specific Ed25519 domain with the campaign key.

If supplied-row accounting is partial—for example, a planned row is missing or
duplicated—the summary withholds the randomization seed (`seed_hex` is `null`),
marks `schedule_verified` false, and refuses comparison even when the correct
seed was supplied. The signed refused summary still commits to every supplied
plan-bound row and emits fixed condition accounting. An unplanned row is
invalid input, not another experimental outcome.

## Verify a published summary

Summary-only verification needs the exact campaign and signed summary:

```bash
python3 evaluation/paired-agent/verify_summary.py \
  --campaign campaign.json \
  --summary summary.json > verification.json
```

This `summary-only` mode validates the public schemas and campaign-byte binding,
then authenticates the summary with the campaign's Ed25519 public key. It proves
that the campaign-key holder signed those summary bytes; it cannot recompute the
publisher's accounting, eligibility, scores, metrics, schedule, or evidence
claims.

Full verification additionally requires the exact frozen plan and every sealed
sanitized row in the summary evidence list:

```bash
python3 evaluation/paired-agent/verify_summary.py \
  --campaign campaign.json \
  --summary summary.json \
  --plan plan.json \
  --trial trial-1.json \
  --trial trial-2.json > verification.json
```

Repeat `--trial` for every evidence record, including repeated rows in a
duplicate/refused summary. Every supplied row must bind one frozen plan
assignment; unplanned rows are rejected. `full` mode verifies the summary and
trial seals, checks the ordered evidence commitments, and independently
recomputes every unsigned summary field from the exact campaign, plan, and
supplied rows. A
successful verification emits
[`summary-verification-v0alpha1`](schemas/summary-verification-v0alpha1.schema.json)
with its mode and canonical summary hash. This verifies publication derivation;
it does not rerun the native scorer or establish native-process causality.

## Independent task truth

The IHP scorer does not inspect the condition or award points for using
OpenADA. It independently requires bounded regular single-link native files,
checks the active inverter netlist and exact deck-owned raw write, rejects
unresolved-symbol markers, checks clean pinned ngspice log evidence, and parses
the Spice3 binary raw file itself. The waveform must contain finite real data,
80 or 81 points, strictly increasing time from 0 through 2 microseconds, the
required voltage vectors, nominal 1.2 V supply, and the reviewed settled
high/low/high inverter response.

The scorer keeps separate conclusions:

- `engineering_verdict` describes what the native waveform proves;
- `verified_artifact_complete` requires every requested native artifact and
  report, but does not claim which process generated those bytes;
- `reported_status_correct` compares the participant's claim with native
  truth; and
- artifact and provenance fields show which participant-reported hashes and
  identities independently agree.

A valid waveform can prove the electrical assertion while a missing required
log leaves the requested artifact bundle incomplete. Missing or malformed
waveform evidence is `unknown`; structurally valid evidence which violates the
inverter assertion is `fail`. These are not collapsed into an opaque weighted
score.

The primary metric is deliberately named `verified_artifact_complete`. The
scorer proves semantics of retained bytes and the accuracy of the participant's
artifact report; it is not cryptographic proof that Xschem or ngspice generated
those bytes. `native_execution_verified` remains ineligible until a future
trusted executor audit binds exact native argv/cwd, opened input hashes, fresh
outputs, exits, and output hashes. Do not relabel artifact completeness as
verified process execution.

## Metric-specific eligibility

Missing telemetry is unknown, never zero and never synthesized.

| Metric | Required authority |
|---|---|
| Engineering outcome and artifact/provenance completeness | Fresh policy-conforming workspace plus the independent scorer |
| Exact native-process execution and output causality | Trusted executor audit; unavailable in this offline version |
| Adapter wall time | Trusted monotonic launcher start/wait interval |
| Codex action counts | Complete pinned-parser, single-turn event lifecycle with no uncovered child work |
| Codex token totals | Completed fresh non-resumed single-turn cumulative usage snapshot |
| First relevant EDA operation, exact retries, native-result volume | Identical trusted executor/broker audit stream in both conditions |
| Session, provider request count/identity, request latency, TTFT, API retry metrics | Transport-level authoritative trace; unavailable from Codex `exec --json` alone |

Codex CLI 0.144.3 JSONL exposes a thread ID, turn boundaries, action
lifecycles, command exits, and a cumulative usage snapshot. It does not expose a
native turn ID, provider/API request IDs, event timestamps, request boundaries,
TTFT, or observed provider/model identity. The reducer therefore supports
bounded action/usage evidence for one fresh turn while explicitly marking the
unsupported provider/session metrics ineligible.

Eligibility is per metric. Missing provider request telemetry does not erase an
independently verified engineering outcome, and a valid engineering artifact
does not authorize a provider-latency claim.

## Privacy and publication

Raw agent event streams may contain private paths, project data, prompts,
reasoning, commands, tool arguments/results, and incidental credentials. Keep
them restricted with short retention. The reducer discards those fields rather
than trying to sanitize arbitrary text with regexes. The assembled public row
contains no capture paths, restricted-content hashes, or per-trial wall-clock
timestamps, but its opaque pair and condition data can still be linkable.
Summary-only publication minimizes linkability but authenticates only what the
campaign-key holder published; it cannot support independent recomputation.
Any public comparison claim should publish the exact campaign bundle, plan,
signed summary, and every sealed sanitized row committed by the summary. That
enables full verification but deliberately accepts the rows' residual
pair/condition linkability. Artifact hashes, relative action/timing data, and
usage totals are also possible row fingerprints. Assess that disclosure before
running the campaign, and never publish the restricted raw captures or
supervisor records. Retain those restricted inputs only for the audit window.

Never seed a trial from restricted historical product-use traces. They are
observational calibration without authoritative versions, turns, requests, or
final engineering outcomes and cannot be reused as either condition.
