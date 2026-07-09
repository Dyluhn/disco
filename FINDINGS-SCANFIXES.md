# Scan Fix Findings

- pnpm claim kept: `deploy/sandbox/Dockerfile` installs `pnpm` globally via `npm install -g npm@latest pnpm --force`, so the prompt claim is verifiable in this repo.
- The named artifact-family catch-alls in `image_gen.py`, `slides.py`, `document.py`, and `audio_overview.py` already returned non-empty `content`; I left those behavior paths intact and added regression coverage for representative failures.
- I also fixed two adjacent empty-content catch-alls found by the sweep (`design_lint.py` and `verify_appkit_app.py`) because they violated the same failure-observation invariant even though they were outside the named Item B list.
- The old assist-only shell spill-to-file behavior was replaced by the new shell observation cap. Full shell output is no longer retained automatically; the observation now instructs the agent to rerun redirected to a file when full output is needed.
