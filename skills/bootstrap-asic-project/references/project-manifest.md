# Bootstrap identity ledger

The helper maintains a bounded identity and planning ledger. It is skill-owned
coordination state, not an OpenADA protocol object or engineering result.

## Contents

- [Claim boundary](#claim-boundary)
- [Deliverables and derived requirements](#deliverables-and-derived-requirements)
- [Authoritative inputs](#authoritative-inputs)
- [Lifecycle](#lifecycle)
- [Commands](#commands)
- [Interpretation](#interpretation)

## Claim boundary

The ledger format is `openada.skill/bootstrap-manifest/v0alpha1`. A successful
`freeze` means only:

> the required identities are structurally declared, their canonical regular
> files currently match the retained byte counts and SHA-256 values, and
> declared tool files are executable.

It does **not** identify a tool by executing it, parse the declared collateral,
prove that a role label is truthful, establish compatibility, execute a design,
or imply DRC/LVS/timing cleanliness or signoff. `set-tool --version` stores a
caller-declared string alongside the executable hash; preserve the bounded
version-probe evidence separately.

The helper emits `valid` or `invalid`, never an engineering `pass` or `fail`.
Actual OpenADA and native operation results remain separate evidence.

## Deliverables and derived requirements

Choose exactly one deliverable:

| Deliverable | Derived scope |
|---|---|
| `synthesized-core` | project intent, sources, PDK identity, RTL/function context, Verilator, Yosys, SDC, Liberty, licenses |
| `routed-core` | synthesized core plus flow tool, OpenROAD/OpenSTA, KLayout/Netgen, physical views, RCX, DRC and LVS inputs |
| `full-chip` | routed core plus a frozen template, IO/bondpad/padframe, seal ring, fill/density/antenna, pinout and package plan |
| `submission-candidate` | full chip plus the current submission checklist and retained waiver ledger |

A physical deliverable always requires its selected flow tool. IHP SG13G2
additionally requires the evaluated OpenROAD, OpenSTA, KLayout, Netgen, and
Magic identities. Other PDK/flow combinations declare their actual stage tools
through `flow.requirements`, allowing integrated timing or KLayout-based LVS
without pretending one universal backend set. Added requirements cannot weaken
the derived minimum.
A full chip does not automatically require a submission checklist; that is the
distinction between `full-chip` and `submission-candidate`.

## Authoritative inputs

Top-level file records bind:

- the project specification (function, interface, clock/reset, voltage, area,
  package/bonding, license policy, target shuttle and verification intent);
- the ordered source/firmware/memory manifest;
- optional template origin, immutable revision and lock file;
- runtime identity (OCI launch/mount/network policy, Nix lock, or native
  environment manifest);
- flow configuration and immutable revision; and
- a canonical absolute evidence root, which may be on external scratch storage.

Revisions are tagged as `git-sha1`, `git-sha256`, or `content-sha256`; their
values must match the selected immutable form. OCI references must use a
manifest digest and an explicit `linux/amd64` or `linux/arm64` platform.

Collateral entries have stable IDs independent of their closed roles. Repeat a
role with different IDs for corners and transitive inputs.

| Role family | Closed roles |
|---|---|
| Target/intent | `pdk.revision-attestation`, `constraints.sdc`, `license.manifest` |
| Standard cells | `standard-cell.liberty`, `.lef`, `.gds`, `.cdl` |
| IO and bondpads | `io.liberty`, `.lef`, `.gds`, `.cdl`; `bondpad.lef`, `.gds` |
| Macros | `macro.liberty`, `.lef`, `.gds`, `.cdl`, `.verilog` |
| Physical checks | `drc.deck`, `lvs.deck`, `rcx.rules`, `antenna.deck`, `density.deck`, `fill.deck` |
| Full-chip assembly | `seal-ring.config`, `padframe.config`, `pdn.config`, `pinout`, `package.plan` |
| Submission | `submission.checklist`, `waiver.ledger` |

`seal-ring.config` is the immutable generator/configuration used before the
run, not the generated final seal-ring GDS. Retain that generated GDS with the
native run evidence after execution.

Binding one executable Python, Ruby, or Tcl entry file does not enumerate its
transitive reads. Bind each known included deck, setup, layer map, or revision
attestation under a stable ID and retain the native run directory.

## Lifecycle

`init` creates a `draft`. Draft mutations increment a ledger revision and name
the last action. `freeze` always checks every current path/hash, executable
mode, derived deliverable requirement, template requirement, OCI identity and
flow-specific addition before changing state to `frozen`.

A frozen ledger rejects mutations. Use `thaw --reason ...`, make the intentional
change, rerun affected engineering stages into fresh evidence, then freeze
again. The current revision and last thaw reason are coordination metadata, not
a full audit log; source control and retained evidence provide that history.

Gaps have stable IDs, stage, kind, detail, and `open|resolved` status.
`resolve-gap` retains the original detail plus a resolution; it does not delete
history or prove that an engineering stage passed.

## Commands

Create a draft with all top-level identity inputs. Full-chip targets require
the four `--template-*` arguments:

```bash
python3 scripts/bootstrap_manifest.py init \
  --output /project/.openada/bootstrap-manifest.json \
  --project-root /project --name cpu-chip --deliverable full-chip \
  --top chip_top --project-spec /project/PROJECT_SPEC.md \
  --source-manifest /project/SOURCE_MANIFEST.sha256 \
  --template-origin https://example/template.git \
  --template-revision-scheme git-sha1 --template-revision COMMIT \
  --template-lock /project/flake.lock \
  --pdk-id ihp-sg13g2 --pdk-root /foss/pdks/ihp-sg13g2 \
  --pdk-revision-scheme git-sha1 --pdk-revision COMMIT \
  --runtime-kind oci --runtime-profile iic-osic-tools \
  --runtime-identity /project/OCI_RUNTIME.json \
  --image-reference registry/image@sha256:DIGEST \
  --image-platform linux/amd64 \
  --flow-name librelane --flow-tool librelane \
  --flow-revision-scheme git-sha1 --flow-revision COMMIT \
  --flow-config /project/librelane/config.yaml \
  --evidence-root /scratch/cpu-chip-evidence
```

Bind and deliberately replace collateral by stable ID:

```bash
python3 scripts/bootstrap_manifest.py bind-file MANIFEST \
  --id stdcell-lib-typ --role standard-cell.liberty --path /pdk/typ.lib
python3 scripts/bootstrap_manifest.py replace-file MANIFEST \
  --id stdcell-lib-typ --role standard-cell.liberty --path /pdk/reviewed-typ.lib
python3 scripts/bootstrap_manifest.py remove-file MANIFEST --id obsolete-deck
```

Record exact executable bytes and a separately observed version string:

```bash
python3 scripts/bootstrap_manifest.py set-tool MANIFEST \
  --name openroad --path /foss/tools/bin/openroad \
  --version 'OpenROAD 26Q2 ...'
python3 scripts/bootstrap_manifest.py remove-tool MANIFEST --name obsolete-tool
```

`set-flow` atomically changes its revision, config digest, selected executable,
and extra declared requirements. It cannot leave an old top-level config hash
behind.

The other top-level setters are `set-project`, `set-template`, `set-pdk`,
`set-runtime`, and `set-evidence-root`. `set-pdk` is a stack change: it also
accepts the new template/flow identity and deliberately clears all collateral
and tool bindings. A runtime change clears every tool identity. Rebind and
requalify them rather than carrying stale declarations across either change.

Retain and resolve capability/evidence gaps:

Valid stages are `project`, `rtl`, `function`, `synthesis`, `physical`,
`timing`, `padframe`, `drc`, `lvs`, `handoff`, and `submission`. Valid kinds
are `capability`, `collateral`, `compatibility`, `evidence`,
`external-acceptance`, and `resource`.

```bash
python3 scripts/bootstrap_manifest.py add-gap MANIFEST \
  --id routed-mcmm --stage timing --kind capability \
  --detail 'OpenADA has no routed MCMM timing operation'
python3 scripts/bootstrap_manifest.py add-gap MANIFEST \
  --id routed-timing-evidence --stage timing --kind evidence \
  --detail 'No separately retained routed timing review exists yet'
python3 scripts/bootstrap_manifest.py resolve-gap MANIFEST \
  --id routed-timing-evidence \
  --resolution 'Reviewed native project-flow evidence retained separately'
```

The OpenADA capability gap remains open: native evidence may satisfy the
separate evidence need but cannot create or resolve a missing semantic profile.

Freeze and later revise intentionally:

```bash
python3 scripts/bootstrap_manifest.py freeze MANIFEST
python3 scripts/bootstrap_manifest.py validate MANIFEST --require-frozen
python3 scripts/bootstrap_manifest.py thaw MANIFEST \
  --reason 'replace SDC after interface review'
```

Validation of a frozen ledger always rechecks paths and executable modes even
without `--check-paths`. A draft validation can be structure-only unless that
flag is supplied. Validation also reports `freeze_ready` and a complete
`missing_freeze_requirements` object; `outcome: valid` by itself still means
only that the requested validation scope was internally consistent.

## Interpretation

Report the ledger state and identity claim separately from every engineering
gate. Open gaps are useful and may coexist with a frozen project context; they
do not become waivers. A changed frozen input makes dependent evidence stale
even when all new hashes are internally consistent.
