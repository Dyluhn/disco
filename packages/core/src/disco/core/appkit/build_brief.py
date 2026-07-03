"""AppKit EPIC B — the Build Brief: a deterministic (NON-LLM) classifier that
distills a free-text build request into a small, structured brief.

Why deterministic: the brief is shown to the user BEFORE submit (a read-only
preview card) and persisted as a hidden `<build_brief>` ENVIRONMENT message that
the model reads. A model call here would make the preview non-reproducible and
add latency/cost to the very first keystroke-to-submit path. So this is pure
rules — keyword/phrase matching only — and is byte-for-byte deterministic: the
same request always yields the same brief.

Drift guard: the (request → expected brief) GOLDEN FIXTURE in
`build_brief_golden.json` is consumed by BOTH the Python test AND the TS test
(`frontend/src/lib/buildBrief.ts`), so the two implementations can never diverge
silently. If you change a rule here, regenerate the fixture
(`python -m disco.core.appkit.build_brief --regen-golden`) and the TS classifier
must reproduce it.

Layering: `disco.core` is the leaf package (.importlinter) — this module imports
ONLY pydantic + the stdlib. No agent-server / runtime imports.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# ---- the model ----------------------------------------------------------------


class BuildBrief(BaseModel):
    """A small, structured distillation of a free-text build request.

    Every field is derived deterministically by `classify_build_brief`. The set is
    intentionally minimal-but-useful (P0): enough to frame the work and let the
    user sanity-check the agent's reading of the request, no more."""

    # The dominant kind of thing being built (one of _APP_KINDS, else "unknown").
    app_kind: str = "unknown"
    # The request itself, whitespace-collapsed and length-capped — the one-line goal.
    primary_goal: str = ""
    # Salient content words from the request (filler/stopwords removed), order-preserved.
    key_entities: list[str] = Field(default_factory=list)
    # Who the result is for (one of _AUDIENCES, else "general").
    audience: str = "general"
    # Cross-cutting capabilities/sections the request implies, in a fixed order.
    must_have_sections: list[str] = Field(default_factory=list)


# ---- deterministic classification ---------------------------------------------

_PRIMARY_GOAL_MAX = 200

# Tokens that carry no entity signal: function words + build-request filler. Kept
# IDENTICAL to the TS classifier's STOPWORDS so key_entities never drift.
_STOPWORDS: frozenset[str] = frozenset({
    # articles / conjunctions / prepositions / pronouns
    "a", "an", "the", "and", "or", "but", "for", "with", "without", "to", "of",
    "in", "on", "at", "by", "from", "as", "is", "are", "be", "am", "was", "were",
    "that", "this", "these", "those", "it", "its", "i", "me", "my", "mine", "we",
    "our", "us", "you", "your", "they", "them", "their", "he", "she", "his", "her",
    # build-request filler verbs / nouns
    "build", "builds", "building", "built", "make", "makes", "making", "made",
    "create", "creates", "creating", "created", "want", "wants", "wanting",
    "need", "needs", "needing", "would", "like", "please", "help", "let", "lets",
    "using", "use", "uses", "used", "via", "app", "apps", "application",
    "applications", "website", "websites", "web", "site", "sites", "tool", "tools",
    "program", "programs", "project", "projects", "thing", "things", "something",
    "some", "any", "can", "could", "should", "will", "shall", "do", "does", "did",
    "get", "gets", "got", "have", "has", "had", "add", "adds", "also", "then",
    "so", "just", "where", "which", "who", "what", "when", "how", "system",
})

# app_kind detection — FIRST match wins, so order = priority.
_APP_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("game", ("game", "puzzle", "snake", "tetris", "arcade", "platformer", "rpg", "pong", "quiz")),
    ("dashboard", ("dashboard", "analytics", "kpi", "metrics", "visualize", "visualization")),
    ("ecommerce", ("ecommerce", "e commerce", "shop", "store", "cart", "checkout", "storefront")),
    ("api", ("api", "rest", "endpoint", "microservice", "graphql", "webhook")),
    ("cli", ("cli", "command line", "terminal app", "command line tool")),
    ("mobile_app", ("mobile app", "ios app", "android app", "react native")),
    ("chat_app", ("chat", "messaging", "chatbot", "messenger")),
    ("blog", ("blog", "cms", "content site")),
    ("landing_page", ("landing page", "marketing page", "portfolio", "homepage")),
    ("data_tool", ("scraper", "crawler", "etl", "data pipeline", "parser")),
    ("web_app", ("web app", "website", "platform", "saas", "dashboard", "page")),
)
# Generic fallback signals → still "web_app" rather than "unknown".
_WEB_APP_FALLBACK: tuple[str, ...] = ("app", "site", "web", "platform", "page")

