# Implemented intent routing

Route an engineering question to the smallest implemented semantic primitive.
This table is a capability boundary, not a claim that every plausible analog
metric is already supported.

| Engineering question | Required evidence chain | Implemented boundary |
|---|---|---|
| Bias, node voltage, branch current at one operating point | OP simulation → exact series extraction → `sample_at`, minimum, maximum, or mean as appropriate | ngspice OP only; exact native voltage/current selectors |
| Static transfer or line/load sweep extrema and mean | DC simulation → series extraction → scalar measurement | Explicit typed single-source sweep; no arbitrary nested sweep |
| Threshold location or crossing | DC or transient simulation → series extraction → `crossing` | Exact threshold unit and directed occurrence; adjacent-point linear interpolation |
| Rise or fall time | Transient simulation → series extraction → `rise_time` or `fall_time` | Caller supplies absolute lower/upper thresholds and occurrence |
| Settling time | Transient simulation → series extraction → `settling_time` | Caller supplies target, tolerance, reference, hold duration, and optional window |
| Minimum, maximum, arithmetic mean, or RMS | Any compatible real series → `result.measure` | Retained samples, optional closed window, exact units |
| Coherent single-tone SNR, SINAD, THD, or SFDR | Transient simulation → series extraction → `result.spectral.measure` | Uniform power-of-two coherent record and fixed rectangular partition only |
| AC output-over-input trace and first-frequency gain | AC simulation → four Cartesian series → `result.transfer.measure` | Same-unit input/output phasors; first positive simulated frequency is explicitly not DC |
| −3 dB bandwidth or unity-gain frequency | AC transfer trace → `result.transfer.measure` | Exactly one falling crossing; linear-value interpolation over log10 frequency |
| Negative-feedback phase margin | Reviewed loop-gain testbench → AC transfer trace → `result.transfer.measure` | Explicit negative-feedback interpretation; 180° plus unwrapped phase at the unique falling unity crossing |
| Explicit scalar limit | Any measured scalar → `specification.evaluate` | Exact units and exact condition bindings; lower and/or upper bound |
| Valid OP/DC/AC/TRAN native analysis evidence | `circuit.simulate/v1alpha2` | ngspice OP/DC/AC/TRAN; Xyce DC/AC/TRAN |
| Explicit manifest-declared circuit-simulation provider execution | Explicit manifest + complete request → `provider invoke` | `circuit.simulate/v1alpha2` only; exact selector, canonical filesystem inputs/fresh destination, local JSON-stdio, wait-only; conformance metadata is self-declared; no discovery or MCP |

## Questions that remain planned

Do not force these into the closest implemented primitive:

- true zero-frequency DC gain, poles/zeros, gain margin, phase-crossing search,
  multiple-crossing selection, smoothing, fitting, or de-embedding;
- device-region or headroom interpretation not represented as selected finite
  voltage/current series;
- noise analysis or integrated noise;
- arbitrary signal expressions, differential combination, resampling, or unit
  conversion;
- noncoherent/windowed spectra, HD2/HD3 as individual outputs, SNDR alias,
  ENOB, jitter, or phase noise;
- nested sweeps, corners, Monte Carlo, statistical yield, or campaign
  aggregation; and
- PSS, PAC, periodic noise, S-parameters, electromagnetic, thermal,
  reliability, aging, or signoff assertions.

For a planned question, retain the characterization row as `not evaluated —
capability unavailable`. Name the missing operation in engineering terms and
state its required source, parameters, units, output fact, and uncertainty
boundary. Do not drop to simulator syntax and relabel the result as portable
OpenADA evidence.

## Execution discipline

For each row, freeze the testbench, model/PDK identity, mode, supplies,
stimulus, load, corner, temperature, source signals, metric definition, and
limit before dispatch. Execute only the next unproven layer. Preserve every
request ID and native/canonical digest. If a choice changes, append a row
revision; do not overwrite the evidence ledger.
