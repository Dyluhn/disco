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

from pydantic import BaseModel, ConfigDict, Field

from .primitives import HELLO_PRIMITIVE_ID, PrimitiveDefinition, register_primitive
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


register_primitive(
    PrimitiveDefinition(
        id=HELLO_PRIMITIVE_ID,
        default_app_spec=default_hello_app_spec,
        prepare_app_spec=prepare_hello_app_spec,
        generate=generate_hello,
        tier="fillable",
        host_contract=(),
        spec_schema=HelloSpec,
        verify=None,
        apply_spec=apply_hello_spec,
    )
)


__all__ = [
    "HELLO_PRIMITIVE_ID",
    "HelloSpec",
    "apply_hello_spec",
    "default_hello_app_spec",
    "generate_hello",
    "prepare_hello_app_spec",
]