# audience detection — FIRST match wins.
_AUDIENCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("children", ("kid", "kids", "child", "children", "toddler")),
    ("students", ("student", "students", "school", "classroom", "teacher", "education")),
    ("developers", ("developer", "developers", "engineer", "engineers", "programmer", "technical")),
    ("business", ("business", "enterprise", "company", "team", "client", "clients", "b2b")),
    ("personal", ("personal", "myself", "my own", "hobby", "for me")),
)

# must_have_sections — fixed iteration order = stable output order.
_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("authentication", ("login", "log in", "signup", "sign up", "auth", "register", "password")),
    ("payments", ("payment", "payments", "checkout", "stripe", "billing", "subscription", "cart")),
    ("search", ("search", "filter", "filtering")),
    ("dashboard", ("dashboard", "analytics", "report", "reports", "charts", "graph", "graphs")),
    ("notifications", ("notification", "notifications", "alert", "alerts", "reminder", "email")),
    ("user_profiles", ("profile", "profiles", "user account", "settings")),
    ("database", ("database", "persist", "persistence", "storage", "crud", "save data")),
    ("realtime", ("realtime", "real time", "live update", "websocket", "websockets")),
    ("leaderboard", ("leaderboard", "high score", "scoreboard", "ranking")),
    ("uploads", ("upload", "uploads", "file upload", "attachment", "attachments")),
    ("admin", ("admin", "moderation", "management panel")),
)

_KEY_ENTITY_CAP = 8


def _normalize(request: str) -> str:
    """Lowercase, map every non-alphanumeric run to a single space, collapse, and
    pad with a leading/trailing space so phrase lookups are word-boundary safe.

    Kept byte-identical to the TS `normalize()` so both classifiers see the same
    string."""
    lowered = request.lower()
    spaced = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return f" {spaced} "


def _has(padded: str, keyword: str) -> bool:
    """Word-boundary-safe membership: the keyword (itself normalized) appears as
    whole word(s) in the padded, normalized request."""
    kw = re.sub(r"[^a-z0-9]+", " ", keyword.lower()).strip()
    return f" {kw} " in padded


def _key_entities(padded: str) -> list[str]:
    """Order-preserving, de-duplicated content words (len >= 3, not a stopword),
    capped. Operates on the normalized token stream so PY/TS agree exactly."""
    out: list[str] = []
    seen: set[str] = set()
    for tok in padded.split():
        if len(tok) < 3 or tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= _KEY_ENTITY_CAP:
            break
    return out


def classify_build_brief(request: str) -> BuildBrief:
    """Distill a free-text build request into a `BuildBrief` using rules ONLY.

    Deterministic by construction: same input ⇒ same output (no model call, no
    randomness, no time/locale dependence)."""
    padded = _normalize(request)

    app_kind = "unknown"
    for kind, keywords in _APP_KINDS:
        if any(_has(padded, kw) for kw in keywords):
            app_kind = kind
            break
    else:
        if any(_has(padded, kw) for kw in _WEB_APP_FALLBACK):
            app_kind = "web_app"

    audience = "general"
    for name, keywords in _AUDIENCES:
        if any(_has(padded, kw) for kw in keywords):
            audience = name
            break

    must_have_sections = [
        name for name, keywords in _SECTIONS if any(_has(padded, kw) for kw in keywords)
    ]

    primary_goal = re.sub(r"\s+", " ", request).strip()[:_PRIMARY_GOAL_MAX]

    return BuildBrief(
        app_kind=app_kind,
        primary_goal=primary_goal,
        key_entities=_key_entities(padded),
        audience=audience,
        must_have_sections=must_have_sections,
    )


# ---- golden fixture (shared with the TS classifier) ---------------------------

# The request corpus for the golden fixture. The expected briefs are GENERATED by
# `classify_build_brief` (run `--regen-golden`) so the fixture is always in sync
# with this module; the TS classifier is then held to the SAME fixture.
_GOLDEN_REQUESTS: tuple[str, ...] = (
    "Build me a snake game with a leaderboard and high score tracking",
    "Create a SaaS analytics dashboard for business teams with charts and reports",
    "I need a REST API with login and a Postgres database for storing orders",
    "Make an online store with a shopping cart, checkout and Stripe payments",
    "A simple personal blog where I can write posts and readers can search",
    "Build a chat app with real time messaging, user profiles and file uploads",
    "Make a fun math quiz for kids in a classroom with a scoreboard",
    "build a landing page portfolio for myself",
    "",
)


def _golden_cases() -> list[dict[str, object]]:
    return [
        {"request": r, "expected": classify_build_brief(r).model_dump()}
        for r in _GOLDEN_REQUESTS
    ]


if __name__ == "__main__":  # pragma: no cover - dev tooling
    import json
    import sys
    from pathlib import Path

    golden_path = Path(__file__).with_name("build_brief_golden.json")
    if "--regen-golden" in sys.argv:
        _ = golden_path.write_text(
            json.dumps({"cases": _golden_cases()}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {golden_path}")
    else:
        print(json.dumps({"cases": _golden_cases()}, indent=2, ensure_ascii=False))
