# IHP clean/failing DRC + inverter LVS conformance

This is OpenADA's first pinned, reproducible real-design conformance workflow.
It checks two real layouts and one pre-extracted inverter comparison with the
`ihp-sg13g2` PDK. All three reviewed outcomes must be reproduced:

- KLayout DRC completes, returns an engineering pass, and reports zero
  violations for the inverter.
- KLayout DRC completes natively but returns an engineering failure with
  exactly eight violations for the `lvs_tester` gallery: `M1.b` × 6,
  `Cnt.d` × 1, and `Cnt.e` × 1.
- Netgen LVS completes, returns an engineering pass, and reports a unique
  circuit match with zero recognized mismatches.

The failing DRC is successful conformance evidence, not a runner error. The
native KLayout process must exit zero and produce a valid report; the semantic
result must then classify the eight markers as an engineering failure. The
runner succeeds only after the independent verifier accepts that whole chain.

The LVS operation compares the design's pinned, pre-extracted layout netlist
with its pinned schematic netlist. It does not extract that netlist from the GDS;
the DRC and LVS inputs are separate reviewed native artifacts.

The public design is fetched from
[IHP-AnalogAcademy](https://github.com/IHP-GmbH/IHP-AnalogAcademy) at commit
`133ecf657572e021b5921b5a1b7693abfb209623` under Apache-2.0. The reference
runtime is the linux/amd64 manifest of `hpretl/iic-osic-tools:2026.06`, pinned
by digest in `manifest.json`. The bundled `ihp-sg13g2` PDK is independently
bound to revision `144f811cdffda49b71d28f64e8a92b697b61cf06` through the
hash of `/foss/pdks/ihp-sg13g2/COMMIT`. Referencing these projects does not
imply an endorsement or collaboration.

No PDK or third-party design input is stored in this repository. The reviewed
semantic publication does retain generated reports, normalized results, and
bounded transcripts so every coverage claim can be checked from repository
bytes. The container is a frozen reference runtime for comparison; it is not
required by OpenADA's architecture or by native installations.

## Prerequisites

- Python 3.10 or newer
- OpenADA installed with the `conformance` extra in a virtual environment
- Git
- Docker with access to linux/amd64 images (or a compatible OCI CLI selected
  with `--container-engine`)
- Enough local storage for the pinned IIC-OSIC-TOOLS image

Run the commands shown below from the OpenADA repository root.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[conformance]'
```

## 1. Network-enabled setup

Setup is the only step that fetches remote content. It pulls the exact image
manifest and creates an external, detached design checkout under the user cache:

```bash
python3 conformance/ihp-inverter/setup.py
```

Use `--cache-dir /absolute/external/path` to select another cache. The scripts
reject a cache inside the OpenADA checkout so the design cannot accidentally be
vendored. Existing checkouts must already be clean and at the pinned revision.

## 2. Network-disabled EDA replay

The runner first verifies the local image identity, design revision, license,
and all design input hashes. It never pulls an image. All three operations also
hash the PDK revision file as a declared rules dependency. Each EDA operation
then runs in a separate container with `--network none`, a read-only root
filesystem, all capabilities dropped, and read-only mounts for both OpenADA and
the design. Only a newly created evidence directory and an isolated `/tmp`
tmpfs are writable. An outer 240-second guard also attempts named-container
cleanup for a stuck operation after the driver's 180-second timeout and reports
if cleanup cannot be confirmed.

```bash
python3 conformance/ihp-inverter/run.py \
  --evidence-dir /tmp/openada-ihp-inverter-evidence \
  --receipt-class release
```

The named evidence path must not exist and must be outside the OpenADA checkout,
conformance cache, and pinned design checkout.
If it is omitted, the runner creates a new directory under the system temporary
directory. A failure retains partial evidence for diagnosis but that directory
must never be reused for another run.

The evidence directory contains:

- `drc.json`, `inverter.drc.lyrdb`, and the bounded process transcript
  `inverter.drc.lyrdb.openada.log`
- `drc-fail.json`, `lvs-tester.drc.lyrdb`, and the bounded process transcript
  `lvs-tester.drc.lyrdb.openada.log`
- `lvs.json`, the final Netgen comparison `inverter.lvs.comp`, native structured
  output `inverter.lvs.json`, and complete-or-unknown process transcript
  `inverter.lvs.comp.openada.log`
- `run.json`, which records the conformance-manifest hash, exact local image
  identity, design revision, and tracked plus untracked OpenADA checkout state
  before and after execution. `commit_exact` is true only when both observed
  states are clean and unchanged.

## 3. Independent verification

Replay verification without invoking an EDA:

```bash
python3 conformance/ihp-inverter/verify.py \
  /tmp/openada-ihp-inverter-evidence
```

The verifier checks all three JSON documents against strict schemas, binds the
exact runtime tool identities and reviewed argv shape, and checks exact
declared input and native artifact hashes. It then independently requires the
LYRDB's native child generator, top cell, nonempty category catalog, cells/items
sections, item category/cell bindings, positive multiplicities, waiver tags,
and geometry values. The clean report must have zero weighted markers. The
failing report must have exactly eight unwaived items in the reviewed order and
cell distribution, with eight retained edge-pair geometries and 32 coordinate
pairs. Those independently parsed facts are cross-checked against the normalized
JSON result. The verifier also validates both bounded KLayout transcripts and
independently parses and cross-checks Netgen's final report, native JSON,
complete transcript, setup-read status, and declared PDK provenance. Generated
artifact hashes are deliberately not frozen in the manifest.

For the pinned PDK, Netgen may emit native `Unable to permute model ... pins
...` warning lines on stderr while still producing a valid match. The driver and
independent verifier accept only that exact bounded warning grammar, record its
line count and warning diagnostic, and reject any other stderr as untrusted.

Validate only the static manifest (no Docker, network, PDK, or design checkout):

```bash
python3 conformance/ihp-inverter/verify.py --manifest-only
```

The default unit tests exercise the manifest and verifier with schema-valid
synthetic results and do not use Docker. After running setup, opt in to the real
network-disabled replay test with the development dependencies installed:

```bash
python -m pip install -e '.[dev]'
OPENADA_RUN_IHP_CONFORMANCE=1 \
  python3 -m pytest -m conformance tests/test_conformance.py
```

Set `OPENADA_IHP_CACHE_DIR` or `OPENADA_CONTAINER_ENGINE` for non-default
locations. The real test invokes only `run.py`; it never performs setup or a
pull.

## 4. Agent-facing semantic publication

After a fresh replay passes `verify.py`, publish the complete, content-addressed
evidence chain with:

```bash
python3 conformance/ihp-inverter/semantic.py \
  --publish \
  --native-evidence /tmp/openada-ihp-inverter-evidence
```

Release publication requires the source-attested clean replay produced by the
command above and writes the fixed repository paths plus
`semantic-chain-run.json`. Publication first reruns the independent verifier.
It then executes four
adversarial checks: the real gallery DRC failure, an explicitly labeled
synthetic native-LVS mismatch injection, a seven-item LYRDB whose normalized
count and digest were reconciled, and an unbound Netgen JSON byte. Every
negative/tamper verdict is retained separately with its exact observed
diagnostic. The bundle keeps the clean and failing LYRDB databases, authoritative
Netgen comparison and native JSON, all normalized results, bounded transcripts,
and replay metadata as distinct files.

`semantic-evidence.json` gives an agent two scope-specific decisions. The clean
inverter may proceed to the next engineering stage because DRC is clean and LVS
matches uniquely; this is not tapeout signoff. The separate gallery fixture is
blocked with all eight exact rule classes and edge-pair geometries attached.
The document also retains provenance and tool limitations. No IEEE measurement
standard is claimed for these geometry and structural-equivalence assertions;
the pinned IHP DRC deck and Netgen setup are the governing sources.

Verify the repository-local publication without Docker or the external design:

```bash
python3 conformance/ihp-inverter/semantic.py
```

For direct script use with a non-default cache or container CLI, pass the same
`--cache-dir` or `--container-engine` option to setup and run. The verifier only
needs the manifest and retained evidence.
