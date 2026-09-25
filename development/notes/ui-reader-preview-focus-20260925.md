# Reader allocation, preview access, and dialog focus

Owner: PKG-25-UI-READER-PREVIEW-FOCUS. Incremental transition after the accepted 7830edeb639f73ffb8d490e4013bc60170051aaf authority baseline. This receipt covers the exact source/API/test changes below; it is not a full UI acceptance or factual-quality claim.

Four empty 12,000-token reader replies previously exhausted the 48,000-token stage allowance before every long source received an attempt. Initial reads now share remaining capacity with one recovery share; later active sources share the remainder. Existing call/stage/role ceilings, pre-dispatch durable reservations, exact known usage charges, conservative missing-usage charges, hash invalidation and honest incomplete outcomes remain intact. Initial-opportunity fairness is not a guarantee of recovery after every later chunk. No provider/model-name behavior is introduced.

LIVE preview requests previously called the process-discovery status path twice merely to read managed generation/port identity. The route now reads existing live_session metadata through a pure projection, while retaining fresh owner/generation checks and actual application dispatch. Availability/Cockpit status and FINISHED routing retain their existing semantics. PreviewService stays within its unchanged public-method budget. Real-browser latency improvement remains to be measured.

A preview left open beyond its lease/cookie deadline previously showed application errors such as invalid credentials. The host now receives actual lease and verified-redemption deadline metadata, warns before expiry, blocks input in the retained iframe at expiry, and requires a confirmed explicit Reload Preview. Cancel, failed mint, and stale responses retain the document; no automatic mutation replay or authorization extension is added. Build, artifact and noVNC consumers share the boundary. Existing Refresh and selection controls cannot bypass expired access. Metadata contains no bearer, is accepted only from the exact current frame/origin before ready, is latched once, and is clamped to the existing lease. The bootstrap watchdog and legacy ready handshake remain.

Real390px keyboard checks found Escape from both audio and include-follow-up choosers returned focus to BODY. Controlled dialogs now restore the actual invoking control, retain the original invoker across a dialog chain, suppress restoration during handoff, and avoid disconnected/disabled/inert targets. Existing selection and generation behavior is preserved.

## Reviewed public surface

| Identity | Reason |
| --- | --- |
| `frontend / frontend/src/api/client.ts / PreviewLaunch` | Optional browser deadline derived from actual server lease expiry; existing url/intent callers remain valid. |
| `frontend / frontend/src/components/PreviewAccessBoundary.tsx / PreviewAccessBoundary` | Retain iframe contents across expiry, warn, block expired input, and require confirmed explicit reload. |
| `frontend / frontend/src/components/PreviewLaunchFrame.tsx / PreviewLaunchFrame` | Optional reload/expiry callbacks around the keyed existing iframe; trusted bootstrap metadata is source/origin bound and latched. |
| `frontend / frontend/src/components/build/agentCanvas/ArtifactPreview.tsx / ArtifactPreview` | Extract existing artifact preview frame and selection integration to stay within unchanged architecture limits. |
| `frontend / frontend/src/components/build/agentCanvas/useLiveBrowserSession.ts / LiveBrowserSession` | Explicit guarded reload callback for the existing noVNC session, without restarting that session. |
| `frontend / frontend/src/components/build/canvas/previewPaneParts/PreviewStage.tsx / PreviewStage` | Thread access expiry and confirmed reload into the existing preview/selection stage. |
| `frontend / frontend/src/components/build/canvas/previewPaneParts/PreviewToolbar.tsx / PreviewToolbar` | Disable the existing Refresh button during expiry so confirmed recovery cannot be bypassed. |
| `frontend / frontend/src/components/build/canvas/previewPaneParts/usePreviewLaunch.ts / PreviewLaunchState` | Explicit asynchronous guarded mint for confirmed reload; preserve current launch on failed/stale replies. |
| `frontend / frontend/src/components/build/canvas/previewPaneParts/usePreviewOpenError.ts / usePreviewOpenError` | Extract existing open-in-new-tab error mapping to preserve architecture limits. |
| `frontend / frontend/src/components/research/IncludeFollowUpsModal.tsx / IncludeFollowUpsModal` | Optional invoker reference and explicit successor-dialog flag; restore cancelled/standalone focus without stealing focus during handoff. |
| `frontend / frontend/src/components/research/IncludeFollowUpsModal.tsx / IncludeFollowUpsModalProps` | Optional returnFocusRef and confirmOpensDialog preserve existing callers and describe focus handoff. |
| `frontend / frontend/src/components/research/needMoreCardParts/AudioModeDialog.tsx / AudioModeDialog` | Optional invoker reference restores the original control when a controlled chooser closes. |
| `frontend / frontend/src/components/research/needMoreCardParts/AudioModeDialog.tsx / AudioModeDialogProps` | Optional returnFocusRef supports the original topbar or footer invoker across a chooser sequence. |
| `frontend / frontend/src/components/research/needMoreCardParts/AudioSection.tsx / AudioSection` | Thread the optional original invoker reference into the persistent audio chooser. |
| `frontend / frontend/src/components/research/needMoreCardParts/AudioSection.tsx / AudioSectionProps` | Optional returnFocusRef leaves existing callers valid. |
| `frontend / frontend/src/hooks/useDialogRestoreFocus.ts / useDialogRestoreFocus` | Capture the actual invoker before autofocus; restore only connected, enabled, non-inert controls and support explicit handoff suppression. |
| `frontend / frontend/src/hooks/usePreviewAccess.ts / usePreviewAccess` | Track conservative lease/cookie deadline with warning, focus/visibility recovery, and irreversible expiry. |
| `frontend / frontend/src/hooks/usePreviewSelectionAccess.ts / usePreviewSelectionAccess` | Disarm selection on expiry and expose its current state to preview consumers. |

