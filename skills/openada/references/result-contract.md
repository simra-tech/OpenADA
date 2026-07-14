# OpenADA result contract

OpenADA emits one JSON object per operation using schema identifier
`openada.result/v0alpha1`.

## Fields

- `operation`: Semantic action requested from OpenADA.
- `tool`: Selected native EDA name, resolved executable, and detected version.
- `execution`: Invocation status, exit code, elapsed milliseconds, and exact argv vector.
- `engineering`: `pass`, `fail`, `unknown`, or `not_applicable`, plus a concise conclusion.
- `inputs`: Native input files with roles, sizes, and SHA-256 hashes.
- `artifacts`: Generated output/evidence files with roles, sizes, and SHA-256 hashes.
- `diagnostics`: Bounded messages with stable codes, severities, and optional recovery hints.
- `data`: Operation-specific normalized measurements or report details.
- `provenance`: OpenADA, Python, OS, architecture, and timestamp metadata.

## Execution statuses

- `completed`: The process launched and exited. Inspect `exit_code` and `engineering.status`.
- `timed_out`: OpenADA stopped waiting at the operation timeout.
- `not_available`: The selected executable could not be resolved or launched.
- `invalid_request`: Required input or a supported argument was invalid before launch.
- `failed`: The driver encountered an operating-system or internal execution error.

## CLI exit codes

- `0`: Engineering `pass` or `not_applicable`.
- `1`: Engineering `fail`.
- `2`: Engineering `unknown`, including unavailable tools or invalid requests.

A nonzero exit can represent useful engineering evidence. Preserve and report the JSON object instead of treating every nonzero CLI status as a missing result.

## Scoped doctor preflight

`doctor --project-root ROOT --assertion ASSERTION` returns one mapped tool and
one `data.preflight.target`. Its engineering `pass` is environment readiness,
not a design result: `data.preflight.assertion_evaluated` remains false.
`data.preflight.pdk.catalog_enumerated: false` means an empty `data.pdks` is
not proof that no PDK exists. Likewise, empty startup `selected_files` means
the recommended operation must still receive exact project-specific
configuration.
