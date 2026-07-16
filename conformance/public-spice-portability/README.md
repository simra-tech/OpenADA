# Public SPICE portability semantic chain

This bundle closes 29 active semantic rows through real ngspice and Xyce
execution on pinned public designs. It proves that the shared
`circuit.simulate/v1alpha2` and `result.series.extract/v1alpha1` contracts can
carry analysis-specific native evidence across both backends; it does not
claim that unlike circuits should produce equal numbers.

## Public sources and native matrix

The Xyce cases come from `Xyce_Regression` release 7.10.0 at commit
`d6e278e371ec2f3df1325dcff4552e585bc7ecc1`. The retained inputs are its
public DC, AC RC, and transient decks. The ngspice cases come from IHP
AnalogAcademy commit `133ecf657572e021b5921b5a1b7693abfb209623`: the real
SG13G2 inverter supplies OP and DC evidence, and the two-stage OTA supplies AC
evidence. Both source trees, their licenses, exact Git trees, and every
consumed file digest are checked before and after replay.

The network-disabled native matrix is:

| Backend | Analyses | Public circuit | Independently checked evidence |
|---|---|---|---|
| ngspice 46 | OP, DC | IHP inverter | Spice3 raw identity, axis, vectors, finite samples, and extracted `vin`/`vout` series |
| ngspice 46 | AC | IHP two-stage OTA | Complex Spice3 raw values and extracted real/imaginary `vout` series |
| Xyce 7.10 | DC | Xyce regression DC deck | Xyce raw identity, sweep facts, values, and extracted voltage series |
| Xyce 7.10 | AC | Xyce regression RC deck | Derived-deck provenance, complex Xyce raw values, and extracted Cartesian series |
| Xyce 7.10 | transient | Xyce regression transient deck | Time axis, finite native samples, and extracted voltage series |

The chain also runs the legacy ngspice CLI variant and the non-EDA
`capabilities`, scoped `doctor`, profile list/show, and provider list/validate
surfaces. Every row is tied to its positive semantic command. Six negative
replays and nine byte/identity/lineage tamper replays close the refusal
boundaries; none substitutes for a positive run.

Native execution uses the pinned linux/amd64 IIC-OSIC-TOOLS image at digest
`sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0`
with Xschem 3.4.8RC, ngspice 46, Xyce 7.10, and the exact IHP PDK/model/OSDI
closure declared in `manifest.json`. The container has no network, a read-only
root and source mounts, dropped capabilities, and only an isolated evidence
directory writable.

## Reproduce and verify

Setup is the only network-enabled phase:

```bash
python3 conformance/public-spice-portability/setup.py
```

Run a source-frozen replay into a fresh external directory:

```bash
python3 conformance/public-spice-portability/run.py \
  --evidence-dir /tmp/openada-public-spice-portability
```

The independent verifier imports no OpenADA implementation. It reparses the
native raw formats, reconstructs derived-deck provenance, checks every
normalized series against native values, verifies the administrative results,
and reruns all tamper mutations:

```bash
python3 conformance/public-spice-portability/verify.py \
  /tmp/openada-public-spice-portability \
  --run-tamper-probes
```

Only an unchanged clean OpenADA checkout can create a release receipt.
`--allow-dirty` creates an explicitly provisional external replay and cannot
be combined with `--publish`. After a source freeze, publish a fully verified
release bundle with:

```bash
python3 conformance/public-spice-portability/run.py \
  --evidence-dir /tmp/openada-public-spice-portability-release \
  --publish
```

Run the focused static contract and coverage checks with:

```bash
python3 -m pytest -q \
  conformance/public-spice-portability/test_portability_chain.py \
  -k 'not real_replay'
```

After setup, opt into another full native replay with
`OPENADA_RUN_PUBLIC_SPICE_PORTABILITY=1`. Set
`OPENADA_PUBLIC_SPICE_PORTABILITY_CACHE_DIR` or `OPENADA_CONTAINER_ENGINE` for
non-default local environments.

## Agent decision and standards boundary

`evidence/agent-evidence.json` is the agent-facing result. It reports the
backend/analysis matrix, exact result and artifact lineage, administrative
capability facts, negative outcomes, source derivations, and limitations. A
passing receipt supports using the implemented shared contract for these exact
analysis/format combinations and routing unsupported combinations explicitly.
It does not establish backend numerical equivalence, PDK qualification,
performance specifications, silicon correlation, or signoff.

This chain validates analysis evidence and series extraction; it computes no
SNR, SINAD, THD, SFDR, transition, jitter, or converter metric. No IEEE
measurement standard is therefore asserted. Standards-qualified measurements
belong in a separate chain whose object under test, method, conditions, and
normative scope are explicit.
