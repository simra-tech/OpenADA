# Qualitative ASIC onboarding evaluation

This package records an exploratory fresh-workspace session, a bounded forward
test after improving the plugin, and the source-only CPU candidate produced by
the integration exercise. It evaluates onboarding behavior, evidence
discipline, recovery behavior, and the boundary between OpenADA operations and
explicitly native EDA work.

## Package contents

- [`task.md`](task.md) preserves the sanitized user task.
- [`first-session-report.md`](first-session-report.md) records the observed
  timeline, engineering gates, costs, contract behavior, and product findings.
- [`forward-session-report.md`](forward-session-report.md) records the fresh
  fail-closed test of the new bootstrap coordinator and compares its descriptive
  workflow costs without presenting a benchmark claim.
- [`final-integration-report.md`](final-integration-report.md) records the exact
  full-chip integration stack, retries, gate outcomes, final-view hashes,
  OpenADA replay findings, and the unresolved DRC, timing, package, and signoff
  boundaries.
- [`candidate/`](candidate/) retains the minimal reproducible source overlay
  against the exact official IHP full-chip template; generated run evidence is
  deliberately excluded.

Raw prompts, tool transcripts, and generated design artifacts are intentionally
not copied here. The restricted external capture for this session is:

```text
/dev/shm/openada-vibe-cpu-20260716/codex-home/sessions/2026/07/16/
rollout-2026-07-16T20-55-09-019f6cb6-3b81-7240-b059-2dadef29a106.jsonl
```

The corresponding external workspace was
`/dev/shm/openada-vibe-cpu-20260716/workspace`. These paths may be ephemeral and
are not part of the repository's public evaluation contract.

The forward-test capture is likewise retained externally at:

```text
/dev/shm/openada-vibe-cpu-20260716/codex-home/sessions/2026/07/16/
rollout-2026-07-16T22-10-22-019f6cfb-1745-7b63-bd1a-322d6de60bac.jsonl
```

## Method

The session began in an empty Git workspace with an isolated Codex home and the
current OpenADA 0.4.0 plugin installed from this checkout. The task authorized
autonomous provisioning and explicitly permitted native EDA work when OpenADA
had no matching semantic operation, provided that the gap was labeled.

The review used the persisted session events and retained result envelopes and
native reports. It checked:

1. setup and runtime discovery;
2. tool, flow, and PDK selection stability;
3. progress through RTL, simulation, synthesis, physical implementation,
   timing, DRC, LVS, padframe, and handoff gates;
4. separation of `execution.status` from `engineering.status`;
5. provenance, artifact retention, and signoff language; and
6. retries, downloads, disk use, interaction latency, and avoidable friction.

This is a qualitative case study, not a benchmark result. There was no paired
control session, repeated trial, frozen campaign manifest, randomized
condition, or independent score. The host also contained useful EDA tools and a
pre-existing IIC-OSIC-TOOLS image, while network provisioning was allowed.
Results therefore describe this run, not a comparative performance estimate.

The capture reports Codex CLI `0.144.5`. The repository's v0alpha1 paired-agent
adapter and trace schema are pinned to `0.144.3` and reject other versions.
The raw capture must not be passed to that adapter while claiming the pinned
version. Requalification and a new adapter/schema revision are required before
this session format can enter a paired campaign.

## Interpretation boundary

The selected [IHP Open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK) is public
preview collateral and is not production signoff material. OpenADA-normalized
checks, native open-source flow reports, and this qualitative evaluation do not
replace IHP or foundry review.

The evaluated route should now prefer IHP's reviewed
[LibreLane full-chip template](https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template)
and [full-chip documentation](https://ihp-open-pdk-docs.readthedocs.io/en/latest/digital/librelane_full_chip.html).
Operational submission gates remain external; see IHP's
[IP development steps](https://github.com/IHP-GmbH/Open-Silicon-MPW/blob/main/IP-development-steps.md)
and [submission process](https://github.com/IHP-GmbH/Open-Silicon-MPW/blob/main/Submission-process.md).
