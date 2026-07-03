"""`design_lint` — the AST-light slop scanner (AppKit EPIC D3).

A generated web app betrays its LLM origin through a small, recurring set of
visual tells: Inter/Geist everywhere, an AI-purple primary, gradient-clipped hero
text, dark-neon-glow surfaces, pill-button monoculture, emoji-as-icons, animation
soup, and the centered-hero / three-cards / CTA section sequence. `design_lint`
SCANS the generated workspace (CSS + HTML/JSX/Vue/Svelte) plus the workspace's
`.disco/designspec.json` and returns a STRUCTURED verdict: a ranked list of
findings, each `{rule_id, severity, path, line, evidence, choice_key, message}`.

Two deliberate design choices:

* AST-light. No CSS/JS parser dependency (P0): rules are regex + string scanning
  over curated slop constants. Best-effort and conservative — a rule that can't be
  sure stays quiet rather than firing a false positive.
* Justification-aware, VALUE-AWARE SUPPRESSION. An off-default value is NOT
  automatically slop — it is slop only when it is UNJUSTIFIED. A finding is
  suppressed when `.disco/designspec.json` carries a SUBSTANTIVE justification for
  the finding's canonical `choice_key` (the same keys `recipes.to_design_spec`
  emits) AND — for the rules whose `choice_key` maps to a concrete DesignSpec
  field — the spec's ACTUAL field value equals the FLAGGED value. So justifying
  `palette.primary=#000000` does NOT excuse a `#7c3aed` finding, and justifying a
  Roboto heading does NOT excuse an Inter one: the justified value must BE the
  thing the implementation uses. A missing or invalid DesignSpec suppresses
  NOTHING — you cannot claim intent without the spec — so the relevant rules fire.

The pure engine (`lint_design`) takes an in-memory `{path: text}` map + an
optional `DesignSpec`; the `DesignLintTool` is a thin, READ-ONLY sandbox wrapper
that walks the workspace, loads the spec, and calls it. Read-only by contract so
Epic G can use it as a verification probe and re-linting is non-productive work.

Layering: tools -> core is allowed; this imports `DesignSpec` + the canonical
choice keys from `disco.core.appkit` (the single source of truth recipes write).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from disco.core import SecurityRisk
from disco.core.appkit import (
    CHOICE_COMPONENT_BUTTON_RADIUS,
    CHOICE_EFFECTS_GLOW,
    CHOICE_EFFECTS_GRADIENT_TEXT,
    CHOICE_ICONS_STYLE,
    CHOICE_LAYOUT_SECTION_SEQUENCE,
    CHOICE_MOTION_DENSITY,
    CHOICE_PALETTE_ACCENT,
    CHOICE_PALETTE_PRIMARY,
    CHOICE_TYPOGRAPHY_BODY,
    CHOICE_TYPOGRAPHY_HEADING,
    MAX_DESIGNSPEC_BYTES,
    load_design_spec_from_bytes,
)
from disco.core.appkit.spec import DesignSpec
from pydantic import BaseModel, ConfigDict, Field

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome

if TYPE_CHECKING:
    from ..sandbox.base import SandboxInstance

# The canonical choice keys design_lint can SUPPRESS a finding for. EVERY one is
# defined in core (`disco.core.appkit`) and imported above — there is NO local
# copy, so the linter's vocabulary and core's exported `CANONICAL_CHOICE_KEYS`
# can never drift (a test asserts this set is a subset of core's). Two of these
# keys map to a concrete DesignSpec FIELD whose value is matched against the
# flagged value (palette.primary/accent -> ai_purple; typography.heading/body ->
# generic_font); the rest name binary slop conditions with no spec field, so a
# substantive justification keyed to them is the suppression granularity.
SUPPRESSIBLE_CHOICE_KEYS: frozenset[str] = frozenset(
    {
        CHOICE_PALETTE_PRIMARY,
        CHOICE_PALETTE_ACCENT,
        CHOICE_TYPOGRAPHY_HEADING,
        CHOICE_TYPOGRAPHY_BODY,
        CHOICE_EFFECTS_GRADIENT_TEXT,
        CHOICE_EFFECTS_GLOW,
        CHOICE_COMPONENT_BUTTON_RADIUS,
        CHOICE_ICONS_STYLE,
        CHOICE_MOTION_DENSITY,
        CHOICE_LAYOUT_SECTION_SEQUENCE,
    }
)

_MIN_REASON_LEN = 12  # mirrors spec.Justification._reason_substantive

# ---- curated slop constants ---------------------------------------------------

# Median/default fonts — the LLM tell. Matched only in a FONT context (see rule).
_GENERIC_FONTS: frozenset[str] = frozenset(
    {
        "inter",
        "geist",
        "system-ui",
        "ui-sans-serif",
        "-apple-system",
        "blinkmacsystemfont",
        "sf pro",
        "sf pro text",
        "sf pro display",
        "segoe ui",
        "arial",
        "helvetica",
        "helvetica neue",
        "roboto",
    }
)

# The #7c3aed violet family (+ the indigo it's usually paired with). Hex only;
# a couple of rgb() spellings of the canonical purples too.
_AI_PURPLE_HEXES: frozenset[str] = frozenset(
    {
        "#7c3aed",
        "#8b5cf6",
        "#6d28d9",
        "#5b21b6",
        "#a855f7",
        "#9333ea",
        "#7e22ce",
        "#c084fc",
        "#6366f1",
        "#818cf8",
        "#4f46e5",
        "#4338ca",
        "#a78bfa",
    }
)
_AI_PURPLE_RGB: frozenset[str] = frozenset(
    {
        "rgb(124,58,237)",
        "rgb(139,92,246)",
        "rgb(99,102,241)",
        "rgb(168,85,247)",
    }
)

# Neon accents for the dark-neon-glow tell.
_NEON_HEXES: frozenset[str] = frozenset(
    {
        "#39ff14",
        "#00ff00",
        "#0f0",
        "#00ffff",
        "#0ff",
        "#ff00ff",
        "#f0f",
        "#fe53bb",
        "#08f7fe",
        "#ff2079",
        "#00f5d4",
        "#f5d300",
        "#ff073a",
        "#bc13fe",
        "#00ffea",
    }
)

# Pill (fully-rounded) radius values.
_PILL_RADII: tuple[str, ...] = ("9999px", "999px", "9999rem", "100vmax", "100vh", "50%")

_BUTTON_SELECTOR_TOKENS: tuple[str, ...] = (
    "button",
    ".btn",
    ".button",
    "[type=submit]",
    "[type='submit']",
    'btn',
)

_HEADING_SELECTOR_TOKENS: tuple[str, ...] = (
    "h1",
    "h2",
    "hero",
    "headline",
    "banner",
    "masthead",
    "display",
    "title",
)

# Broad emoji ranges (pictographs, symbols, transport, dingbats, supplemental).
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f000-\U0001f0ff"
    "\U00002b00-\U00002bff"
    "]"
)

_CSS_BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b")
_FONT_CONTEXT_RE = re.compile(r"font-family|font\s*:|--font|family=|googleapis\.com/css", re.I)

_ANIMATION_THRESHOLD = 12  # transition/animation declarations over this = soup
_EMOJI_THRESHOLD = 3  # this many emoji glyphs in markup = emoji-as-icons

# Extensions split by how we scan them.
_CSS_EXTS: frozenset[str] = frozenset({".css", ".scss", ".sass", ".less"})
_MARKUP_EXTS: frozenset[str] = frozenset(
    {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".astro", ".js", ".ts"}
)
SCAN_EXTS: frozenset[str] = _CSS_EXTS | _MARKUP_EXTS

# Directories never worth scanning (deps/build output/vcs/the .disco spec dir).
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".astro",
        "out",
        "coverage",
        "__pycache__",
        "vendor",
        ".disco",
        ".cache",
        ".vercel",
    }
)

# Walk / read caps so a hostile or huge workspace can't blow up the scan.
_MAX_FILES = 400
_MAX_DEPTH = 12
_MAX_FILE_BYTES = 512 * 1024
# Source files cap; the designspec cap is core's (`MAX_DESIGNSPEC_BYTES`) so the
# pre-read probe bounds against the SAME limit the loader enforces post-read.
_MAX_SPEC_BYTES = MAX_DESIGNSPEC_BYTES
# Small ceiling for the in-sandbox `wc -c` size probe — it only stats one file.
_SIZE_PROBE_TIMEOUT_S = 10


async def _bounded_read(
    sandbox: SandboxInstance, path: str, cap: int
) -> bytes | None:
    """Read a file ONLY if it is within `cap` bytes — and prove that BEFORE pulling
    any bytes into this process. We stat the size in-sandbox (`wc -c < file` streams
    the file through the guest's own `wc`; nothing lands in Python memory) and refuse
    to `read_file` an over-cap (or unmeasurable) file. A multi-GB hostile file is
    therefore never a memory/time bomb here, regardless of any post-read length check.

    Returns the file bytes when readable and within `cap`; None when the size can't
    be determined (non-zero exit / unparseable) or exceeds `cap` — the caller treats
    None as absent/oversized (a source file is skipped; the designspec is treated as
    present-but-invalid, so rules fire and nothing is suppressed)."""
    try:
        res = await sandbox.exec_shell(
            f"wc -c < {shlex.quote(path)}", timeout_s=_SIZE_PROBE_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001 — probe failed → treat as unmeasurable
        return None
    if res.exit_code != 0 or res.timed_out:
        return None
    try:
        size = int(res.stdout.strip())
    except (ValueError, AttributeError):
        return None
    if size < 0 or size > cap:
        return None
    try:
        return await sandbox.read_file(path)
    except Exception:  # noqa: BLE001 — vanished/unreadable between probe and read
        return None


# ---- finding model ------------------------------------------------------------


class DesignFinding(BaseModel):
    """One structured slop finding. `choice_key` is the canonical key whose
    justification (in `.disco/designspec.json`) would suppress it."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    severity: str  # "error" | "warning" | "info"
    path: str
    line: int
    evidence: str
    choice_key: str
    message: str


# rule_id -> (severity, rank-priority). Lower priority sorts first within a
# severity. Severity dominates the sort (error < warning < info).
_RULE_META: dict[str, tuple[str, int]] = {
    "ai_purple": ("error", 10),
    "generic_font": ("warning", 20),
    "gradient_hero_text": ("warning", 30),
    "dark_neon_glow": ("warning", 40),
    "centered_hero_3_cards_cta": ("warning", 50),
    "too_many_animations": ("info", 60),
    "pill_button_monoculture": ("info", 70),
    "emoji_as_icons": ("info", 80),
}
_SEVERITY_RANK: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


# ---- small scanning helpers ---------------------------------------------------


def _line_of(text: str, offset: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, max(offset, 0)) + 1


def _ext_of(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def _luminance(hexstr: str) -> float | None:
    """Relative luminance (0..1) of a #rgb/#rrggbb color, or None if unparseable."""
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        r = int(h[0:2], 16) / 255
        g = int(h[2:4], 16) / 255
        b = int(h[4:6], 16) / 255
    except ValueError:
        return None
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _norm_color(raw: str) -> str:
    """Lowercase + strip whitespace inside rgb()/hex so spellings collapse."""
    return re.sub(r"\s+", "", raw.strip().lower())


def _resolve_rgb(raw: str) -> tuple[int, int, int] | None:
    """Resolve a `#rgb`/`#rrggbb`/`rgb(r,g,b)` color to an (r,g,b) int tuple, or
    None if unparseable. Resolving to RGB lets a hex field value and an `rgb()`
    flagged value compare as the SAME color so suppression is spelling-agnostic
    (the DesignSpec palette is hex-only; the implementation may write either)."""
    s = _norm_color(raw)
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return None
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except ValueError:
            return None
    m = re.match(r"rgba?\((\d+),(\d+),(\d+)", s)
    if m is not None:
        try:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


# ---- suppression --------------------------------------------------------------


def _normalize_choice(raw: str) -> str:
    """Canonicalize a justification's `choice` so a recipe-written key and a
    hand-written one collapse. Lowercase, drop an optional `=value` suffix (the
    field+value form `palette.primary=#7c3aed`), and unify space/slash separators
    to '.'. Underscores are PRESERVED — they're meaningful inside the canonical
    keys design_lint owns (`effects.gradient_text`, `component.button_radius`,
    `layout.section_sequence`), so each constant normalizes to itself."""
    s = raw.strip().lower().split("=", 1)[0].strip()
    s = s.replace(" ", "").replace("/", ".")
    return s


def _justified_keys(design_spec: DesignSpec | None) -> set[str]:
    """The set of canonical choice keys SUBSTANTIVELY justified by the spec.

    A justification only counts if its reason clears the substantive-length bar
    (the same bar the DesignSpec schema enforces — re-checked here so a raw,
    hand-edited spec that slipped a stub reason past loading can't suppress)."""
    if design_spec is None:
        return set()
    out: set[str] = set()
    for j in design_spec.justifications:
        if len((j.reason or "").strip()) >= _MIN_REASON_LEN:
            out.add(_normalize_choice(j.choice))
    return out


def _font_justified(design_spec: DesignSpec | None, token: str, justified: set[str]) -> bool:
    """generic_font is suppressed only by a FIELD+VALUE match: the spec must
    actually DECLARE that exact font in heading/body AND justify the matching
    typography key. Declaring Inter without saying why never suppresses."""
    if design_spec is None:
        return False
    t = token.strip().lower()
    typ = design_spec.typography
    if t == typ.heading_font.strip().lower() and CHOICE_TYPOGRAPHY_HEADING in justified:
        return True
    if t == typ.body_font.strip().lower() and CHOICE_TYPOGRAPHY_BODY in justified:
        return True
    return False


# ---- the rules ----------------------------------------------------------------


def _rule_generic_font(
    path: str, text: str, design_spec: DesignSpec | None, justified: set[str]
) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    seen: set[str] = set()
    body_font = (
        design_spec.typography.body_font.strip().lower() if design_spec is not None else ""
    )
    for lineno, line in enumerate(text.splitlines(), 1):
        if not _FONT_CONTEXT_RE.search(line):
            continue
        # Only the PRIMARY (chosen) family is the tell — fallbacks like
        # `system-ui`/`sans-serif` after the real font are legitimate and must
        # not be flagged. Collect the chosen family from each declaration.
        for token in _primary_families(line):
            if token in seen or token not in _GENERIC_FONTS:
                continue
            seen.add(token)
            if _font_justified(design_spec, token, justified):
                continue
            choice = CHOICE_TYPOGRAPHY_BODY if token == body_font else CHOICE_TYPOGRAPHY_HEADING
            findings.append(
                DesignFinding(
                    rule_id="generic_font",
                    severity="warning",
                    path=path,
                    line=lineno,
                    evidence=f"font '{token}' in: {line.strip()[:140]}",
                    choice_key=choice,
                    message=(
                        f"Generic/default font '{token}' as the primary family — the LLM-median "
                        "tell. Declare a deliberate family in .disco/designspec.json and justify "
                        "it, or pick a real pairing (see SiteRecipe)."
                    ),
                )
            )
    return findings


def _primary_families(line: str) -> list[str]:
    """The CHOSEN (primary) font families declared on a line — the first family
    in each `font-family`/`--font*` declaration and every family imported via a
    Google-Fonts `family=` URL. Fallbacks after the primary are intentionally
    excluded (a `system-ui` fallback is not slop)."""
    low = line.lower()
    out: list[str] = []

    def _first(value: str) -> str:
        return value.split(",", 1)[0].strip().strip("'\"")

    for m in re.finditer(r"font-family\s*:\s*([^;{}]+)", low):
        out.append(_first(m.group(1)))
    for m in re.finditer(r"--[a-z0-9-]*font[a-z0-9-]*\s*:\s*([^;{}]+)", low):
        out.append(_first(m.group(1)))
    for m in re.finditer(r"family=([^&\"'<>;:]+)", low):
        for fam in re.split(r"[|]|&family=", m.group(1)):
            out.append(fam.split(":", 1)[0].replace("+", " ").strip())
    return [f for f in out if f]


def _justified_palette_rgbs(
    design_spec: DesignSpec | None, justified: set[str]
) -> set[tuple[int, int, int]]:
    """The resolved (r,g,b) of each palette ROLE the spec DECLARES *and*
    justifies. A purple finding is suppressed only if its color IS one of these —
    i.e. the spec's own primary/accent value is that exact violet. Justifying one
    color can never excuse a DIFFERENT one."""
    if design_spec is None:
        return set()
    out: set[tuple[int, int, int]] = set()
    pal = design_spec.palette
    if CHOICE_PALETTE_PRIMARY in justified:
        rgb = _resolve_rgb(pal.primary)
        if rgb is not None:
            out.add(rgb)
    if CHOICE_PALETTE_ACCENT in justified and pal.accent is not None:
        rgb = _resolve_rgb(pal.accent)
        if rgb is not None:
            out.add(rgb)
    return out


def _rule_ai_purple(
    path: str, text: str, design_spec: DesignSpec | None, justified: set[str]
) -> list[DesignFinding]:
    # VALUE-AWARE suppression: a flagged violet is suppressed ONLY when a
    # justified palette role (primary/accent) actually declares THAT exact color.
    justified_rgbs = _justified_palette_rgbs(design_spec, justified)
    findings: list[DesignFinding] = []
    low = text.lower()
    seen: set[str] = set()
    for m in re.finditer(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?|rgb\([^)]*\)", low):
        val = _norm_color(m.group(0))
        if val in seen:
            continue
        if val in _AI_PURPLE_HEXES or val in _AI_PURPLE_RGB:
            seen.add(val)
            flagged_rgb = _resolve_rgb(val)
            if flagged_rgb is not None and flagged_rgb in justified_rgbs:
                continue  # the spec declares + justifies THIS exact violet
            findings.append(
                DesignFinding(
                    rule_id="ai_purple",
                    severity="error",
                    path=path,
                    line=_line_of(low, m.start()),
                    evidence=m.group(0),
                    choice_key=CHOICE_PALETTE_PRIMARY,
                    message=(
                        f"AI-purple primary ({m.group(0)}) — the single most common generated "
                        "site tell. Pick a palette that suits the subject, or justify "
                        "palette.primary in .disco/designspec.json if the violet is intended."
                    ),
                )
            )
    return findings


def _rule_gradient_hero_text(
    path: str, text: str, justified: set[str]
) -> list[DesignFinding]:
    if CHOICE_EFFECTS_GRADIENT_TEXT in justified:
        return []
    findings: list[DesignFinding] = []
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).lower()
        body = m.group(2).lower()
        if not any(tok in selector for tok in _HEADING_SELECTOR_TOKENS):
            continue
        clipped = ("background-clip" in body and "text" in body) or (
            "-webkit-background-clip" in body and "text" in body
        )
        gradient = (
            "linear-gradient" in body
            or "conic-gradient" in body
            or "radial-gradient" in body
        )
        if clipped and gradient:
            findings.append(
                DesignFinding(
                    rule_id="gradient_hero_text",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=m.group(1).strip()[:120],
                    choice_key=CHOICE_EFFECTS_GRADIENT_TEXT,
                    message=(
                        "Gradient-clipped hero text (background-clip:text over a gradient on a "
                        "heading) — a generated-site cliche. Use a solid type treatment, or "
                        "justify effects.gradient_text in .disco/designspec.json."
                    ),
                )
            )
    return findings


def _rule_dark_neon_glow(
    path: str, text: str, justified: set[str]
) -> list[DesignFinding]:
    if CHOICE_EFFECTS_GLOW in justified:
        return []
    low = text.lower()
    # dark surface: a low-luminance hex on a root/body/app surface declaration
    dark = False
    _dark_re = r"(background[^;{}]*?|--bg[^;:]*?|--background[^;:]*?):[^;{}]*?(#[0-9a-f]{3,6})"
    for bm in re.finditer(_dark_re, low):
        lum = _luminance(bm.group(2))
        if lum is not None and lum < 0.22:
            dark = True
            break
    if not dark:
        return []
    # neon accent present
    neon_match: re.Match[str] | None = None
    for nm in re.finditer(r"#[0-9a-f]{3}(?:[0-9a-f]{3})?", low):
        if _norm_color(nm.group(0)) in _NEON_HEXES:
            neon_match = nm
            break
    if neon_match is None:
        return []
    # glow: a shadow/blur effect
    glow = bool(
        re.search(r"box-shadow|text-shadow|drop-shadow|filter\s*:[^;{}]*blur", low)
    )
    if not glow:
        return []
    return [
        DesignFinding(
            rule_id="dark_neon_glow",
            severity="warning",
            path=path,
            line=_line_of(low, neon_match.start()),
            evidence=neon_match.group(0),
            choice_key=CHOICE_EFFECTS_GLOW,
            message=(
                "Dark surface + neon accent + glow — the 'cyberpunk SaaS' generated look. Tone the "
                "accent and drop the glow, or justify effects.glow in .disco/designspec.json."
            ),
        )
    ]


def _rule_too_many_animations(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_MOTION_DENSITY in justified:
        return []
    low = text.lower()
    count = len(re.findall(r"\b(transition|animation)\s*:", low))
    count += len(re.findall(r"@keyframes\b", low))
    count += len(re.findall(r"\banimate-[a-z]", low))  # tailwind animate utilities
    if count <= _ANIMATION_THRESHOLD:
        return []
    return [
        DesignFinding(
            rule_id="too_many_animations",
            severity="info",
            path=path,
            line=1,
            evidence=f"{count} animation/transition declarations",
            choice_key=CHOICE_MOTION_DENSITY,
            message=(
                f"{count} animation/transition declarations (> {_ANIMATION_THRESHOLD}) — motion "
                "soup. Reserve motion for a few intentional moments, or justify motion.density."
            ),
        )
    ]


def _rule_pill_buttons(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_COMPONENT_BUTTON_RADIUS in justified:
        return []
    low = text.lower()
    pill = 0
    nonpill = 0
    first: tuple[int, str] | None = None
    # CSS: button-ish selector blocks with a border-radius
    for m in _CSS_BLOCK_RE.finditer(low):
        selector = m.group(1)
        body = m.group(2)
        if not any(tok in selector for tok in _BUTTON_SELECTOR_TOKENS):
            continue
        rm = re.search(r"border-radius\s*:\s*([^;{}]+)", body)
        if rm is None:
            continue
        value = rm.group(1).strip()
        if any(p in value for p in _PILL_RADII):
            pill += 1
            if first is None:
                first = (_line_of(low, m.start()), m.group(1).strip()[:80])
        else:
            nonpill += 1
    # Markup: tailwind rounded-full on button-ish elements
    for m in re.finditer(r'class\s*=\s*"([^"]*)"', low):
        cls = m.group(1)
        if ("btn" in cls or "button" in cls) and "rounded-full" in cls:
            pill += 1
            if first is None:
                first = (_line_of(low, m.start()), "rounded-full")
    if pill >= 2 and nonpill == 0 and first is not None:
        return [
            DesignFinding(
                rule_id="pill_button_monoculture",
                severity="info",
                path=path,
                line=first[0],
                evidence=first[1],
                choice_key=CHOICE_COMPONENT_BUTTON_RADIUS,
                message=(
                    "Every button is a full pill — a one-note component language. Vary the radius "
                    "by emphasis, or justify component.button_radius in .disco/designspec.json."
                ),
            )
        ]
    return []


def _rule_emoji_icons(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_ICONS_STYLE in justified:
        return []
    matches = _EMOJI_RE.findall(text)
    if len(matches) < _EMOJI_THRESHOLD:
        return []
    first = next(iter(_EMOJI_RE.finditer(text)))
    sample = "".join(dict.fromkeys(matches[:6]))
    return [
        DesignFinding(
            rule_id="emoji_as_icons",
            severity="info",
            path=path,
            line=_line_of(text, first.start()),
            evidence=f"{len(matches)} emoji used as icons (e.g. {sample})",
            choice_key=CHOICE_ICONS_STYLE,
            message=(
                "Emoji standing in for icons — reads as a placeholder. Use a real icon set, or "
                "justify icons.style in .disco/designspec.json."
            ),
        )
    ]


def _rule_centered_hero_3_cards_cta(
    markup: list[tuple[str, str]], justified: set[str]
) -> list[DesignFinding]:
    """Workspace-level section-sequence check across the markup files."""
    if CHOICE_LAYOUT_SECTION_SEQUENCE in justified:
        return []
    centering = ("text-center", "items-center", "justify-center", "mx-auto", "text-align:center")
    for path, text in markup:
        low = re.sub(r"\s+", " ", text.lower())
        # hero (centered)
        hero = re.search(r'(<section[^>]*|class\s*=\s*"[^"]*)hero', low)
        if hero is None:
            continue
        window = low[hero.start() : hero.start() + 400]
        if not any(c in window for c in centering):
            continue
        hero_idx = hero.start()
        # exactly three cards: a 3-col grid OR exactly three card-classed elements
        cards_idx: int | None = None
        grid = re.search(r"grid-cols-3|repeat\(3,|grid-template-columns\s*:\s*repeat\(\s*3", low)
        card_iter = list(re.finditer(r'class\s*=\s*"[^"]*card', low))
        if grid is not None and grid.start() > hero_idx:
            cards_idx = grid.start()
        elif len(card_iter) == 3 and card_iter[0].start() > hero_idx:
            cards_idx = card_iter[0].start()
        if cards_idx is None:
            continue
        # a CTA strictly AFTER the cards (search the tail so a "Get started" button
        # sitting INSIDE the hero can't be mistaken for the closing CTA).
        cta = re.search(
            r'(class\s*=\s*"[^"]*cta|<section[^>]*cta|get started|sign up|start free)',
            low[cards_idx:],
        )
        if cta is None:
            continue
        return [
            DesignFinding(
                rule_id="centered_hero_3_cards_cta",
                severity="warning",
                path=path,
                line=_line_of(text, 0),
                evidence="centered hero -> 3 feature cards -> CTA",
                choice_key=CHOICE_LAYOUT_SECTION_SEQUENCE,
                message=(
                    "The centered-hero / three-cards / CTA sequence is THE generated-landing-page "
                    "shape. Compose from the section-variant catalog instead, or justify "
                    "layout.section_sequence in .disco/designspec.json."
                ),
            )
        ]
    return []


# ---- the pure engine ----------------------------------------------------------


def lint_design(
    files: Mapping[str, str],
    design_spec: DesignSpec | None,
    *,
    spec_present: bool,
    spec_valid: bool,
) -> dict[str, Any]:
    """Scan an in-memory `{path: text}` map and return the structured verdict.

    `design_spec` is the parsed spec (None when absent OR invalid). `spec_present`
    / `spec_valid` are reported in the verdict so the agent knows WHY rules fired
    (a missing/invalid spec suppresses nothing — intent can't be claimed)."""
    justified = _justified_keys(design_spec)
    findings: list[DesignFinding] = []
    markup: list[tuple[str, str]] = []

    for path in sorted(files):
        text = files[path]
        ext = _ext_of(path)
        is_css = ext in _CSS_EXTS
        is_markup = ext in _MARKUP_EXTS
        if not (is_css or is_markup):
            continue
        if is_markup:
            markup.append((path, text))
        # rules that apply to any styled text (css + markup w/ inline styles)
        findings += _rule_generic_font(path, text, design_spec, justified)
        findings += _rule_ai_purple(path, text, design_spec, justified)
        findings += _rule_gradient_hero_text(path, text, justified)
        findings += _rule_dark_neon_glow(path, text, justified)
        findings += _rule_too_many_animations(path, text, justified)
        findings += _rule_pill_buttons(path, text, justified)
        if is_markup:
            findings += _rule_emoji_icons(path, text, justified)

    findings += _rule_centered_hero_3_cards_cta(markup, justified)

    findings.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(f.severity, 9),
            _RULE_META.get(f.rule_id, ("info", 999))[1],
            f.path,
            f.line,
        )
    )

    counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    ok = len(findings) == 0
    if ok:
        summary = (
            f"design_lint: clean — no slop across {len(files)} scanned file(s)."
            if files
            else "design_lint: no scannable files found."
        )
    else:
        rule_ids = [f.rule_id for f in findings]
        summary = (
            f"design_lint: {len(findings)} finding(s) "
            f"({counts['error']} error, {counts['warning']} warning, {counts['info']} info) — "
            f"{', '.join(dict.fromkeys(rule_ids))}."
        )

    return {
        "ok": ok,
        "findings": [f.model_dump() for f in findings],
        "counts": counts,
        "summary": summary,
        "scanned_files": len(files),
        "design_spec_present": spec_present,
        "design_spec_valid": spec_valid,
    }


def _render(verdict: dict[str, Any]) -> str:
    lines = [f"DESIGN_LINT: {'PASS' if verdict['ok'] else 'FINDINGS'}", verdict["summary"]]
    if not verdict["design_spec_present"]:
        lines.append("note: no .disco/designspec.json — off-default values can't be justified.")
    elif not verdict["design_spec_valid"]:
        lines.append("note: .disco/designspec.json is invalid — justifications ignored.")
    for f in verdict["findings"][:25]:
        lines.append(
            f"  [{f['severity']}] {f['rule_id']} ({f['choice_key']}) "
            f"{f['path']}:{f['line']} — {f['evidence']}"
        )
    return "\n".join(lines)


# ---- the tool wrapper ---------------------------------------------------------


class DesignLintArgs(BaseModel):
    root: str = Field(
        default=".",
        description="Workspace-relative directory to scan (defaults to the workspace root).",
    )


class DesignLintTool:
    """[CONTRACT boundary] READ-ONLY design-slop scanner. Walks the workspace's
    CSS + HTML/JSX and `.disco/designspec.json` and returns a structured verdict
    of ranked findings. Observes only — makes no change — so it is safe for the
    planner and counts as a verification probe (not productive work)."""

    definition = ToolDef(
        name="design_lint",
        description=(
            "Scan the generated web app for design SLOP (the LLM-median tells: Inter/Geist "
            "fonts, AI-purple primary, gradient-clipped hero text, dark-neon-glow, pill-button "
            "monoculture, emoji-as-icons, animation soup, centered-hero/3-cards/CTA) and return "
            "a STRUCTURED verdict — a ranked list of findings, each with rule_id, severity, path, "
            "line, evidence, the canonical choice_key, and a fix message. An off-default value is "
            "flagged ONLY if UNJUSTIFIED: a finding is suppressed when .disco/designspec.json "
            "carries a substantive justification for its choice_key. Read-only — run it to CHECK a "
            "build, not to change it. Scans the workspace root unless `root` is given."
        ),
        args_model=DesignLintArgs,
        needs=frozenset({Capability.FILESYSTEM}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,  # observes only — safe for the planner + a non-productive probe
    )

    async def run(self, args: DesignLintArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        try:
            root = (args.root or ".").strip() or "."
            files = await self._collect_files(ctx, root)
            design_spec, spec_present, spec_valid = await self._load_design_spec(ctx)
            verdict = lint_design(
                files,
                design_spec,
                spec_present=spec_present,
                spec_valid=spec_valid,
            )
            return ToolOutcome(
                success=True,  # the scan ran; pass/findings live in `structured`
                content=_render(verdict),
                structured=verdict,
            )
        except Exception as e:  # noqa: BLE001 — never crash the loop; report a scan error
            return ToolOutcome(success=False, content="", error=f"design_lint error: {e}")

    async def _collect_files(self, ctx: ToolContext, root: str) -> dict[str, str]:
        """Walk the workspace via the sandbox `list_dir`, reading only scannable
        files (bounded by file/depth/size caps). Directories are distinguished
        without relying on backend-specific error types: an entry whose name has
        a non-scannable extension is treated as a leaf file and skipped; an
        extension-less entry is probed as a directory (a failed `list_dir` →
        it was a leaf, skip it)."""
        assert ctx.sandbox is not None
        sandbox = ctx.sandbox
        files: dict[str, str] = {}

        async def walk(rel: str, depth: int) -> None:
            if depth > _MAX_DEPTH or len(files) >= _MAX_FILES:
                return
            try:
                entries = await sandbox.list_dir(rel)
            except Exception:  # noqa: BLE001 — unreadable/non-dir → nothing to scan here
                return
            for name in sorted(entries):
                if len(files) >= _MAX_FILES:
                    return
                # Skip dep/build dirs and ALL dotfiles/dotdirs (incl. `.disco`,
                # which is loaded separately as the spec — never scanned for slop).
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                child = name if rel in (".", "") else f"{rel.rstrip('/')}/{name}"
                ext = _ext_of(name)
                if ext in SCAN_EXTS:
                    # Size-check IN the sandbox before reading: an over-cap (or
                    # unmeasurable) source file is skipped without ever pulling its
                    # bytes into this process — no read-then-discard memory bomb.
                    data = await _bounded_read(sandbox, child, _MAX_FILE_BYTES)
                    if data is None:
                        continue
                    files[child] = data.decode("utf-8", errors="replace")
                elif "." not in name:
                    await walk(child, depth + 1)

        await walk(root, 0)
        return files

    async def _load_design_spec(
        self, ctx: ToolContext
    ) -> tuple[DesignSpec | None, bool, bool]:
        """Read + parse `.disco/designspec.json`. Returns (spec, present, valid).
        Absent → (None, False, False); present-but-bad → (None, True, False)."""
        assert ctx.sandbox is not None
        path = ".disco/designspec.json"
        try:
            exists = await ctx.sandbox.file_exists(path)
        except Exception:  # noqa: BLE001 — treat a probe failure as absent
            exists = False
        if not exists:
            return None, False, False
        # Size-check IN the sandbox BEFORE read: an over-cap (or unmeasurable)
        # `.disco/designspec.json` is never pulled into this process — `_bounded_read`
        # returns None, which we treat as present-but-invalid (rules fire, nothing
        # suppressed). A multi-GB hostile spec is therefore not a memory/time bomb.
        data = await _bounded_read(ctx.sandbox, path, _MAX_SPEC_BYTES)
        if data is None:
            return None, True, False
        try:
            # Defense-in-depth: the Epic C airtight loader re-enforces the same
            # `MAX_DESIGNSPEC_BYTES` cap before json.loads and validates the schema.
            spec = load_design_spec_from_bytes(data)
        except Exception:  # noqa: BLE001 — present but malformed/invalid
            return None, True, False
        return spec, True, True


__all__ = [
    "SUPPRESSIBLE_CHOICE_KEYS",
    "DesignFinding",
    "DesignLintArgs",
    "DesignLintTool",
    "lint_design",
]
