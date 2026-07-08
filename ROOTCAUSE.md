# ROOT CAUSE: GLM actionless stalls in `conv_a7e7a24316064c8db8b821bc13bfa462`

## 1. Mechanism

The loop paused because GLM consumed driver turns without producing any adapter-visible work:

1. The OpenAI-compatible streaming adapter only treats `delta.content` as assistant text and `delta.tool_calls` as executable action. It collects `delta.reasoning_content`, but only uses it for tool-call recovery when `req.assist` is true. This GLM run was on the standard/capable path, so `assist=False`; reasoning-only output is not persisted and is not an action. See `openai_provider.py:796-804` and `openai_provider.py:870-881`.
2. `RouterAgent.step` maps a response with no `tool_calls` to `AgentStep(tool_call=None)`. In Build mode, prose is not finish; the only way to complete is the `finish` tool. See `agent.py:266-321`.
3. A tool-less step with nonblank text is persisted as an agent `MessageEvent`. A tool-less step with blank/whitespace text persists nothing and increments `_invisible_steps`. See `turn_control.py:1749-1761`.
4. After each no-op, `_post_noop_valve` recomputes `noops = signals.consecutive_noops(events) + self._loop._invisible_steps`. See `turn_control.py:923-931`. The breaker cap is 3 (`engine.py:755-762`).
5. When `noops >= 3` and plan steps remain incomplete, `actionless_valve` lands `PAUSED(actionless)` and then the blocked-question landing (`AWAITING_USER_QUESTION`). See `turn_control.py:704-773` and `turn_control.py:438-523`.

The event-log symptom follows from that design: the persisted log can show fewer than three assistant artifacts because blank/no-output model turns are deliberately counted only in memory. In this conversation, each actionless pause had one persisted agent prose no-op immediately before it, so the other two no-ops were invisible steps.

Important correction: this `evidence.db` snapshot contains three persisted `StatusEvent(status=PAUSED, detail=actionless)` rows, not four. They are seq 17, 29, and 60. Each is followed by a blocked landing at seq 20, 32, and 63. Counting all status payloads whose metadata mentions actionless gives six status rows, not four. The productive burst happened between the second and third persisted PAUSED(actionless), not between a third and fourth in this snapshot.

## 2. Evidence chain

### Event-log facts

- Seq 14 approved the first plan; seq 16 is a tool-less agent message: "Let me check the design tokens file that the direction provides, then scaffold everything." Seq 17 is `PAUSED/actionless`.
- Seq 26 approved the revised plan; seq 28 is a tool-less agent message: "On it - starting step 1 now..." Seq 29 is `PAUSED/actionless`.
- Seq 43-58 show real work: shell, CSS/HTML/storage writes, an elided-placeholder write error at seq 56, then a successful shell diagnostic at seq 57-58. Seq 59 is a tool-less agent message saying it still needs to write four JS modules. Seq 60 is `PAUSED/actionless`.
- Before seq 17, 29, and 60, `signals.consecutive_noops(events_before_pause)` is exactly 1. Since the breaker cap is 3, each pause requires two in-memory invisible no-op steps that are not in `events`.

### Reproducer script

Script committed at `scripts/repro_glm_actionless.py`.

Command used:

```bash
PYTHONPATH=$(ls -d $PWD/packages/*/src | tr '\n' ':') \
GLM_REPRO_API_KEY=<redacted> \
/var/home/dylan/projects/disclaude/.venv/bin/python scripts/repro_glm_actionless.py
```

Output:

```text
conversation=conv_a7e7a24316064c8db8b821bc13bfa462
persisted_PAUSED_actionless_count=3
persisted_PAUSED_actionless_seqs=[17, 29, 60]
pause_seq=17 persisted_consecutive_noops=1 inferred_invisible_steps_to_reach_3=2
  trailing_persisted_events:
    seq=12 PlanEvent
    seq=13 status status=AWAITING_PLAN_APPROVAL detail=evt_97eab6d44d874e558eb79d50c496343e
    seq=14 status status=RUNNING detail=plan_approved
    seq=15 status status=RUNNING detail=None
    seq=16 message source=agent content='Let me check the design tokens file that the direction provides, then scaffold everything.'
pause_seq=29 persisted_consecutive_noops=1 inferred_invisible_steps_to_reach_3=2
  trailing_persisted_events:
    seq=24 PlanEvent
    seq=25 status status=AWAITING_PLAN_APPROVAL detail=evt_55cda45bbefb4b3ebbd67714b35d2d51
    seq=26 status status=RUNNING detail=plan_approved
    seq=27 status status=RUNNING detail=None
    seq=28 message source=agent content='On it — starting step 1 now. Let me grab the design tokens file first so I build with the right palette, then scaffold t...'
pause_seq=60 persisted_consecutive_noops=1 inferred_invisible_steps_to_reach_3=2
  trailing_persisted_events:
    seq=55 action tool=file_write
    seq=56 agent_error
    seq=57 action tool=shell
    seq=58 observation tool=shell success=True
    seq=59 message source=agent content='I have index.html, all three CSS files, and storage.js on disk. I still need to write the four remaining JS modules (tre...'
persisted_agent_message_count=7
persisted_agent_message_seqs=[16, 19, 28, 31, 42, 59, 62]
live_probe_http_status=200
live_probe_finish_reasons=['stop']
live_probe_adapter_visible_content_len=0
live_probe_adapter_visible_content_repr=''
live_probe_reasoning_content_len=93
live_probe_reasoning_content_prefix='The user wants me to return exactly three spaces and nothing else. No tools should be called.'
live_probe_structured_tool_chunk_count=0
live_probe_usage={'prompt_tokens': 178, 'total_tokens': 200, 'completion_tokens': 22, 'prompt_tokens_details': {'cached_tokens': 177}}
```

