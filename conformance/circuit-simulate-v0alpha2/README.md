# `circuit.simulate` native portability proof

The v0alpha2 bundle proves four closed, model-free analysis capabilities with
ideal sources, resistors, and one capacitor. Every fixture contains exactly one
top-level analysis and no `.include`, `.lib`, `.control`, `.measure`, `.print`,
or `.step` directive. The driver owns fresh native-result selection and
evidence capture.

| Capability | Fixture | ngspice | Xyce |
| --- | --- | --- | --- |
| Operating point | `resistor-divider-op.cir` | 1-point native raw | Not advertised: standalone Xyce OP has no complete `-r` raw result |
| DC sweep | `resistor-divider-dc.cir` | 5-point native raw | 5-point native raw |
| AC sweep | `rc-ac.cir` | 16-point complex native raw | 16-point complex native raw |
| Transient | `rc-transient.cir` | 68-point native raw | 43-point native raw |

The proof is intentionally narrow. It establishes request-bound, fresh,
structurally valid native evidence and checks the fixture's simple electrical
relations independently. It does not establish model fidelity, circuit
requirements, PDK correctness, arbitrary-deck portability, or signoff
suitability.

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
python3 conformance/circuit-simulate-v0alpha2/run.py \
  --evidence-dir /tmp/openada-circuit-simulate-evidence
python3 conformance/circuit-simulate-v0alpha2/verify.py \
  /tmp/openada-circuit-simulate-evidence
```

The runner invokes every backend advertised by each capability case. It retains
each normalized result, native log, and native raw file, then calls a separate
verifier. The verifier:

1. validates the pinned fixture, exact `circuit.simulate/v1alpha2` profile
   digest, image identity, container policy, and public result schema;
2. checks exact driver/tool/profile identity and fresh artifact hashes;
3. parses real and complex ngspice binary raw and Xyce ASCII raw without using
   the driver parser;
4. proves OP divider bias, every declared DC grid point and divider relation,
   each backend's exact LIN/DEC/OCT AC grid and complex RC transfer, and
   transient time/branch/response behavior;
5. compares transient backend results within declared semantic tolerances while
   allowing different adaptive point counts.

On the pinned runtime, ngspice produces 68 points and Xyce produces 43. Those
native differences are expected. Both establish the same
`simulation.evidence.valid` assertion.

## Compatibility

The expanded manifest, run record, and verification record use the v0alpha2
schema identifiers and conformance identity
`model-free-op-dc-ac-tran-ngspice-xyce-v0alpha2`. Historical v0alpha1 evidence
under `model-free-rc-transient-ngspice-xyce` remains a transient-only proof; its
meaning is unchanged, and the v0alpha2 verifier does not relabel or accept it as
evidence for OP, DC, or AC capability.

Use a new evidence path for every run; the runner never deletes or accepts an
existing destination. The ordinary unit suite validates the manifest and
independent parsers without Docker. To opt into the pinned native pytest replay:

```bash
OPENADA_RUN_CIRCUIT_SIMULATE_CONFORMANCE=1 \
  PYTHONPATH=src pytest -q tests/test_circuit_simulate_conformance.py
```
