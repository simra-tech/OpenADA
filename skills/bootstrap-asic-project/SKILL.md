---
name: bootstrap-asic-project
description: Bootstrap and coordinate a fresh open-source ASIC project when the PDK, EDA runtime, project configuration, or physical-design collateral is missing or ambiguous. Use for requests to choose an open PDK, provision compatible tools, create a reproducible ASIC workspace, turn RTL such as a CPU into a core or full-chip tapeout candidate, add an IO/pad ring, or stage-gate synthesis, place-and-route, timing, DRC, LVS, and final handoff without confusing open-tool evidence with foundry signoff.
---

# Bootstrap ASIC Project

Turn an underspecified chip idea into a frozen project context and a sequence of
honest engineering gates. Use `$openada:openada` for every implemented OpenADA
assertion. Keep provisioning, design creation, native gap work, and foundry
acceptance distinct from OpenADA-normalized evidence.

## Define the finish line first

Record the intended deliverable before installing or generating anything:

- `core`: synthesized or routed hard macro without package-facing IO;
- `full-chip candidate`: core plus IO/ESD cells, power pads, bondpads, pad ring,
  seal ring, fill/density, final GDS and reference netlist; or
- `submission candidate`: a full-chip candidate checked against the current
  shuttle checklist and ready for the foundry's intake review.

Also record the function, interfaces, clock, reset, voltage domains, die or area
limit, package/bonding assumption, license constraint, target shuttle, and
required verification. Do not let “CPU” silently mean only synthesizable RTL.
If the user authorizes autonomous choice, choose the smallest target consistent
with these constraints and document the decision. Otherwise ask one choice that
materially changes the process or padframe.

## Inspect in the safe order

1. Establish one writable project root and a separate evidence root. Treat PDK,
   official template, and shared source checkouts as read-only.
2. Resolve the OpenADA CLI exactly as `$openada:openada` specifies. Record the
   plugin and CLI versions separately; a plugin marketplace installation does
   not install the Python entry point or EDA products.
3. Check free disk, memory, architecture, container/Nix availability, and an
   already-present compatible runtime before downloading tools. Prefer a pinned
   compatible runtime already on the machine over unrelated “latest” binaries.
4. Run `openada capabilities`, then one scoped preflight for only the smallest
   next implemented assertion. A preflight pass is tool readiness, not a design
   result.
5. Read configured PDK roots and explicit project context. Do not recursively
   crawl the host or a PDK tree to compensate for an empty catalog. Do not infer
   that an empty preflight PDK list means no PDK exists.

`--profile iic-osic-tools` recognizes an existing `/foss` layout; it does not
launch Docker or install that layout. Treat OCI/Nix launch and mount policy as a
separate provisioning decision.

## Select one coherent target stack

Compare candidates using official current sources: process status, license,
voltage and device needs, standard-cell and IO libraries, SRAM/macros, supported
flow, public DRC/LVS decks, shuttle availability, package constraints, and the
quality of a complete full-chip example. Never splice a CPU synthesized for one
library into DRC/LVS evidence from another PDK.

For an IHP SG13G2 full-chip request, read
[`references/ihp-sg13g2-full-chip.md`](references/ihp-sg13g2-full-chip.md) before
provisioning. It records the evaluated official template and its coupled PDK,
tool, padframe, verification, and submission assumptions. Re-verify current
submission dates and required decks with IHP; do not silently float one pin.

For another PDK, require the same classes of authoritative collateral. If a
complete open padframe or final LVS path is unavailable, narrow the deliverable
to a core or report the full-chip row as not evaluated.

## Freeze a bootstrap manifest

Create `.openada/bootstrap-manifest.json` before the first expensive run. Use
the bundled identity-ledger helper rather than inventing a project-wide ambient
profile. First write a project specification covering the finish-line choices
above and a hash-ordered source/firmware manifest:

Use a lowercase stable ledger key for `--pdk-id`, not necessarily the PDK's
native display name. For example, use `sky130a` for native name `sky130A`, while
retaining the native spelling and exact path in project/runtime context.

```bash
python3 /path/to/skill/scripts/bootstrap_manifest.py init \
  --output /absolute/project/.openada/bootstrap-manifest.json \
  --project-root /absolute/project --name cpu-chip --deliverable full-chip \
  --top chip_top --project-spec /absolute/project/PROJECT_SPEC.md \
  --source-manifest /absolute/project/SOURCE_MANIFEST.sha256 \
  --template-origin https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template \
  --template-revision-scheme git-sha1 --template-revision TEMPLATE_COMMIT \
  --template-lock /absolute/project/flake.lock \
  --pdk-id ihp-sg13g2 --pdk-root /absolute/pdk-root \
  --pdk-revision-scheme git-sha1 --pdk-revision PDK_COMMIT \
  --runtime-kind oci --runtime-identity /absolute/project/OCI_RUNTIME.json \
  --runtime-profile iic-osic-tools \
  --image-reference registry/image@sha256:DIGEST \
  --image-platform linux/amd64 --flow-name librelane --flow-tool librelane \
  --flow-revision-scheme git-sha1 --flow-revision FLOW_COMMIT \
  --flow-config /absolute/project/librelane/config.yaml \
  --evidence-root /absolute/scratch/evidence
```

Use stable IDs with `bind-file` for every authoritative Liberty, LEF, GDS, CDL,
DRC, LVS, revision attestation, padframe, package, and checklist input, and
`set-tool` for each native tool path plus its separately observed version.
Then run `freeze` followed by `validate --require-frozen`. See
[`references/project-manifest.md`](references/project-manifest.md) for the
deliverable-dependent requirements, lifecycle, closed roles, and claim boundary.
When adding gaps, use only stages `project`, `rtl`, `function`, `synthesis`,
`physical`, `timing`, `padframe`, `drc`, `lvs`, `handoff`, or `submission`, and
kinds `capability`, `collateral`, `compatibility`, `evidence`,
`external-acceptance`, or `resource`.

