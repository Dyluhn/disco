# LIVE Preview routing and unobstructed inspector controls

Owner: PKG-24-UI-PREVIEW-FOLLOWUP. This receipt accepts the incremental source and
test inventory changes after the reviewed 9ba7e6de5 authority baseline. It does
not declare comprehensive UI acceptance or release qualification complete.

Real browser testing of the isolated Service Desk project found that an app
could render while LIVE but return 502 on sign-in: the published listener was
unreachable outside its isolated network. Retrying a mutation through another
transport would risk duplicate writes. The fix selects the existing approved
in-session transport before dispatch, binds the owner, manager, selected
projection and sandbox generation, and checks them again after body collection
and awaited lookup. An ambiguous outcome is never retried. Host-shared listeners
retain their existing published transport. No runtime is started by this route.
LIVE and FINISHED paths share bounded HTTP response, cookie and header policy.

Thirty new ASGI regression cases cover methods, authentication headers and
cookies, escaped paths, stale or missing bindings, changed authority during
awaits, owner lookup failures, framing/body limits, disconnection and ambiguous
outcomes. All existing test identities are retained. Native focused validation
passed 115 cases, followed by a 65-case final rerun including the two latest
negative cases. Full Python types, Ruff and architecture checks passed; the
final statement-order adjustment was covered by the focused rerun.

Browser testing also found that the floating Inspect button intercepted an
embedded app's Sign out button. Inspector controls now occupy a separate host
toolbar above each iframe. Selection rectangles and edit affordances retain
their iframe-relative positioning. The first-run hint occupies toolbar layout
space as well. Existing inspector and preview tests passed (49 cases). A ResizeObserver clears
stale selection coordinates after actual viewport size changes while preserving
Inspect mode; unchanged first observations retain selection. Five added hook
cases cover size changes, initial observation, queued resize across rerenders,
and observer cleanup. All six hook cases passed. Plain Inspect now displays its own Stop inspecting
state independently of Edit. Switching Inspect and Point disarms the previous
mode; five new real-hook component cases cover those transitions, stale nonce
rejection, Edit behavior and the untrusted boundary. The 59 directly affected
frontend cases passed. The prior full 1,765-case run passed before this small
follow-up; its result is not relabelled as a full run of the later source.

The reviewed frontend declaration changes are:

| Module | Export | Accepted change |
| --- | --- | --- |
| frontend/src/components/build/canvas/SelectionOverlay.tsx | SelectionOverlay | Optional `showToolbar` prop defaults to true, preserving existing callers; iframe hosts render controls separately and disable the embedded toolbar. |
| frontend/src/components/build/canvas/SelectionOverlay.tsx | SelectionToolbar | Extracted controls share the existing arm/disarm callbacks and untrusted restriction without covering app controls. |
| frontend/src/components/build/canvas/previewPaneParts/PreviewStage.tsx | PreviewStage | Adds the host-owned onArmInspect callback so Inspect and Point are mutually exclusive; all repository callers supply it. |

There are no public Python interface changes, removals, renamed test cases,
provider/model-specific branches, sandbox permission changes or new CI UI gates.
The unchanged official authority generators and seal check govern acceptance.
Deployment, browser regression, and remaining research quality/latency gaps are
tracked separately; test execution is not substituted for real UI acceptance.
