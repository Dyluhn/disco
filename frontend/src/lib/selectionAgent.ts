/**
 * In-frame selection agent — injected as a `<script>` into preview iframes.
 *
 * The script is a self-contained IIFE that:
 *  1. Listens for host→frame commands (arm / disarm / walkup).
 *  2. On arm: installs mousemove + click interceptors and draws a hover ring.
 *  3. On click: captures the bounding rect + resolver attributes and postMessages
 *     a `disco:selection` envelope to the parent host window.
 *  4. On walkup: re-targets to the current element's parentElement.
 *  5. On disarm: removes all handlers and hides the ring.
 *
 * Security inside the frame:
 *  - Validates incoming commands: `e.source === window.parent` + `data.v === 1`
 *    + `data.channel === "disco-select"`.
 *  - On arm, stores the session nonce; all outgoing frames carry it so the host
 *    can reject replays.
 *  - Outgoing postMessages target `"*"` because the parent may be on a different
 *    origin from the sandboxed frame.
 *
 * Clean-room reimplementation of the Onlook (Apache-2.0) hover-ring + selector
 * behaviour. No upstream code is transcribed.
 *
 * @module selectionAgent
 */

// SINGLE SOURCE OF TRUTH: the IIFE body lives in the co-located agent-server
// package asset `selection_agent.js` (read by the preview-edit route via
// importlib.resources). We import the very same file here as a raw string (Vite
// `?raw`, aliased in vite.config.ts) so the script the browser runs and the
// script the server injects can never drift. Editing the .js updates both.
import SELECTION_AGENT_SCRIPT_RAW from "@selection-agent-script?raw";

/**
 * JavaScript IIFE source, exported as a plain string for injection into
 * `<script>` tags via `deriveSrcDoc` (srcdoc path), the `preview.py` proxy
 * response rewrite (live-proxy path), or the A1 preview-edit route (server-side).
 */
export const SELECTION_AGENT_SCRIPT: string = SELECTION_AGENT_SCRIPT_RAW;

