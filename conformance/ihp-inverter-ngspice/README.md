# IHP inverter Xschem + ngspice conformance

This bundle replays a real IHP inverter from its native Xschem schematic through
ngspice and proves OpenADA's explicit deck-owned output contract. The testbench
contains a native `.control` block which writes `test_inverter.raw`; OpenADA is
told about that exact relative path with
`--expect-output raw=test_inverter.raw`. It does not scan the working directory
or infer outputs after execution.

The public design is
[IHP-AnalogAcademy](https://github.com/IHP-GmbH/IHP-AnalogAcademy) at commit
`133ecf657572e021b5921b5a1b7693abfb209623` under Apache-2.0. The reference
runtime is the linux/amd64 manifest of `hpretl/iic-osic-tools:2026.06`, with
both its manifest and image-config identities pinned by digest. The bundled
`ihp-sg13g2` PDK is pinned at commit
`144f811cdffda49b71d28f64e8a92b697b61cf06`; the manifest also pins the PDK
`COMMIT` file, Xschem rcfile, PDK ngspice init, and ngspice system `spinit`
hashes.

Referencing these projects does not imply an endorsement or collaboration. No
PDK, design file, generated netlist, waveform, or run evidence is stored in
OpenADA. The container is a frozen comparison profile, not a requirement of
OpenADA's architecture.

## Prerequisites

- Python 3.10 or newer
- OpenADA installed with its `conformance` extra
- Git
- Docker, or a compatible OCI CLI selected with `--container-engine`
- enough local storage for the pinned IIC-OSIC-TOOLS image

From the OpenADA checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[conformance]'
```

## 1. Fetch the pinned external inputs

Setup is the only network-enabled step. It pulls the exact image manifest and
creates a detached design checkout outside the repository:

```bash
python3 conformance/ihp-inverter-ngspice/setup.py
```

The default cache is shared with `conformance/ihp-inverter`, because both
bundles use the same exact design revision and image. Use
`--cache-dir /absolute/external/path` to select another location. An existing
checkout must be clean and at the pinned revision.

## 2. Run the network-disabled replay

Choose a path which does not exist:

```bash
python3 conformance/ihp-inverter-ngspice/run.py \
  --evidence-dir /tmp/openada-ihp-ngspice-evidence
```

The runner verifies the local image and design before launch. It then uses one
container with:

- no network;
- a read-only root filesystem;
- all capabilities dropped and no privilege escalation;
- read-only OpenADA and design mounts;
- an isolated `/tmp` tmpfs; and
- only the new evidence mount writable.

Inside that boundary OpenADA first netlists the read-only schematic into
`/evidence/work/inverter_tb.spice`, using the explicit pinned Xschem rcfile. It
then runs ngspice in `control` mode from `/evidence/work`. It passes the pinned
system `spinit` through `--system-init-file`, which makes OpenADA set an exact
`SPICE_SCRIPTS` directory and hash that configuration input. The native `-n`
option disables local and user `.spiceinit` files. The generated control
launcher embeds validated paths and sources the explicit pinned PDK
`.spiceinit` followed by the generated deck. OpenADA requires the deck-owned
`test_inverter.raw` before it may report an engineering pass. The PDK itself
remains on the image's read-only filesystem.

Successful evidence contains:

```text
run.json
netlist.json
simulate.json
simulation/inverter_tb.log
simulation/inverter_tb.openada-control.sp
work/inverter_tb.spice
work/test_inverter.raw
```

`run.json` records the complete OCI argv, exact image identity, observed PDK
commit, PDK configuration, and ngspice system-init hashes, exact inner OpenADA
invocations, and the tracked plus untracked checkout state before and after execution. A failed run
retains partial evidence for diagnosis; never reuse that directory.

## 3. Verify retained evidence offline

Verification does not invoke Docker, Xschem, ngspice, Git, or the network:

```bash
python3 conformance/ihp-inverter-ngspice/verify.py \
  /tmp/openada-ihp-ngspice-evidence
```

The verifier applies the strict OpenADA result and run schemas, checks all
recorded input and artifact hashes, binds exact tool and command identities,
and checks the container policy and PDK/init observation. It then parses the
Spice3f5 binary raw file independently of OpenADA. The raw hash is deliberately
not frozen because ngspice embeds run-specific header data. Instead the
verifier requires:

- one real, binary transient plot with 80 or 81 version-dependent points;
- finite values and strictly increasing time from 0 through 2 microseconds;
- `time`, `v(vdd)`, `v(vin)`, and `v(vout)` vectors;
- VDD between 1.19 V and 1.21 V; and
- settled high/low/high inverter behavior in three windows away from the input
  edges.

Validate only the pinned static manifest with:

```bash
python3 conformance/ihp-inverter-ngspice/verify.py --manifest-only
```

The default unit suite uses a small synthetic binary raw file and remains fully
offline:

```bash
python3 -m pytest -q tests/test_ihp_ngspice_conformance.py
```