The live probe is not the historical build prompt; it reproduces the relevant provider shape from GLM-5.2 through the same endpoint: `finish_reason=stop`, no `content`, no structured tool call, and non-empty `reasoning_content`. Under the current adapter and BuildAgent mapping, that is an actionless/invisible step.

### Why the persisted assistant count is lower than "3"

The no-op count at the valve is not only persisted assistant messages. It is:

```text
signals.consecutive_noops(events) + self._loop._invisible_steps
```

`signals.consecutive_noops` counts trailing persisted agent prose messages and a few persisted non-work events (`signals.py:509-570`). `_invisible_steps` carries no-output steps that intentionally have no event row (`engine.py:755-762`, `turn_control.py:1757-1760`). That is why seq 17, 29, and 60 can honestly use the "3 consecutive responses" pause text while the database shows only one normal assistant message before each pause.

## 3. Alternatives disproven

1. **The endpoint cannot do tool calls.** Disproven. The event log has many successful GLM tool calls in the same conversation: seq 43 shell, seq 45/47/49/51/53 file writes, and seq 57 shell. My live minimal probes also returned structured `tool_calls`.
2. **The pauses were caused by tool execution failures.** Disproven for seq 17 and seq 29: there is no post-approval tool execution before either pause. Seq 60 follows a real failure at seq 56, but seq 57-58 is a successful diagnostic action and seq 59 is a fresh tool-less no-op before the pause.
3. **The event store lost assistant rows.** Disproven by code and arithmetic. Blank/no-output steps are intentionally not persisted; `_invisible_steps` exists specifically because these steps are invisible to event-derived detectors.
4. **This was a context-window or output-length truncation path.** Not supported by the log. The truncation handler emits a "previous message was cut off" environment reminder before re-stepping; no such events appear before seq 17, 29, or 60. The live GLM empty probe also finished with `stop`, not `length`.
5. **The plan was complete and the loop should have finished.** Disproven. The actionless branch taken at seq 17/29/60 is gated on incomplete plan state. The planned file set was not complete at seq 17 or 29, and seq 59 explicitly says four JS modules remain.
6. **The SPEC's four-PAUSED premise is present in the snapshot.** Disproven by direct DB scan. The snapshot contains three `PAUSED/actionless` status events. The paired blocked-question statuses also carry actionless metadata, but those are `AWAITING_USER_QUESTION`, not additional PAUSED landings.

## 4. Minimal fix design (not implemented)

Do not lower the actionless breaker; it is doing its job. The minimal fix is to stop treating adapter-invisible reasoning-only GLM turns as ordinary no-ops:

1. In `OpenAIProvider.stream_complete`, when the final response has no accumulated `content`, no structured `tool_calls`, `finish_reason=="stop"`, and non-empty `reasoning_buf`, classify it as an empty reasoning-only driver turn.
2. Surface that classification to the agent/loop, either as a dedicated finish reason or response metadata.
3. In Build mode, retry that same driver step once with thinking disabled for this request (`chat_template_kwargs.enable_thinking=False`) or with a targeted system reminder: "Your previous response had no visible content and no tool call; call one tool now or produce visible text."
4. If the retry is still empty, persist a diagnostic environment message before counting the no-op. That keeps the event log self-explaining and eliminates the "3 responses but fewer artifacts" ambiguity for future incidents.
5. For GLM specifically, consider a catalogue/provider flag to disable thinking for agent-driver tool-call turns, or classify GLM-5.2 into an execution policy that enables this empty-reasoning repair without turning on every weak-model compensation.

## 5. Confidence

High confidence in the loop-level mechanism: the sequence numbers, no-op arithmetic, and code path line up exactly.

Medium confidence in the exact historical provider bytes for the two invisible turns before each pause, because the raw GLM responses were not persisted. The event log proves they were event-invisible no-work steps; the live probe proves GLM-5.2 can produce the adapter-invisible `stop` shape on the same endpoint.

The single experiment that would raise confidence to high on the model-side trigger is to add temporary raw driver telemetry for GLM-5.2 - per call: `finish_reason`, `content_len`, `reasoning_content_len`, and structured `tool_call_count` - then rerun/resume a comparable build until the first actionless pause. Seeing the missing turns as `stop/content_len=0/tool_call_count=0/reasoning_content_len>0` would close the last gap.
