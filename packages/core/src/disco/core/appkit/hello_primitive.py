"""The AppKit `hello` primitive: the WO-A0 framework proof.

The SIMPLEST possible primitive — a single static `index.html` with a visible
heading. No Worker, no React, no entities. It exists to prove the extended
`PrimitiveDefinition` framework (tier/host_contract/spec_schema/verify) end to
end through the registry without touching any real vertical: registration,
`default_app_spec`, identity `prepare_app_spec`, and a mountable `generate`
tree, all with the WO-A0 defaults applied explicitly.
"""

from __future__ import annotations

import html

from .primitives import HELLO_PRIMITIVE_ID, PrimitiveDefinition, register_primitive
from .recipes import SiteRecipe
from .spec import AppSpec, DesignSpec, Page


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

    `design` is deliberately unused — see `default_hello_app_spec`.
    """
    del design
    title = html.escape(app.name)
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
        spec_schema=None,
        verify=None,
    )
)


__all__ = [
    "HELLO_PRIMITIVE_ID",
    "default_hello_app_spec",
    "generate_hello",
    "prepare_hello_app_spec",
]
