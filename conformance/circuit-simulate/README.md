# `circuit.simulate` native portability proof

`fixtures/rc-transient.cir` is the first same-intent proof fixture. It uses
only ideal sources, a resistor, and a capacitor, so both ngspice and Xyce can
run the same authoritative deck without a PDK or compact-model dependency.

The fixture contains exactly one transient analysis and deliberately contains
no `.include`, `.lib`, `.control`, `.measure`, or `.print` directive. The
driver owns fresh native-result selection and evidence capture.

The proof is intentionally narrow: it establishes fresh, structurally valid
transient analysis evidence. It does not establish model fidelity, circuit
requirements, or signoff suitability.

## Pinned runtime

The native replay uses IIC-OSIC-TOOLS `2026.06` on linux/amd64:

- manifest digest:
  `sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0`;
- config digest:
  `sha256:28573cfe204f66872c2aa7eb050692f7788ff0104eb733d950b790c72c253ceb`;
- ngspice: `** ngspice-46 : Circuit level simulation program`;
- Xyce: `Xyce Release 7.10-opensource`.

The image must already exist locally. The runner uses `--pull=never`, disables
networking for both EDA invocations, makes the container root and OpenADA mount
read-only, drops capabilities, and writes only to a fresh evidence directory.
If the image is absent, fetch it as a separate setup action before the replay:

```bash
docker pull \
  hpretl/iic-osic-tools@sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0
```

## Run and verify

```bash
python3 conformance/circuit-simulate/run.py \
  --evidence-dir /tmp/openada-circuit-simulate-evidence
python3 conformance/circuit-simulate/verify.py \
  /tmp/openada-circuit-simulate-evidence
```

The runner invokes the same operation/profile shape twice. It retains each
normalized result, native log, and native raw file, then calls a separate
verifier. The verifier:

1. validates the pinned fixture, image identity, container policy, and public
   result schema;
2. checks exact driver/tool/profile identity and fresh artifact hashes;
3. parses ngspice binary raw and Xyce ASCII raw without using the driver parser;
4. checks finite monotonic time, the 1 kOhm branch relation, and the expected RC
   midpoint/final response;
5. compares backend results within declared semantic tolerances while allowing
   different adaptive point counts.

On the pinned runtime, ngspice produces 68 points and Xyce produces 43. Those
native differences are expected. Both establish the same
`simulation.evidence.valid` assertion.

Use a new evidence path for every run; the runner never deletes or accepts an
existing destination. The ordinary unit suite validates the manifest and
independent parsers without Docker. To opt into the pinned native pytest replay:

```bash
OPENADA_RUN_CIRCUIT_SIMULATE_CONFORMANCE=1 \
  PYTHONPATH=src pytest -q tests/test_circuit_simulate_conformance.py
```
