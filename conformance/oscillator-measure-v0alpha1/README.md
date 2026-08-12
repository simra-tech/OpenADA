# Oscillator measurement primitives conformance v0alpha1

This model-free bundle exercises the versioned
`openada.operation/result.osc.measure/v1alpha1` contract implemented by
`org.openada.kernel.oscillator-evidence` 1.0.0. It covers all seven advertised
features without a simulator, network access, random input, or inherited
waveform state.

The fixture uses closed analytic waveform definitions. The runner materializes
new finite transient series and invokes the implementation in this checkout.
The independent verifier does **not** import OpenADA: it securely reads bounded
JSON, rejects duplicate keys and non-finite constants, validates the base result
and operation-specific schemas, checks the manifest/profile/fixture digests,
regenerates every sample, and recomputes crossing, startup, shared-window,
tuning-grid, span, and shift facts.

Coverage includes:

- the 2.4168 GHz reference at `vctrl = 0.6 V`, using 100 cycles in the declared
  250–300 ns late crop;
- a clean sustained waveform, decaying startup ringing, no startup,
  started-then-collapsed behavior, and deterministic two-tone beating flagged
  as `multimode`;
- one shared window identity for frequency, period, differential peak-to-peak
  amplitude, average supply power, and the transient receipt;
- the nine-point irregular benchmark curve, its complete local-Kvco vector
  (33–129.75 MHz/V), and 105.9 MHz span;
- a nonmonotonic curve retained with a QC warning, plus incomplete grid and
  shift propagation to typed unknown results;
- signed perturbed-minus-reference supply shift; and
- rejection of a receipt whose frequency was changed without recomputing its
  content digest.

From the repository root, write one fresh evidence file and verify it in the
same command:

```bash
evidence_dir=$(mktemp -d)
PYTHONPATH=src python3 conformance/oscillator-measure-v0alpha1/run.py \
  --evidence-file "$evidence_dir/oscillator.json"
```

The runner opens the evidence path in exclusive-create mode. It will not
overwrite an existing record. A retained record can be checked independently:

```bash
python3 conformance/oscillator-measure-v0alpha1/verify.py \
  "$evidence_dir/oscillator.json"
```

The fixture and bundle are MIT-licensed. Phase-noise measurement is deliberately
absent from this conformance surface.
