# ngspice PDK control-deck provider

This reference local provider maps one deliberately small connection-level
subset onto `openada.operation/circuit.simulate/v1alpha2`. It exists separately
from the built-in model-free bridge so model/PDK mechanics do not weaken that
bridge's immutable input policy.

The provider accepts only:

- one content-bound filesystem testbench;
- one content-bound `simulator-configuration` document satisfying
  `provider-config-v0alpha1.schema.json`;
- one content-bound `pdk` identity file;
- one OP, DC, AC, or transient request that exactly matches a single
  `.control` block; and
- the strictly ordered control commands `save all`, exactly one matching
  `op`, `dc ...`, `ac ...`, or `tran ...`, optional exact `linearize` after
  TRAN only, and one safe `write FILE`.

It rejects every other ngspice control command. The target, startup files, PDK
identity label, provider-selected executable, private executable snapshot,
log, generated launcher, and native raw result are retained or recorded.
Model files reached through the PDK startup search path are not enumerated or
proven clean. The recorded `COMMIT` bytes are therefore an identity label, not
a complete PDK tree attestation or a basis for model correctness.

The request configuration content-binds both startup files and the two allowed
PDK values, `PDK` and absolute `PDK_ROOT`; it cannot select executable code.
The provider resolves only a native ELF `ngspice` at its fixed, provider-owned
absolute locations, content-binds it, copies it into a fresh private
non-writable launch snapshot, and rechecks both identities after execution.
Its version probe and simulation run use a closed child environment containing
only fixed locale/time/path/temp values, the two configured PDK values, and
`SPICE_SCRIPTS` derived from the bound `spinit` parent. Ambient loader, Python,
shell, home, ngspice-init, and library variables are not inherited. The
normalized result records the environment mode and complete effective child
mapping.

The generic local-provider transport is closed before this provider starts as
well. Bare entrypoints resolve only from the authorized working directory's
`bin`, the current Python installation's scripts directory, and fixed system
locations—not caller `PATH`. Every provider entrypoint or standalone code/data
argv file is copied to a fresh private launch snapshot before `Popen`, and the
original and snapshot identities are rechecked afterward. The launcher
receives a fixed locale, timezone,
and `/usr/bin:/bin` path plus private home/temp directories; ambient Python,
loader, PDK, home, and temporary-directory variables are absent. This also
prevents the launcher's `/usr/bin/env python3` shebang from selecting an
attacker-supplied interpreter.

This provider also requires the exact `local-json-stdio` selector and exactly
the OP, DC, AC, or TRAN feature matching the authoritative analysis. It
requires retained native log/artifacts, bounded provenance,
content-addressable evidence, `wait` completion, fresh destination ownership,
and byte ceilings no smaller than the driver's fixed validated limits. It
rejects unsupported or ambiguous fields before creating the evidence
destination.

Invoke it only through the validated provider boundary:

```bash
openada provider validate providers/ngspice-pdk-control/driver-manifest.json
openada provider invoke \
  --manifest providers/ngspice-pdk-control/driver-manifest.json \
  request.json
```

The pinned public-IHP OP/DC/AC/TRAN end-to-end replay and independent verifier
live in `conformance/ihp-ngspice-provider-analyses`.
