# Public-IHP ngspice provider analysis chain

This chain closes the repository-shipped ngspice PDK-control provider across
all four advertised analyses. It starts from the pinned public
[`IHP-AnalogAcademy`](https://github.com/IHP-GmbH/IHP-AnalogAcademy) revision
`133ecf657572e021b5921b5a1b7693abfb209623`, netlists real inverter and
two-stage OTA schematics with Xschem, and invokes the public provider boundary
against ngspice 46 and the IHP SG13G2 PDK in a network-disabled pinned
IIC-OSIC-TOOLS image.

The positive replay retains separate decks, requests, provider results,
ngspice logs, generated launchers, and native raw files for:

- inverter operating point: 1 point;
- inverter `V1` DC transfer sweep: 121 points from 0 V through 1.2 V;
- OTA complex AC sweep: 701 points from 1 Hz through 10 MHz; and
- uniformly linearized inverter transient: 1024 points from 0.5 us through
  32.46875 us.

`oracle.py` contains a second raw parser that imports no OpenADA modules. It
reconstructs the four native records, rechecks provider/request/artifact
binding, calculates scoped OTA gain/crossover evidence and inverter waveform
facts, and emits decisions with explicit limitations. The agent evidence does
not turn this nominal demonstration into signoff: no PVT, Monte Carlo,
mismatch, extracted parasitics, or specification limits are implied.

The chain records an explicit standards applicability decision. IEEE
1241-2023, IEEE 1658-2023, and IEEE 1057-2017 govern converter or waveform-
recorder measurements that this analog provider replay does not perform; IEEE
181-2025 is not invoked because no transition-time metric is computed. The
reported bias, transfer, gain, crossover, and waveform facts therefore make no
IEEE conformance claim.

The negative boundary includes four pre-launch request/deck rejections and one
real ngspice launch against an unresolved subcircuit. Separate tamper replays
exercise raw-byte, request-feature, result-digest, raw-header, and provider
version substitutions. Contract tests also exercise hostile ambient transport
and child-environment isolation.

Run a fresh replay with a clean pinned checkout already available:

```bash
.venv/bin/python conformance/ihp-ngspice-provider-analyses/replay.py \
  --design-dir ~/.cache/openada/conformance/ihp-inverter/IHP-AnalogAcademy \
  --evidence-dir /tmp/openada-ihp-provider-analyses \
  --publish --receipt-class release
```

The runner checks the exact design revision and file hashes, uses
`--network none`, validates the independent oracle, runs focused contract
tests, and republishes content-addressed evidence. Release publication requires
an unchanged clean OpenADA checkout. A development replay may use
`--receipt-class provisional`, but the release index rejects it. The provider
manifest's conformance digest must equal the exact final run JSON digest
registered by the mechanical semantic-index publisher.
