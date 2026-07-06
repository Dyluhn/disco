/*
 * The DATA-ACCESS LAYER (BoD §13.8) — the ONE place that talks to "the API".
 * Components never import this; only query/mutation hooks do.
 *
 * Research-answer streaming is now LIVE: when VITE_AGENT_BASE is set,
 * `subscribeResearch` opens a WebSocket to the agent-server's /ws/research and
 * forwards the real rewrite→search→extract→rerank→generate→verify pipeline's
 * frames (state→token→final) — the same StreamFrame contract the fixture used.
 * Unset → it replays the in-repo fixture (offline / tests), so the UI and its
 * tests run with no backend.
 */
import { ensureAgentSession, researchWsUrl } from "@/api/client";
import { rrfAnswer, sheetDemoAnswer } from "@/fixtures/answers";
import type { AnswerBlock, GroundedAnswer, ReScope, StreamFrame } from "@/types/grounded";

export interface StreamHandle {
  cancel: () => void;
}

/** Stream cadence (mutable so tests can zero it; also a real tuning surface). */
export const streamTiming = {
  ttft: 650, // time-to-first-token: the signature micro-interaction window
  token: 9, // per-token cadence (typewriter)
  blockGap: 90, // between structured blocks
};

/** Apply a re-scope to the live answer (fake "re-retrieval"): drop weak claims
 * and deny domains. The real backend re-issues `RetrievalEngine.retrieve`; here
 * we transform the fixture so the UI's re-scope controls have visible effect. */
function applyScope(base: GroundedAnswer, scope: ReScope): GroundedAnswer {
  let answer = { ...base, query: scope.query || base.query };
  if (scope.drop_weak) {
    const drop = new Set(
      answer.claims
        .filter((c) => c.verdict !== "supported")
        .flatMap((c) => c.claim.cited_passage_ids),
    );
    answer = {
      ...answer,
      claims: answer.claims.filter((c) => c.verdict === "supported"),
      blocks: answer.blocks.map((b) =>
        b.kind === "prose"
          ? { ...b, text: b.text.replace(/\[\[(\w+)\]\]/g, (m, id) => (drop.has(id) ? "" : m)) }
          : b,
      ),
    };
  }
  if (scope.domains_deny?.length) {
    const deny = scope.domains_deny;
    const blocked = (u: string) => deny.some((d) => u.includes(d));
    answer = {
      ...answer,
      all_hits: answer.all_hits.filter((h) => !blocked(h.url)),
      passages: answer.passages.filter((p) => !blocked(p.source_url)),
    };
  }
  return answer;
}

/** Turn a GroundedAnswer into the ordered frame stream a real run would emit:
 * state(running) → per-block (token… then the completed block) → final → state. */
function* framesFor(answer: GroundedAnswer): Generator<{ delay: number; frame: StreamFrame }> {
  yield { delay: streamTiming.ttft, frame: { type: "state", status: "running" } };
  for (const block of answer.blocks) {
    if (block.kind === "prose" || block.kind === "heading") {
      const text = block.text;
      for (let i = 1; i <= text.length; i += 3) {
        yield {
          delay: streamTiming.token,
          frame: { type: "token", token: text.slice(i - 1, i + 2), block_id: block.id },
        };
      }
    }
    yield { delay: streamTiming.blockGap, frame: { type: "block", block: block as AnswerBlock } };
  }
  yield { delay: streamTiming.blockGap, frame: { type: "final", answer } };
  yield { delay: 0, frame: { type: "state", status: "finished" } };
}

/**
 * Subscribe to a research run. Calls `onFrame` for each frame until done or
 * `cancel()`. [VERIFY] swap this body for a WebSocket to /ws/conversations/{id}
 * (event-state §7) when the agent-server is wired; the frame contract is identical.
 */
