# Browser screenshot history and thumbnail acceptance

Owner package: `PKG-26-UI-SCREENSHOT-HISTORY`.

Actual a97 browser acceptance found two failed, zero-width screenshot thumbnails without accessible names. Saved request evidence showed an earlier 404 followed by a later real PNG at the same path; stored events reused counter-based screenshot names after browser-daemon restarts. A retry alone could display newer bytes as old evidence.

Capture now reserves a unique UUID-suffixed file with exclusive creation and ordinary process-umask permissions before Playwright writes. Failure cleanup removes only that new reservation and preserves the original error. Existing screenshot files and event paths are unchanged. Historically overwritten image bytes cannot be reconstructed.

Thumbnail controls have numbered accessible names and selection state. Failed images retain a visible, focusable unavailable state. No automatic cache-busting, historical-path rewrite, model/provider branch, or fixture normalization is introduced.

Four frontend and seven expanded Python regression cases cover names, failure state, history preservation across daemon/workspace replacement, cleanup, umask behavior and exclusive collision protection. Existing test identities and public declarations remain unchanged. Local focused checks passed within their recorded scope; five existing real-renderer cases require workstation execution because the bounded local environment lacked Chromium or prevented Node address-space reservation. This note does not claim those cases or real-browser acceptance passed.

The f83 predecessor passed all ten release gates, 245 preview cases, 461 reader/review cases and 25 strict full-encoder replay cases, with exact-tree frontend evidence. Final combined validation, browser retests and publication remain required. The screenshot dispatch path does not occur in the preserved Paris captures; fixture bytes and strict matching remain unchanged.

Use unchanged authority tools with this unique owner and accepting commit, preserving membership and prior transitions. The only generated authority updates are the public API and test inventory with their existing seal digests; amend them into this accepting commit. Comprehensive UI acceptance stays outside CI. Publish through the workstation CLI.
