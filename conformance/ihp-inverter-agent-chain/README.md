# IHP inverter semantic-to-agent conformance chain

This bundle closes one real analog workflow from an agent-facing intent to
native EDA bytes, normalized evidence, measurements, specification decisions,
and a bounded agent recommendation. It uses the public IHP AnalogAcademy
inverter instead of a synthetic fixture.

The passing chain exercises:

- Xschem netlisting, including a real missing-symbol negative replay.
- Transient simulation through the external
  `org.openada.driver.ngspice-pdk-control` 0.5.0 provider and the built-in
  `circuit.simulate` ngspice backend.
- A deliberately isolated terminal nonconvergence on both simulation paths,
  retaining the partial raw file and native log.
- Spice3 transient-series extraction for `v(vin)` and `v(vout)`.
- Nine measurement kinds: sample-at, minimum, maximum, arithmetic mean,
  arithmetic RMS, crossing, rise time, fall time, and settling time.
- Eighteen closed-bound specification decisions, one condition-binding
  rejection, 15 negative replays, and 19 byte/lineage/value tamper replays.
- An independently recomputed agent evidence document that preserves the
  design conditions, native lineage, decision margins, failures, and known
  limitations.

The manifest currently covers 57 exact semantic-surface rows. Each row is
bound to the positive semantic command that exercised it; negative and tamper
replays are additional evidence and cannot substitute for positive execution.

## Reproducibility and trust boundary

The source design is pinned to IHP-AnalogAcademy commit
`133ecf657572e021b5921b5a1b7693abfb209623` and its relevant source files are
hashed in `manifest.json`. The linux/amd64 IIC-OSIC-TOOLS runtime, IHP PDK
revision, Xschem and ngspice executables, ngspice startup files, three-file
`mos_tt` model closure, and PSP103 OSDI module are also pinned.

Setup is the only network-enabled phase. The replay uses `--network none`, a
read-only container root, dropped capabilities, read-only source mounts, an
isolated home directory, and a new external evidence directory. It verifies
that both the OpenADA checkout and pinned design checkout remain clean and
unchanged. A dirty OpenADA checkout intentionally prevents a passing receipt.

The independent oracle in `verify.py` imports no `openada` implementation
module. It reparses Spice3 raw bytes and reimplements extraction, all nine
measurement algorithms, condition binding, and bound evaluation. It also
reconstructs the deterministic shared-profile decks from the retained model
closure, checks equality of the relevant built-in and external-provider
waveforms, and reruns every tamper mutation before accepting retained receipts.

## Setup and replay

Run from the repository root with Python 3.10 or newer, Git, Docker (or a
compatible OCI CLI), and OpenADA's conformance dependencies installed:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[conformance]'
python3 conformance/ihp-inverter-agent-chain/setup.py
```

Setup pulls the exact image digest and creates a detached design checkout in
the external conformance cache. Once it succeeds, replay is network-disabled:

```bash
python3 conformance/ihp-inverter-agent-chain/run.py \
  --evidence-dir /tmp/openada-ihp-inverter-agent-evidence
```

The evidence directory must not already exist and must be outside the OpenADA
checkout, cache, and design checkout. A failure leaves the incomplete directory
for diagnosis; never reuse it for another receipt.

Independently verify a completed replay, including fresh execution of all 19
tamper probes:

```bash
python3 conformance/ihp-inverter-agent-chain/verify.py \
  /tmp/openada-ihp-inverter-agent-evidence \
  --run-tamper-probes
```

`run.py --publish` is reserved for a clean source freeze. It copies a fully
verified receipt to this bundle's `evidence/` directory; it does not bypass any
replay or checkout check.

Publish a release receipt from a clean checkout into a fresh external evidence
directory with:

```bash
python3 conformance/ihp-inverter-agent-chain/run.py \
  --evidence-dir /tmp/openada-ihp-inverter-agent-release \
  --publish
```

Use `--cache-dir` and `--container-engine` consistently for non-default cache
locations or OCI clients.

## Agent-facing result

`agent-evidence.json` is the compact decision input. It binds the exact native
raw digest, normalized-series digest, nine measurement records, 18
specification outcomes, negative cases, and limitations. The accompanying
`chain-run.json` indexes exactly 40 distinct trust artifacts: one request
contract, native artifact, independent oracle, normalized result, downstream
decision, and agent result; one artifact for each negative replay; and one
receipt for each tamper replay. Repository paths and trust digests must be
unique.

A passing result means only that this pinned transient workflow reproduced and
its stated limits were met. It is not PDK qualification, model validation,
reliability analysis, or tapeout signoff.

## Measurement standards and scope

[IEEE 181-2025](https://standards.ieee.org/ieee/181/10551/) defines terminology
and algorithms for transitions, pulses, and related two-state waveforms. It is
the relevant standards target for a future standards-qualified rise/fall and
settling workflow. This chain does **not** claim IEEE 181 conformance: its
contract uses caller-supplied levels, directions, windows, tolerances, and
piecewise-linear interpolation, and it does not implement the full standard's
state-level and waveform-analysis procedure.

[IEEE 1241-2023](https://standards.ieee.org/ieee/1241/6797/) covers terminology
and test methods for sampled, quantized analog-to-digital converters. It is an
appropriate target for a future ADC/SNR spectral chain, not for this continuous
analog inverter transient. No SNR, SINAD, ENOB, noise, FFT, or ADC-standard
claim is made here.

Mean and RMS in this version are arithmetic operations over the retained
adaptive ngspice sample values; they are not time-weighted integrals. Crossing,
rise/fall, and settling semantics are defined completely by the versioned
OpenADA request and algorithm identifiers retained in the evidence. These
definitions stay explicit so an agent cannot silently substitute a textbook,
instrument, or standards-specific convention.

The exercised EDA slice is transient-only at `mos_tt`, 1.2 V. AC, DC, OP,
noise, Monte Carlo, mismatch, temperature/supply sweeps, extracted parasitics,
DRC/LVS, and reliability are outside this receipt and require separate real
design chains.

## Focused tests

Run the static bundle and coverage-model checks explicitly:

```bash
python3 -m pytest -q \
  conformance/ihp-inverter-agent-chain/test_agent_chain.py \
  -k 'not real_replay'
```

After setup, opt in to a fresh network-disabled real replay:

```bash
OPENADA_RUN_IHP_AGENT_CHAIN=1 \
  python3 -m pytest -q -m conformance \
  conformance/ihp-inverter-agent-chain/test_agent_chain.py
```

Set `OPENADA_IHP_AGENT_CHAIN_CACHE_DIR` or `OPENADA_CONTAINER_ENGINE` when
needed. The opt-in test never performs setup or pulls an image.