/**
 * Reactive error surfacing (Prompt 3E / llm-router v1.3). A real run sends the
 * request as assigned and, if the provider rejects it, surfaces the PROVIDER's
 * real error content — never a generic failure. The fixture triggers that path
 * when the query asks for it, so the UI's clean-error state is exercised. The
 * message mirrors the backend's `LLMError [provider/model]: reason` shape.
 */
function providerErrorFor(scope: ReScope): string | null {
  if (/provider-error/i.test(scope.query)) {
    return "LLMContentFiltered [openrouter / frontier-xl]: the model refused this request — image input is not supported by the assigned model.";
  }
  return null;
}

/**
 * The LIVE path: open a WebSocket to the agent-server's research endpoint, send
 * the scope as one query frame, and forward the server's frames verbatim — the
 * backend emits the exact StreamFrame contract this hook reconciles. Closing the
 * socket cancels the in-flight run (the server stops streaming on disconnect).
 */
function subscribeLive(
  url: string,
  scope: ReScope,
  onFrame: (f: StreamFrame) => void,
): StreamHandle {
  let closed = false;
  let ws: WebSocket | null = null;
  function openSocket() {
    if (closed) return;
    ws = new WebSocket(url);
    ws.onopen = () => {
      ws?.send(
        JSON.stringify({
          query: scope.query,
          drop_weak: scope.drop_weak ?? false,
          domains_deny: scope.domains_deny ?? [],
          model_override: scope.model_override ?? null, // the leader pill → answerer
          think: scope.think ?? false, // reasoning mode for the answerer (Think toggle)
          // G1/DR-4: thread the pre-created cid so the server loads upload seeds.
          // null → OFF path (server-side byte-identical to pre-DR-4 code).
          conversation_id: scope.conversation_id ?? null,
          sources: scope.sources ?? [],
        }),
      );
    };
    ws.onmessage = (ev) => {
      if (closed) return;
      try {
        onFrame(JSON.parse(ev.data) as StreamFrame);
      } catch {
        onFrame({ type: "error", message: "malformed frame from server" });
      }
    };
    ws.onerror = () => {
      if (!closed) onFrame({ type: "error", message: "research stream connection failed" });
    };
    ws.onclose = () => {
      /* terminal */
    };
  }
  void ensureAgentSession().then(openSocket).catch(() => {
    if (!closed) onFrame({ type: "error", message: "research auth failed" });
  });
  return {
    cancel: () => {
      closed = true;
      ws?.close();
    },
  };
}

export function subscribeResearch(scope: ReScope, onFrame: (f: StreamFrame) => void): StreamHandle {
  const live = researchWsUrl();
  if (live) return subscribeLive(live, scope, onFrame);

  // Offline / tests: replay the fixture as the same frame contract.
  const providerError = providerErrorFor(scope);
  // "sheet-demo" query routes to the sheet-block fixture (RP-11 visual check);
  // keep its own query string so the block, not the swapped query, is the point.
  const base = /sheet-demo/i.test(scope.query) ? sheetDemoAnswer : rrfAnswer;
  const answer = applyScope(base, scope);
  const seq: { delay: number; frame: StreamFrame }[] = providerError
    ? [
        { delay: streamTiming.ttft, frame: { type: "state", status: "running" } },
        { delay: streamTiming.blockGap, frame: { type: "error", message: providerError } },
      ]
    : [...framesFor(answer)];
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const pump = (i: number) => {
    if (cancelled || i >= seq.length) return;
    const { delay, frame } = seq[i];
    timer = setTimeout(() => {
      if (cancelled) return;
      onFrame(frame);
      pump(i + 1);
    }, delay);
  };
  pump(0);

  return {
    cancel: () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    },
  };
}

/** The "submit / re-scope" action endpoint. Real impl POSTs to create a
 * conversation and kick retrieval; the fake acknowledges and returns the scope
 * to stream. Kept here so the mutation hook never touches transport directly. */
export async function requestResearch(scope: ReScope): Promise<ReScope> {
  await new Promise((r) => setTimeout(r, 20));
  return scope;
}
