# IHP SAR RTL semantic conformance

This chain closes OpenADA's `rtl-check` and strict `rtl-lint` intents on a real public design: the
8-bit SAR controller in IHP AnalogAcademy at commit
`133ecf657572e021b5921b5a1b7693abfb209623`. It covers exactly:

- `surface|openada.surface/cli.rtl-check/v1`
- `preflight|rtl-structural-check-passes`
- `surface|openada.surface/cli.rtl-lint/v1`
- `preflight|rtl-lint-clean`
- `profile|openada.operation/rtl.lint/v1alpha1`
- `assertion|openada.operation/rtl.lint/v1alpha1|openada.assertion/rtl.lint.clean/v1alpha1`
- `feature|openada.operation/rtl.lint/v1alpha1|openada.feature/rtl.lint.systemverilog/v1alpha1`
- `native-mapping|openada.operation/rtl.lint/v1alpha1|org.openada.driver.verilator|org.verilator.verilator|openada.feature/rtl.lint.systemverilog/v1alpha1`
- `provider|org.openada.driver.verilator|openada.operation/rtl.lint/v1alpha1|openada.feature/rtl.lint.systemverilog/v1alpha1`

Both language-selector values exposed by the lint profile (`1800-2017` and
`1800-2023`) are replayed independently. No unlisted surface, provider, or
native mapping is claimed.

The pinned source is 576 bytes with SHA-256
`b33c7b25215ac916b3b07e0dc385ae353294f6872eaa226f4c0126ecfd7063da`.
The network-disabled reference run uses the linux/amd64 IIC-OSIC-TOOLS image
by manifest and config digest, Yosys 0.66 by exact version, and the direct
Verilator 5.048 `verilator_bin` executable at its pinned path. Both lint probes
and executions use the same closed non-inheriting runtime environment. The runner keeps
the OpenADA checkout and public design read-only, drops container capabilities,
and makes only a fresh evidence directory and isolated `/tmp` writable.

## Reproduce the native chain

From the repository root, install the conformance dependencies, fetch the
pinned image and detached design checkout, then run into a path that does not
already exist:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[conformance]'

python3 conformance/ihp-sar-rtl/setup.py
python3 conformance/ihp-sar-rtl/run.py \
  --evidence-dir /tmp/openada-ihp-sar-rtl-evidence \
  --receipt-class release
```

`setup.py` is the only network-enabled stage. `run.py` never pulls and launches
all five native requests with `--network none`:

1. `sar_logic` must elaborate, pass `check -assert`, and produce a parsed native
   Yosys JSON netlist.
2. `sar_logic` must pass strict Verilator lint with zero warnings and zero errors
   under the `1800-2017` selector and fixed `.v`, `.sv`, `.vh`, and `.svh`
   extension mappings.
3. The same source and top must independently pass the same strict policy under
   the `1800-2023` selector and corresponding fixed extension mappings.
4. The same pinned source requested as top `missing_sar_logic` must fail natively
   in Yosys with `ERROR: Module \`missing_sar_logic' not found!`.
5. A real Verilator request for top `missing_sar_logic` must fail natively with
   `Specified --top-module 'missing_sar_logic' was not found in design.`.

The evidence retains all five OpenADA results, both generated Yosys scripts, the
positive Yosys JSON, exact Yosys transcripts, all three complete fresh Verilator
logs, and `run.json`.
The latter binds every artifact plus image, source, wrapper, and unchanged
before/after checkout state.

## Independent evidence and adversarial replay

The verifier does not import OpenADA:

```bash
python3 conformance/ihp-sar-rtl/verify.py \
  /tmp/openada-ihp-sar-rtl-evidence
```

It validates the result and lint-profile data schemas; exact source, image,
wrapper, native tools, both language-selector argv variants, scripts, and
artifact hashes; both real missing-top failures; and the native JSON structure.
The reviewed positive structure is one `sar_logic`
module with five one-bit inputs, three 8-bit outputs, B structurally aliased to
D, 18 cells, no black boxes, a 4-bit counter, and three state elements with
widths 4, 8, and 8.

Publish the verified evidence for agents:

```bash
python3 conformance/ihp-sar-rtl/semantic.py --publish \
  --evidence-dir /tmp/openada-ihp-sar-rtl-evidence
```

Publication first reruns the independent verifier. It then removes `rst` from a
copy of the native Yosys JSON and reconciles both the OpenADA artifact digest
and run-level digests. The independent structural oracle must still reject that
tamper with `positive Yosys JSON ports`. The real negative and tamper verdicts
are stored separately under `semantic-replays/`. A second tamper injects a
Verilator latch warning into the previously clean transcript and reconciles the
OpenADA and run-level hashes; the independent transcript oracle must still reject it.

Verify the repository-local publication without Docker or the external design:

```bash
python3 conformance/ihp-sar-rtl/semantic.py
python3 -m pytest -q tests/test_ihp_sar_rtl_conformance.py
```

## Agent decision and standards boundary

`semantic-evidence.json` says `proceed` only to behavioral, formal, timing, and
mixed-signal integration verification. The evidence supports that bounded
decision because exact elaboration, interface/state structure, clean strict lint,
native negative behavior, and tamper rejection all agree. Lint is not functional
correctness, CDC safety, timing closure, or physical proof, and this chain is not
tapeout signoff; the document gives
the next checks and explicit block conditions.

No IEEE measurement standard applies because this chain computes no electrical
or signal-quality measurement such as SNR. The active
[IEEE 1800-2023 SystemVerilog standard](https://standards.ieee.org/ieee/1800/7743/)
is recorded as language context because the drivers invoke `read_verilog -sv`
and independently declared Verilator 1800-2017 and 1800-2023 selectors. The
chain certifies those pinned Yosys and Verilator behaviors, not complete IEEE
1800 compliance.
