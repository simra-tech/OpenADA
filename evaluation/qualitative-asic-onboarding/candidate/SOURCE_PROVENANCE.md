<!--
SPDX-FileCopyrightText: 2026 OpenADA Vibe CPU Contributors
SPDX-License-Identifier: Apache-2.0
-->

# Source provenance

The candidate was copied byte-for-byte, including Git metadata, from the clean
template checkout below before the CPU adaptation was applied.

- Upstream repository: `https://github.com/IHP-GmbH/ihp-sg13g2-librelane-template.git`
- Upstream commit: `0418301723d86133de686ef743cfd668bb3d11d4`
- Source checkout used by the conductor:
  `/dev/shm/openada-vibe-cpu-20260716/ihp-template`
- Candidate checkout:
  `/dev/shm/openada-vibe-cpu-20260716/candidate`

The original `origin` remote and upstream commit remain in the candidate's
`.git` directory. No adaptation commit was created. `git diff` records tracked
modifications and deletions relative to the exact upstream commit, and
`git status --short` also enumerates the new files.
