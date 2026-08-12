# Upstream submission runbook — IHP-Open-PDK svaricap hot-temperature fix

NOTHING in this directory has been submitted. A human runs these steps after
reviewing `issue-body.md`, `pr-body.md`, and the commit described below.

## What is already prepared

- **Fix commit** (local only, never pushed): branch `fix-svaricaphv-dsubw-tlevc`
  in the local clone `~/simra/pll-repro/IHP-Open-PDK`, commit `afb59ad` —
  authored and DCO-signed-off by `amirmabhout <amirmabhout@users.noreply.github.com>`
  per the project's CONTRIBUTING.md, no other trailers. Two files, one token
  each (`tlevc = 1` on the `dsubw` cards).
- `issue-body.md` — follows the project's `.github/ISSUE_TEMPLATE/bug_report.md`
  sections; self-contained (deck inline, no references to private infrastructure).
- `pr-body.md` — references the issue number (placeholder `#ISSUE_NUMBER`).
- Duplicate check done 2026-08-12: no existing IHP-Open-PDK issue or PR covers
  this (all svaricap hits are LVS/DRC-related).

## Steps

```console
# 1. File the issue first (PR body links to it)
gh issue create -R IHP-GmbH/IHP-Open-PDK \
  --title "sg13_hv_svaricap: ngspice transient fails above 52.47 C with positive control bias (dsubw vj=0.1 without TLEVC)" \
  --body-file issue-body.md
# note the returned issue number -> NNNN

# 2. Fork (once) and push the prepared branch
gh repo fork IHP-GmbH/IHP-Open-PDK --clone=false
cd ~/simra/pll-repro/IHP-Open-PDK
git remote add fork https://github.com/amirmabhout/IHP-Open-PDK  # if not present
git push fork fix-svaricaphv-dsubw-tlevc

# 3. Open the PR (edit ISSUE_NUMBER in pr-body.md first)
sed -i "s/#ISSUE_NUMBER/#NNNN/" pr-body.md
gh pr create -R IHP-GmbH/IHP-Open-PDK \
  --head amirmabhout:fix-svaricaphv-dsubw-tlevc \
  --title "ngspice: fix sg13_hv_svaricap hot-temperature transient failure (dsubw TLEVC)" \
  --body-file pr-body.md
```

## Review notes

- The commit intentionally contains no tool attribution; DCO sign-off is the
  submitter's own certification of origin.
- The issue's "available on request" line for the arithmetic mirror refers to
  `../junction_temperature.py`; attach it to the issue thread if maintainers ask.
- If maintainers prefer a different temperature mode (e.g. characterized TPB),
  the report's root cause stands regardless — only the fix line changes.
