# IHP analog transfer and spectral semantic chain

This bundle closes 47 exact semantic rows from public IHP schematics to an
agent-facing engineering decision. Thirty rows are the complete
`result.transfer.measure/v1alpha1` and `result.spectral.measure/v1alpha1`
surfaces. The other 17 are the Xschem, external-provider, and Spice3-series
prerequisites that this workflow actually invokes; they are included rather
than mislabeling native acquisition as a measurement kernel.

The public design is pinned to IHP AnalogAcademy commit
`133ecf657572e021b5921b5a1b7693abfb209623`. Native execution uses the pinned
linux/amd64 IIC-OSIC-TOOLS image at digest
`sha256:fd38cb07a29d49d5f9720494cc4497cd8e8c80dfa06b4224d46447bc0f3c2ef0`,
Xschem 3.4.8RC, ngspice 46, and the explicit
`org.openada.driver.ngspice-pdk-control` 0.5.0 provider. Every EDA execution is
network-disabled.

## Closed real-design workflows

The OTA path starts from
`modules/module_1_bandgap_reference/part_1_OTA/testbenches/ota_testbench.sch`.
Its exact public bytes, OTA schematic, and symbol are pinned. A deterministic
source transform replaces the demonstration's two exploratory control blocks
with the provider's closed `save all` / AC / `write` grammar. Xschem then
materializes the real hierarchical deck. The provider runs `ac dec 100 1
10e6`, retaining its request, generated deck, native raw, log, launcher, and
normalized protocol. `openada extract` projects the Cartesian components of
`v(vout)` and `v(vp)`, and four actual `openada transfer` commands produce:

- Low-frequency gain: 70.11974138671727 dB at 1 Hz.
- −3 dB bandwidth: 1332.3043851822767 Hz.
- Unity-gain frequency: 4157414.5570058133 Hz.
- Negative-feedback phase-margin convention: 61.42309151169668 degrees.

The inverter path starts from the public module-0 inverter testbench,
schematic, and symbol. Its deterministic execution derivative preserves the
DUT, makes the source a coherent 500 kHz periodic drive, and requests 1024
linearized samples from 0.5 us through 32.46875 us. The provider retains the
exact native plot name `Transient Analysis (linearized)`. `openada extract`
selects `v(vout)`, and four actual `openada spectral` commands with harmonic
orders 2 through 31 produce:

- SNR: 93.28584204310002 dB.
- SINAD: 6.327655053654634 dB.
- THD: −6.327655062403778 dB.
- SFDR: 9.598544308390958 dB.

The retained endpoints imply a floating Nyquist frequency of
15999999.999999814 Hz. The request therefore uses the explicit conservative
upper band edge 15,999,999 Hz rather than a rounded 16 MHz value that would be
outside the closed band. The 500 kHz fundamental is exactly bin 16; the
independent oracle confirms the 30 declared harmonic bins, 480 noise bins,
510 residual bins, and bin 48 as the winning spur.

## Independent evidence and failure boundaries

`verify.py` imports no OpenADA implementation. It independently reparses both
binary Spice3 raw files, binds provider protocol records to native bytes,
checks both extracted series against native values, and reimplements:

- complex output-over-input gain, deterministic phase unwrapping, and
  log-frequency falling-crossing interpolation; and
- a direct O(N²) DFT, coherent-bin checks, harmonic partition, and all four
  spectral ratios (intentionally unlike OpenADA's radix-2 implementation).

Thirteen real public-CLI negatives close the missing-symbol, AC/TRAN request
binding, AC/TRAN selector, four transfer, and four spectral boundaries. Five
fresh tamper probes must reject a changed native raw byte, normalized metric,
standards context, public-source identity, and engineering decision. The
semantic run indexes every trust artifact with a unique repository path,
SHA-256, DAG source step/output, or replay ID.

## Engineering and standards decision

The OTA result is one nominal 27 °C `mos_tt` open-loop response. The public
source contains no numeric project specification, and this chain performs no
PVT, Monte Carlo, or mismatch analysis. The correct agent decision is
**proceed to requirements and PVT/statistical review**—not “design passed” and
not signoff.

The inverter is a square-wave logic stage, so large harmonic content is
expected. Its generic SNR, SINAD, THD, and SFDR values are not ADC or DAC
quality figures.

The public lifecycle/scope pages for
[IEEE 1241-2023](https://standards.ieee.org/ieee/1241/6797/) (ADCs),
[IEEE 1658-2023](https://standards.ieee.org/ieee/1658/7350/) (DACs),
[IEEE 1057-2017](https://standards.ieee.org/ieee/1057/5945/) (digitizing
waveform recorders), and
[IEEE 181-2025](https://standards.ieee.org/ieee/181/10551/) (transitions and
pulses) are bound in the request and evidence. None applies as a converter or
recorder conformance method for this inverter, and the OTA metrics are not an
IEEE 181 transition test. No licensed clause-level review was performed and no
standards-conformance claim is made. The inverter uses only the versioned,
generic OpenADA spectral definition.

## Replay

Setup is the only network-enabled phase:

```bash
python3 conformance/ihp-analog-measurements/setup.py
```

Run a fresh network-disabled replay into a new external directory:

```bash
python3 conformance/ihp-analog-measurements/run.py \
  --evidence-dir /tmp/openada-ihp-analog-measurements
```

Independently replay all five mutations:

```bash
python3 conformance/ihp-analog-measurements/verify.py \
  /tmp/openada-ihp-analog-measurements \
  --run-tamper-probes
```

Publication never edits the shared semantic index. During development,
`--publish --receipt-class provisional` may retain an explicitly provisional
bundle that release mode rejects. A release publication requires an unchanged
clean checkout before and after the complete native replay:

```bash
python3 conformance/ihp-analog-measurements/run.py \
  --evidence-dir /tmp/openada-ihp-analog-measurements-release \
  --publish --receipt-class release
```

The mechanical release-index publisher content-addresses the resulting
`semantic-chain-run.json`; never hand-copy a provisional receipt into the
index.

Run focused static checks with:

```bash
python3 -m pytest -q \
  conformance/ihp-analog-measurements/test_measurement_chain.py \
  -k 'not real_replay'
```

After setup, opt in to another full replay:

```bash
OPENADA_RUN_IHP_ANALOG_MEASUREMENTS=1 \
python3 -m pytest -q -m conformance \
  conformance/ihp-analog-measurements/test_measurement_chain.py
```

Set `OPENADA_IHP_ANALOG_MEASUREMENTS_CACHE_DIR` or
`OPENADA_CONTAINER_ENGINE` for non-default local environments.
