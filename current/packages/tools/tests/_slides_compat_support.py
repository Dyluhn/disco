"""Assertions shared by the retained legacy slide-path test IDs."""

from disco.tools.anatomy import ToolContext
from disco.tools.builtin.slides import SlidesGenerateArgs, SlidesTool


async def assert_legacy_markdown_refused(
    context: ToolContext, *, mode: str | None
) -> None:
    outcome = await SlidesTool().run(
        SlidesGenerateArgs(
            markdown="# Legacy",
            mode=mode,
            filename="legacy-mode",
            format="pptx",
        ),
        context,
    )
    assert (
        outcome.success,
        outcome.error,
        await context.sandbox.list_dir("."),
    ) == (False, "legacy_markdown_slide_path_disabled", [])
