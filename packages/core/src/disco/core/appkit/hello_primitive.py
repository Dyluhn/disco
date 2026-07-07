"""The AppKit `hello` primitive: the WO-A0 framework proof.

The SIMPLEST possible primitive — a single static `index.html` with a visible
heading. No Worker, no React, no entities. It exists to prove the extended
`PrimitiveDefinition` framework end to end through the registry without touching
any real vertical: registration, `default_app_spec`, identity `prepare_app_spec`,
and a mountable `generate` tree (WO-A0), plus the fill-and-validate `spec_schema`
+ `apply_spec` pair that makes it ADDABLE via `app_add_primitive` (WO-A1): the
validated `HelloSpec.headline` folds into the first page's title and renders as
the page subtitle.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .primitives import (
    HELLO_PRIMITIVE_ID,
    PrimitiveDefinition,
    PrimitiveVerifyResult,
    VerifyCheck,
    register_primitive,
)
from .recipes import SiteRecipe
from .spec import AppSpec, DesignSpec, Page


class HelloSpec(BaseModel):
    """The hello primitive's declarative spec — the WO-A1 fill-and-validate proof.

    One bounded slot: `headline` becomes the home page's title (rendered as the
    page subtitle). `extra="forbid"` so an unknown key is a PRECISE model-facing
    error, not a silent drop."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(
        min_length=1,
        max_length=120,
        description="The home-page headline (1-120 chars); rendered as the subtitle.",
    )


def apply_hello_spec(app: AppSpec, spec: BaseModel) -> AppSpec:
    """Fold a validated HelloSpec into the AppSpec: the headline becomes the first
    page's title. Same house dance as the app tools: dump → mutate → re-validate."""
    if not isinstance(spec, HelloSpec):  # defensive: app_add_primitive validated it
        raise TypeError(f"apply_spec for {HELLO_PRIMITIVE_ID!r} needs a HelloSpec")
    data = app.model_dump(mode="json")
    if data["pages"]:
        data["pages"][0] = {**data["pages"][0], "title": spec.headline}
    else:
        data["pages"] = [{"id": "home", "route": "/", "title": spec.headline}]
    return AppSpec.model_validate(data)


def default_hello_app_spec(name: str, recipe: SiteRecipe) -> AppSpec:
    """The simplest valid hello AppSpec: one home page, no entities, no actions.

    `recipe` is deliberately unused — a static heading has no design-preference
    surface (the parameter exists to satisfy the `PrimitiveDefinition` contract).
    """
    del recipe
    title = name.strip() or "Hello"
    return AppSpec(
        schema_version=1,
        app_kind=HELLO_PRIMITIVE_ID,
        name=title,
        pages=(Page(id="home", route="/", title="Home"),),
    )


def prepare_hello_app_spec(app: AppSpec) -> AppSpec:
    """Identity — the hello primitive needs no normalization."""
    return app


def generate_hello(app: AppSpec, design: DesignSpec) -> dict[str, str]:
    """Lower a hello AppSpec into the simplest mountable tree: one static page.

    The first page's title renders as a `<p>` subtitle under the heading — that is
    how a folded HelloSpec (`app_add_primitive`) becomes VISIBLE in the output.
    `design` is deliberately unused — see `default_hello_app_spec`.
    """
    del design
    title = html.escape(app.name)
    subtitle = html.escape(app.pages[0].title) if app.pages else ""
    subtitle_line = f"    <p>{subtitle}</p>\n" if subtitle else ""
    return {
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="utf-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
            f"    <title>{title}</title>\n"
            "  </head>\n"
            "  <body>\n"
            f"    <h1>Hello from the hello primitive — {title}</h1>\n"
            f"{subtitle_line}"
            "  </body>\n"
            "</html>\n"
        )
    }


def hello_verify(
    app: AppSpec | None, design: DesignSpec | None, tree: Mapping[str, str]
) -> PrimitiveVerifyResult:
    """The hello primitive's verify hook (WO-A3 proof): `index.html` is in the tree,
    the `<h1>` carries the escaped app name, and — when pages exist — the `<p>`
    subtitle equals the escaped first page's title (how a folded `HelloSpec`
    headline becomes VISIBLE, see `generate_hello`). Pure: reads only `tree`."""
    del design  # a static heading has no design surface — see default_hello_app_spec
    checks: list[VerifyCheck] = []

    index_html = tree.get("index.html")
    checks.append(
        VerifyCheck(
            "hello_index_present",
            index_html is not None,
            "index.html is present in the workspace tree."
            if index_html is not None
            else "index.html is missing from the workspace — run app_create first.",
        )
    )

    src = index_html or ""
    h1 = re.search(r"<h1>(.*?)</h1>", src, re.DOTALL)
    if app is None:
        checks.append(
            VerifyCheck(
                "hello_headline",
                False,
                "no .disco/appspec.json — run app_create first.",
            )
        )
    else:
        title = html.escape(app.name)
        headline_ok = h1 is not None and title in h1.group(1)
        checks.append(
            VerifyCheck(
                "hello_headline",
                headline_ok,
                f"the <h1> carries the escaped app name ({title!r})."
                if headline_ok
                else f"index.html has no <h1> carrying the escaped app name ({title!r}).",
            )
        )

    if app is not None and app.pages:
        expected = html.escape(app.pages[0].title)
        p = re.search(r"<p>(.*?)</p>", src, re.DOTALL)
        subtitle_ok = p is not None and p.group(1) == expected
        checks.append(
            VerifyCheck(
                "hello_subtitle",
                subtitle_ok,
                f"the <p> subtitle equals the escaped first page title ({expected!r})."
                if subtitle_ok
                else (
                    f"the <p> subtitle does not equal the escaped first page title "
                    f"({expected!r}); found {p.group(1)!r}."
                    if p is not None
                    else f"index.html has no <p> subtitle (expected {expected!r})."
                ),
            )
        )

    n_fail = sum(1 for c in checks if not c.passed)
    return PrimitiveVerifyResult(
        ok=n_fail == 0,
        detail=f"{len(checks) - n_fail} passed / {n_fail} failed",
        checks=tuple(checks),
    )


register_primitive(
    PrimitiveDefinition(
        id=HELLO_PRIMITIVE_ID,
        default_app_spec=default_hello_app_spec,
        prepare_app_spec=prepare_hello_app_spec,
        generate=generate_hello,
        tier="fillable",
        host_contract=(),
        spec_schema=HelloSpec,
        verify=hello_verify,
        apply_spec=apply_hello_spec,
    )
)


__all__ = [
    "HELLO_PRIMITIVE_ID",
    "HelloSpec",
    "apply_hello_spec",
    "default_hello_app_spec",
    "generate_hello",
    "hello_verify",
    "prepare_hello_app_spec",
]