This ledger proves only structurally declared, current hash identities. It does
not parse role contents, probe compatibility, or establish engineering status.
It is not an OpenADA operation profile, driver capability, conformance result,
or signoff certificate. Keep operation result envelopes and native run
directories beside it as separate evidence.

## Provision without creating a version lottery

Provision only after the target stack is selected and the user has authorized
installation or the original request already says to provision autonomously.

- Pin source repositories by full commit and runtime images by manifest digest.
- Keep one compatible tool/flow/PDK set. Do not combine a current flow with an
  old OpenROAD binary, then patch around command incompatibilities.
- Estimate required bytes before pulling an image, PDK, or large tool bundle.
  Put large disposable outputs on a filesystem with adequate headroom.
- Use the runtime's reviewed entry point and closed mounts. Mount the project
  writable; mount the PDK, flow source, and OpenADA source read-only; keep
  network disabled during EDA execution after setup.
- Run bounded version probes inside the execution environment and record exact
  paths and versions in the manifest.

If provisioning fails, retain the exact failed gate and diagnostic. Do not
download several unrelated tool distributions as speculative recovery.

## Start from the right design shell

For a full chip, begin from the PDK maintainer's reviewed full-chip template,
not a blank RTL directory or a core-only P&R example. Freeze the template before
editing a writable project copy. Preserve its IO/ESD, power-domain, bondpad,
seal-ring, fill, density, extraction, and verification structure while replacing
the example core.

Define CPU programmability and memory explicitly. A fixed self-test ROM can
demonstrate execution but is not a generally programmable CPU. A serious CPU
candidate needs a pinned core, memories or external bus, boot/loading path,
reset sequencing, firmware identity, and tests that cover those boundaries.
Audit every RTL, firmware, memory image, macro, and generated-artifact license.

## Advance one evidence gate at a time

| Gate | Minimum evidence | Stop condition |
|---|---|---|
| Project freeze | Frozen, hash-consistent identity ledger plus reviewed project specification | Any unresolved PDK, top, flow, deck, runtime, or mislabeled-input question |
| RTL structure | OpenADA `rtl-check` pass for exact sources/top | Engineering `fail` or `unknown` |
| Strict lint | `openada.operation/rtl.lint/v1alpha1` pass | Any normalized warning/error |
| Function | Self-checking RTL simulation plus architectural/firmware tests | Missing or failing behavior; OpenADA has no current HDL-simulation profile |
| Synthesis | `openada.operation/logic.synthesize/v1alpha1` pass when its request can express the stack | Unmapped cells, incomplete closure, or unsupported multi-library handoff |
| Physical implementation | Fresh floorplan/PDN/place/CTS/route/GDS run with stage reports | Congestion, route, antenna, clock, power, or stream-out failure |
| Timing | All declared corners and routed parasitics reviewed | OpenADA timing is currently one-corner ideal-interconnect only |
| Full-chip assembly | IO/ESD, power pads, bondpads, pad ring, seal ring, fill and density present | Core-only GDS or unresolved package/power assumptions |
| DRC | Official selected deck, fresh exact report, zero unexplained markers | `fail`, `unknown`, stale report, blanket waiver, or incomplete deck scope |
| LVS | Final GDS extraction versus an independently generated powered reference netlist | Layout-versus-layout comparison, mismatch, or incomplete hierarchy |
| Handoff | Hashed GDS/netlists/reports, pinout, package plan, waiver and limitation ledger | Any required artifact or foundry checklist row missing |

Preflight only the next OpenADA-supported assertion immediately before that
operation. Preserve `execution.status` separately from `engineering.status`.
Route RTL review to `$openada:review-rtl-architecture`, mapping review to
`$openada:assess-synthesis-and-inference`, and supported synthesis-stage timing
to `$openada:assess-asic-timing`.

## Handle missing OpenADA operations explicitly

OpenADA currently has no semantic operation for behavioral HDL simulation,
floorplanning, place/CTS/route, padframe generation, extraction/PEX, routed
MCMM timing, power integrity, or final submission assembly.

The default action is **not evaluated — capability unavailable**. If the user
explicitly requests an exploratory end-to-end run and authorizes native gap
work, execute the reviewed project flow only as a labeled native operation and
record:

- why no OpenADA profile applied;
- exact executable, version, argv, environment, cwd, timeout, and exit;
- all declared input and output hashes;
- native reports and the independent checks used to interpret them; and
- the missing semantic operation that should be added later.

Never place native output inside an `openada.result` envelope, call it OpenADA
evidence, or let its success promote driver maturity. Return to OpenADA for DRC
or LVS only when their exact caller-supplied inputs and driver contracts fit.

## Repair and resume

Classify each failure before changing anything: design, constraint, collateral,
tool/flow compatibility, environment, evidence contract, or resource capacity.
Change one class at a time, preserve the failed evidence directory, and rerun
into a fresh output path. Use `thaw --reason`, an explicit replacement command,
and `freeze` when a frozen input intentionally changes; explain why prior
evidence is no longer authoritative. Resolve a retained gap only with an
explicit resolution—never by deleting its history.

## Report the honest conclusion

Return a gate table with `pass`, `fail`, `unknown`, or `not evaluated`; exact
tool, PDK, template and source revisions; artifact paths and hashes; fixes and
retries; OpenADA gaps; native-gap evidence; and submission limitations.

Use **open-PDK tapeout candidate** only when every required pre-intake row is
proved. Use **submission candidate** only when the current program checklist is
complete. Foundry review and acceptance remain external; signoff: not claimed.
