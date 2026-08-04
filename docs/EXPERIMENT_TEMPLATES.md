# Experiment templates (`simra.experiment-template/v1`)

`openada experiment compile` turns one closed template plus a parameter
overlay into runnable typed documents:

- `experiment.spec.json` — a fully validated `simra.experiment/v1` document
  (the same validator `openada experiment run` uses; a template that emits an
  invalid experiment is refused and nothing is retained).
- `specifications/<specification_id>.json` — one absolute, unit-bearing
  `specification.evaluate` request per declared specification.
- `compile-receipt.json` — a deterministic receipt binding the template
  digests, the resolved parameter overlay, and every emitted file's SHA-256.

Compilation is deterministic: the same template bytes and the same overlay
produce byte-identical outputs. The receipt carries no timestamps.

## Why template-relative limits

A specification limit written as one measured run's absolute extremum is
anchored to that run's simulator build and stops reproducing across
toolchains. Templates instead declare limits relative to named template
quantities (`nominal ± tolerance`, `fraction × nominal`), and the compiler
resolves them to absolute numbers exactly once, in exact decimal arithmetic,
at compile time. The emitted specification documents remain plain
`specification.evaluate` requests; no downstream schema changes.

## Document shape

```json
{
  "schema": "simra.experiment-template/v1",
  "id": "bandgap_vref_template",
  "constants": {
    "vref_nom": {"value": "1.2", "unit": "V"}
  },
  "parameters": {
    "vdd_nom": {"unit": "V", "minimum": "1.6", "maximum": "2.0", "default": "1.8"}
  },
  "experiment": { "schema": "simra.experiment/v1", "...": "..." },
  "specifications": [
    {
      "specification_id": "vref_band",
      "measurement_id": "vref_dc",
      "limits": {
        "lower": {"value": {"ref": "vref_nom", "factor": "0.99"}, "unit": "V", "inclusive": true},
        "upper": {"value": {"ref": "vref_nom", "factor": "1.01"}, "unit": "V", "inclusive": true}
      }
    }
  ]
}
```

All objects are closed: unknown fields, duplicate JSON keys, and non-finite
numbers are refused. Constant and parameter names share one slug namespace.
Every declared constant and parameter must be referenced at least once.

## Substitution forms

Inside `experiment` (and inside `specifications[].conditions`) exactly two
substitution objects are recognized:

- `{"$ref": "name"}` — replaced by the declared strict SPICE scalar token,
  verbatim, for string positions (element parameters, analysis fields).
- `{"$number": "name"}` — replaced by the resolved finite JSON number, for
  numeric positions (measurement request quantities, condition values).

Any other `$`-prefixed key is refused. When a substitution lands in the
`value` member of an object that also declares a string `unit`, the
referenced declaration's unit must equal that sibling unit exactly; no unit
conversion is ever performed.

## Limit expressions

A bound `value` is either a finite JSON number or the closed linear form
`{"ref": "name", "factor": scalar, "offset": scalar}` evaluated as
`factor × ref + offset` in exact decimal arithmetic. The referenced
declaration's unit must equal the bound's declared unit. `factor` is
dimensionless; `offset` is in the bound's unit. Computed intervals must be
non-empty, and every compiled specification must be accepted verbatim by the
`specification.evaluate` request normalizer, or the compile is refused.

## Overlay

`--set NAME=VALUE` binds one declared parameter to a strict SPICE scalar.
Every parameter without a `default` must be set; unknown names, duplicate
bindings, and values outside the declared inclusive `minimum`/`maximum`
range are refused. The receipt records each parameter's token and whether it
came from the overlay or the default.
