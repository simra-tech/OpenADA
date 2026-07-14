# Paired-agent trace adapters

`codex_jsonl.py` reduces the JSONL emitted by `codex exec --json` into the
content-free `openada.eval.trace/v0alpha1` contract. It supports exactly Codex
CLI 0.144.3 and parser version 1. The adapter is evaluation infrastructure; it
is not part of OpenADA's agent-facing CLI or result contract.

Example:

```bash
python3 evaluation/paired-agent/adapters/codex_jsonl.py \
  --input codex-events.jsonl \
  --codex-cli-version 0.144.3 \
  --process-exit-code 0 \
  --fresh-thread \
  --adapter-duration-ms 1234
```

`--input -` reads standard input. A safely parsed stream produces exactly one
JSON object on standard output and exit status 0, including when some metrics
are ineligible. Unsafe, unparseable, or unreadable input produces exactly one
schema-valid fixed-shape rejected trace on standard output, a bounded
content-free reason code on standard error, and exit status 2. Preserve that
document as the planned trial's trace, record supervisor termination
`adapter_failed`, and never delete or rerun the assignment because reduction
failed.

The input can contain raw Codex events or envelopes shaped as
`{"elapsed_ms": 12, "event": {...}}`. Envelope times must be non-negative and
monotonic. They are adapter observations, not provider timestamps or TTFT.

The reducer enforces these fixed bounds:

- UTF-8 JSON objects with duplicate keys and non-finite numbers rejected;
- at most 1 MiB per input line and 16 MiB per stream;
- JSON depth at most 16 and at most 10,000 source events;
- at most 256 public action records.

Commands, arguments, outputs, paths, tool/server names, messages, reasoning,
errors, native item/thread identifiers, search data, and collab prompts or
identifiers are discarded. The only result-size observation is
`command_result_observed_characters`; it is not model-context volume.

The stream exposes only structural item categories and status/exit buckets.
Provider-request and session metrics are always ineligible because Codex exec
JSONL has no API request ID, native turn ID, or execution-context identity.
Engineering outcome requires the independent task scorer. Token usage is
retained only for a completed, structurally complete, process-consistent run
explicitly declared fresh and single-turn; Codex reports a cumulative thread
usage snapshot. Missing or ineligible measurements remain `null`, never zero.

The rejected trace deliberately contains no actions, usage, duration, or
source thread identity. Its source CLI version is retained only when the
declared version is supported; it does not echo rejected input or filesystem
details.

The schema is `../schemas/trace-v0alpha1.schema.json`.
