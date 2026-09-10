# Export Track-1 Closeout — frozen fixtures

FROZEN acceptance path (WO-C0, plan §1.1: `current/packages/agent-server/tests/fixtures/export_track1_closeout/**`).

This directory holds the VERBATIM sample workspaces the closeout matrices assess
end-to-end (the positive/negative detection matrix of WO-C3, the env/build/
toolchain fixtures of WO-C4, the injection corpora of WO-C5, the overlay-collision
matrix of WO-C6, the multi-service topology fixtures of WO-C7, and the clean-room
Docker lifecycle fixtures of WO-C8). Fixtures are built from real captured samples,
never hand-tuned to pass — a name/id may be randomized from the recorded
`CLOSEOUT_SEED` at test time, but file CONTENTS are real.

This tranche (WO-C0 foundation) ships the harness plumbing only; the fixture
workspaces themselves are added in the C3–C8 tranches. This file exists so the
frozen path is present and hashed by
`current/docs/export-track1-closeout-acceptance.sha256` from the acceptance tag onward.
