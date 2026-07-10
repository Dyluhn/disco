"""Stable DOM selectors for the Disco UI soak.

Keep selectors here so product-hook drift has one obvious repair point.  Each
entry names the frontend source that defines it; text selectors are avoided
except for the Preview tab, whose accessible name is its only stable handle.
"""

# frontend/src/components/PairingGate.tsx: token input data-disco-control. The
# submit button has no hook, so it is scoped to the form containing that input.
PAIRING_TOKEN = '[data-disco-control="auth.pairing-token"]'
PAIRING_SUBMIT = f'form:has({PAIRING_TOKEN}) button[type="submit"]'
PAIRING_ERROR = f'form:has({PAIRING_TOKEN}) [role="alert"]'

# frontend/src/shell/NavRail.tsx: New calls markAllModesFresh(), preventing the
# mode slider from resuming an older active Build conversation.
NEW_CONVERSATION = '[data-disco-control="shell.nav-new"]'

# frontend/src/shell/ModeSlider.tsx: stable per-mode data-disco-control hooks.
MODE_SEARCH = '[data-disco-control="shell.mode-search"]'
MODE_BUILD = '[data-disco-control="shell.mode-build"]'
APP_READY = MODE_SEARCH

# frontend/src/components/QueryInput.tsx: textarea aria-label and primary-send
# control id. The component is intentionally reused by Search and Build.
COMPOSER = 'textarea[aria-label="Ask Disco a question"]'
PRIMARY_SEND = '[data-disco-control="send-message"]'

# frontend/src/components/ResearchSurface.tsx: explicit harness phase/stream
# attributes. "done" can include an error; "final" means an answer exists.
RESEARCH_IDLE = '[data-research-phase="idle"]'
RESEARCH_COMPOSER = f'{RESEARCH_IDLE} {COMPOSER}'
RESEARCH_SEND = f'{RESEARCH_IDLE} {PRIMARY_SEND}'
RESEARCH_FINAL = '[data-research-phase="done"][data-stream-state="final"]'

# frontend/src/components/AnswerDocument.tsx: the answer body is the semantic
# article. Non-whitespace inner text prevents the question from false-passing.
RESEARCH_ANSWER = f'{RESEARCH_FINAL} article'
# frontend/src/components/ResearchSurface.tsx + states.tsx: ErrorState role.
RESEARCH_ERROR = '[data-research-phase] [role="alert"]'

# frontend/src/components/build/PlanPanel.tsx: stable approval affordance.
APPROVE_PLAN = '[data-disco-control="approve-plan"]'

# frontend/src/components/build/AgentStatusBar.tsx: raw ConversationStatus is
# exposed via data-status. FINISHED is the requested successful terminal; the
# frontend ConversationStatus union has no VERIFIED value.
BUILD_STATUS = '[data-disco-control="build.status"]'
BUILD_FINISHED = f'{BUILD_STATUS}[data-status="FINISHED"]'
BUILD_FAILED = (
    f'{BUILD_STATUS}[data-status="ERROR"], '
    f'{BUILD_STATUS}[data-status="STUCK"]'
)

# frontend/src/components/build/ExecutionCanvas.tsx: Tabs.Root exposes its
# active tab. The Preview trigger's accessible name is the sole text fallback.
PREVIEW_TAB_ROLE = "tab"
PREVIEW_TAB_NAME = "Preview"
PREVIEW_ACTIVE = '[data-active-tab="preview"]'
PREVIEW_PANEL = f'{PREVIEW_ACTIVE} [role="tabpanel"][data-state="active"]'

# frontend/src/components/build/canvas/PreviewPane.tsx: legitimate preview
# iframe variants use stable titles (plain HTML, live server, server artifact).
PREVIEW_IFRAMES = (
    'iframe[title="Static preview"], '
    'iframe[title="Live preview"], '
    'iframe[title="Artifact preview"]'
)
