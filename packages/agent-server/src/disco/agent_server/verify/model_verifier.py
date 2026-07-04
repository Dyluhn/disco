"""Model-judged verifier bounded context.

This layer is deliberately not an AgentLoop ``View``. It receives a
``VerifierContextSeed`` from core, makes one ``ModelRole.VERIFIER`` request, and
returns a typed verdict. The builder transcript never enters this prompt, and
the verifier transcript never goes back to the builder log.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from disco.core.events import LLMMessage
from disco.core.llm import (
    CallContext,
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
    OperatingMode,
    Requirement,
)
from disco.core.loop import TypedVerifierVerdict, VerifierContextSeed

_LOG = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are Disco's independent artifact verifier. You are not the builder. "
    "Judge only the bounded evidence provided: contract, deliverable paths, "
    "deterministic check results, and screenshot evidence if attached. Do not "
    "invent facts from missing builder history. Return only JSON with keys: "
    "verified, verdict, detail, failures, next_action, failure_fingerprint. "
    "Allowed verdict values: pass, fail, degraded, unavailable, unverifiable."
)


def _system_prompt_for_seed(seed: VerifierContextSeed) -> str:
    guidance = seed.medium.review_guidance if seed.medium is not None else ""
    if not guidance:
        return _SYSTEM_PROMPT
    return f"{_SYSTEM_PROMPT}\n\n{guidance}"


def _json_payload(seed: VerifierContextSeed) -> dict[str, Any]:
    payload = seed.model_dump(mode="json")
    if payload.get("medium") is None:
        payload.pop("medium", None)
    screenshot = dict(payload.get("screenshot") or {})
    if screenshot.get("image_data_url"):
        screenshot["image_attached"] = True
        screenshot["image_data_url"] = "[attached as message image]"
    payload["screenshot"] = screenshot
    return payload


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("verifier response was not a JSON object")
    return obj


def _normalise_verdict_payload(obj: dict[str, Any]) -> dict[str, Any]:
    out = dict(obj)
    label = str(out.get("verdict") or "").strip().lower()
    aliases = {
        "passed": "pass",
        "pass": "pass",
        "ok": "pass",
        "failed": "fail",
        "failure": "fail",
        "error": "fail",
        "fail": "fail",
        "degraded": "degraded",
        "unavailable": "unavailable",
        "unverifiable": "unverifiable",
    }
    out["verdict"] = aliases.get(label, "fail")
    if "verified" not in out:
        out["verified"] = out["verdict"] == "pass"
    out["verified"] = bool(out["verified"])
    out["detail"] = str(out.get("detail") or out.get("summary") or "")
    failures = out.get("failures")
    out["failures"] = failures if isinstance(failures, list) else []
    out["next_action"] = str(out.get("next_action") or "")
    out["failure_fingerprint"] = str(out.get("failure_fingerprint") or "")
    return out


class ModelVerifier:
    """LLM-backed verifier judge over a bounded seed."""

    def __init__(self, router: Any, *, conversation_id: str | None = None) -> None:
        self._router = router
        self._conversation_id = conversation_id

    async def judge(self, seed: VerifierContextSeed) -> TypedVerifierVerdict:
        payload = _json_payload(seed)
        images = [seed.screenshot.image_data_url] if seed.screenshot.image_data_url else None
        req = CompletionRequest(
            profile=CapabilityProfile(
                role=ModelRole.VERIFIER,
                requirements=frozenset({Requirement.JSON_MODE}),
                mode=OperatingMode.INTERACTIVE,
            ),
            messages=[
                LLMMessage(role="system", content=_system_prompt_for_seed(seed)),
                LLMMessage(
                    role="user",
                    content=json.dumps(payload, sort_keys=True),
                    images=images,
                ),
            ],
            tools=None,
            temperature=0.0,
            max_tokens=900,
            response_format="json",
            enable_thinking=False,
        )
        try:
            resp = await self._router.complete(
                req, context=CallContext(conversation_id=self._conversation_id)
            )
            obj = _normalise_verdict_payload(_extract_json_object(resp.text))
            return TypedVerifierVerdict.model_validate(obj)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("model verifier unavailable", exc_info=True)
            return TypedVerifierVerdict(
                verified=False,
                verdict="unavailable",
                detail=f"model verifier unavailable: {exc}",
                failures=[{"kind": "verifier_unavailable", "message": str(exc)}],
                next_action="",
                failure_fingerprint="model_verifier_unavailable",
            )


__all__ = ["ModelVerifier"]
