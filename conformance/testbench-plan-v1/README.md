# Closed testbench-plan v1 conformance

This bundle exercises the public OpenADA testbench-plan validator, deterministic
ngspice compiler, and native execution runner against an MIT-licensed synthetic
RC DUT. It binds the checkout-local DUT by a complete runtime override, expands
the fresh DC sweep into three independent operating-point decks, executes those
decks plus one finite pulse-train transient, and preserves deck and waveform
digests in every observable's condition lineage.

The bundle records the exact host ngspice identity instead of constraining an
unpinned CI package version. The reference direct run was performed with
ngspice 46. From the repository root, create a fresh evidence file and
independently verify it with:

```bash
mkdir -p .conformance-evidence
python3 conformance/testbench-plan-v1/run.py \
  --evidence-file .conformance-evidence/testbench-plan-v1.json
python3 conformance/testbench-plan-v1/verify.py \
  .conformance-evidence/testbench-plan-v1.json
```

`run.py` refuses to overwrite an evidence file. `verify.py` imports neither
OpenADA nor the native simulator; it validates the closed plan and observables
schemas, re-hashes manifest-bound sources and fixtures, checks the exact
compiler inventory against frozen deck digests, cross-links all four native
condition receipts to observable lineage, and exercises four declared tamper
cases. The plan's artifact path is deliberately non-portable metadata; `run.py`
replaces the entire ABI-compatible DUT binding with the checkout-local fixture
path and exact digest before validation.