Exact old/new fingerprints are reviewed in the companion manifest consumed by the unchanged authority generator. There are18 frontend surface changes (12 changed and6 added), zero removals/relocations, and no Python initializer API changes. Private mint JSON adds actual `access_expires_at` and fractional `server_time`; verified bootstrap metadata carries `expires_at` and `server_time`. Cookie, signature, owner, generation and TTL policies are unchanged.

## Test identity and captured evidence

New focused coverage includes33 LIVE identity cases,17 expanded reader-allocation cases,3 backend deadline cases,27 expanded frontend expiry cases,16 expanded dialog-focus cases, and one explicit historical-v2 integrity case. No accepted test ID, fixture, skip marker, error contract or architecture limit is removed or weakened. The unchanged generators remain responsible for actual collection and transition validation.

The following retained reader IDs intentionally receive allocation-policy expectations while retaining accounting, resume and honest-failure assertions:

- `packages/retrieval/tests/test_reader_output_budget.py::test_truncated_parseable_notes_cannot_pass_and_retry_spends_same_budget`
- `packages/retrieval/tests/test_reader_output_budget.py::test_usage_missing_retains_reservation_and_later_source_is_honestly_unread`
- `packages/retrieval/tests/test_reader_output_budget.py::test_cancelled_dispatch_reservation_survives_json_checkpoint_and_resume`
- `packages/retrieval/tests/test_reader_output_budget.py::test_partial_chunk_notes_and_offsets_survive_exhaustion`
- `packages/retrieval/tests/test_reader_output_budget.py::test_changed_source_requires_fresh_proof_without_refunding_prior_output`
- `packages/retrieval/tests/test_reader_output_budget.py::test_incomplete_read_is_disclosed_to_writer_and_finished_report`
- `packages/retrieval/tests/test_reader_output_budget.py::test_removed_or_shortened_source_spend_survives_another_checkpoint[replacement0]`
- `packages/retrieval/tests/test_reader_output_budget.py::test_removed_or_shortened_source_spend_survives_another_checkpoint[replacement1]`
- `packages/retrieval/tests/test_source_reading_diagnostics.py::test_exhaustion_trail_distinguishes_empty_truncated_and_unattempted_after_reload`
- `packages/retrieval/tests/test_source_reading_diagnostics.py::test_success_on_last_budget_token_keeps_validated_notes_and_clean_trail`
- `packages/retrieval/tests/test_source_reading_fairness.py::test_six_empty_sources_receive_four_first_attempts_not_two_immediate_retries`

The retained frontend case `NeedMoreCard closing the mode dialog returns to idle without generating` additionally asserts restored trigger focus. All other previously accepted dialog assertions remain.

The original strict replay fails because the first reader request ceiling intentionally changed from12,000 to6,000. The old response is not relabeled. The genuine `loop_paris_standard_low_v3` capture contains293 events/129 interactions from source798f239cc, identified over the7830 base image with four read-only production overlays. The pair replayed exactly offline, both with its private captured configuration and with the default public harness configuration (all24 cases, no skips). The current strict test ID and matching/equality/consumption rules remain unchanged; v2 and loop_demo bytes remain preserved, with an added v2 integrity check. The CLI points to v3. Capture and replay are integration evidence, not UI setup or factual-quality proof.

The actual v3 checkpoint completed all7 long sources in15calls using16,755 charged output tokens. The report still contains an unsupported population-definition reconciliation and a table/prose verification inconsistency. Those findings are retained; no report-quality pass is claimed.

Focused tests, types/lint and architecture checks passed in isolated worktrees; the integrated candidate must pass the combined qualification, unchanged release gates and standard production builds. Real-browser expiry/confirm/cancel, focus restoration and latency retests follow deployment. Comprehensive UI acceptance remains outside CI; unavailable, disabled or unexercised controls remain explicitly classified. Publishing uses the workstation CLI.

Only public API/test-inventory authority digests may advance, with unchanged protected membership and the authorized seal refresh. Amend those generated records into this accepting commit rather than creating a child authority commit.
