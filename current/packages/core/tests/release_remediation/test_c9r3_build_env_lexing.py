"""C9R3: build-env discovery follows executable JS/TS reads, not prose decoys."""

from __future__ import annotations

import pytest
from disco.core.release.detect import (
    _discovered_build_env_names,
    _unrepresentable_build_env_blocker,
)


def _names(source: str) -> set[str]:
    return _discovered_build_env_names({"src/main.ts": source})


def test_comments_and_inert_literals_do_not_invent_build_inputs() -> None:
    source = r"""
// import.meta.env.VITE_line_comment
/* import.meta.env.VITE_block_comment */
const docs = "import.meta.env.VITE_double_string";
const moreDocs = 'import.meta.env.VITE_single_string';
const templateDocs = `import.meta.env.VITE_template_text`;
const pattern = /import\.meta\.env\.VITE_regex_text/;
"""
    assert _names(source) == set()
    assert _unrepresentable_build_env_blocker({"src/main.ts": source}) is None


def test_documentation_and_markup_comments_do_not_invent_build_inputs() -> None:
    files = {
        "README.md": "Use import.meta.env.VITE_documented_name in your source.",
        "index.html": "<!-- import.meta.env.VITE_markup_comment -->",
    }
    assert _discovered_build_env_names(files) == set()


def test_real_reads_survive_lexing_including_template_expressions() -> None:
    source = r"""
const direct = import.meta.env.VITE_PUBLIC_TITLE;
const rendered = `title: ${import.meta.env.VITE_TEMPLATE_VALUE}`;
const afterUrl = "https://example.test/docs"; const later = import.meta.env.VITE_AFTER_STRING;
const quotient = left / right; const afterDivision = import.meta.env.VITE_AFTER_DIVISION;
"""
    assert _names(source) == {
        "VITE_AFTER_DIVISION",
        "VITE_AFTER_STRING",
        "VITE_PUBLIC_TITLE",
        "VITE_TEMPLATE_VALUE",
    }


def test_executable_mixed_case_read_still_fails_closed() -> None:
    files = {
        "src/main.ts": (
            "const docs = 'import.meta.env.VITE_documentation';\n"
            "const real = import.meta.env.VITE_Real_Input;\n"
        )
    }
    blocker = _unrepresentable_build_env_blocker(files)
    assert blocker is not None
    assert blocker.code == "build_env_unsupported_name"
    assert "VITE_Real_Input" in blocker.message
    assert "VITE_documentation" not in blocker.message


def test_regex_after_expression_keyword_is_not_a_build_input() -> None:
    source = r"""
function docsOnly() { return /import\.meta\.env\.VITE_regex_docs/; }
const actual = import.meta.env.VITE_ACTUAL;
"""
    assert _names(source) == {"VITE_ACTUAL"}


def test_deeply_nested_templates_are_bounded_and_keep_real_expression() -> None:
    nested = "import.meta.env.VITE_DEEP"
    for _ in range(1_500):
        nested = "`${" + nested + "}`"
    assert _names("const nested = " + nested + ";") == {"VITE_DEEP"}


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("index.html", "<main>import.meta.env.VITE_Doc_Only</main>"),
        ("src/view.jsx", "export default () => <p>import.meta.env.VITE_Doc_Only</p>;"),
        ("src/view.vue", "<template><code>import.meta.env.VITE_Doc_Only</code></template>"),
        ("src/view.svelte", "<p>import.meta.env.VITE_Doc_Only</p>"),
        ("src/view.astro", "---\nconst x = 1\n---\n<p>import.meta.env.VITE_Doc_Only</p>"),
        ("src/types.d.ts", "type T = typeof import.meta.env.VITE_Type_Only;"),
        ("src/types.d.mts", "type T = typeof import.meta.env.VITE_Type_Only;"),
        ("src/types.d.cts", "type T = typeof import.meta.env.VITE_Type_Only;"),
    ],
)
def test_markup_and_declaration_prose_do_not_become_build_inputs(path: str, source: str) -> None:
    assert _discovered_build_env_names({path: source}) == set()
    assert _unrepresentable_build_env_blocker({path: source}) is None


@pytest.mark.parametrize(
    "source",
    [
        "if (ok) /import.meta.env.VITE_Regex_Only/.test(value);",
        "const x = /import.meta.env.VITE_Regex_Only/;",
        "const x = () => /import.meta.env.VITE_Regex_Only/;",
        "if (ok) value(); else /import.meta.env.VITE_Regex_Only/.test(value);",
        "do /import.meta.env.VITE_Regex_Only/.test(value); while (ok);",
    ],
)
def test_regex_literals_in_common_expression_positions_are_ignored(source: str) -> None:
    assert _names(source) == set()


@pytest.mark.parametrize(
    "source",
    [
        "myimport.meta.env.VITE_NOT_IMPORT;",
        "root.import.meta.env.VITE_NOT_IMPORT;",
        "import.meta.env.VITE_WRONG$SUFFIX;",
    ],
)
def test_property_chain_requires_exact_identifier_boundaries(source: str) -> None:
    assert _names(source) == set()


@pytest.mark.parametrize(
    "source",
    [
        "const x = import /* comment */ . meta . env . VITE_REAL;",
        "const x = (import.meta.env).VITE_REAL;",
        "const x = import.meta.env?.VITE_REAL;",
        "const x = counter++ / import.meta.env.VITE_REAL / divisor;",
        "// decoy\rconst x = import.meta.env.VITE_REAL;",
    ],
)
def test_ordinary_executable_reference_variants_are_conserved(source: str) -> None:
    assert _names(source) == {"VITE_REAL"}


@pytest.mark.parametrize(
    ("path", "source"),
    [
        (
            "index.html",
            '<script type="module">const x = import.meta.env.VITE_REAL</script>',
        ),
        ("src/view.jsx", "export default () => <p>{import.meta.env.VITE_REAL}</p>;"),
        ("src/view.tsx", "export default () => <P value={import.meta.env.VITE_REAL} />;"),
        ("src/view.vue", "<template>{{ import.meta.env.VITE_REAL }}</template>"),
        ("src/view.svelte", "<p>{import.meta.env.VITE_REAL}</p>"),
        (
            "src/view.astro",
            "---\nconst x = import.meta.env.VITE_REAL\n---\n<p>{x}</p>",
        ),
    ],
)
def test_framework_executable_regions_remain_visible(path: str, source: str) -> None:
    assert _discovered_build_env_names({path: source}) == {"VITE_REAL"}


def test_vue_directive_expression_is_executable_but_comments_and_styles_are_not() -> None:
    source = """
<template>
  <!-- { import.meta.env.VITE_Comment_Only } -->
  <main :title="import.meta.env.VITE_REAL">ok</main>
</template>
<style>.x { content: 'import.meta.env.VITE_Style_Only' }</style>
"""
    assert _discovered_build_env_names({"src/view.vue": source}) == {"VITE_REAL"}


def test_jsx_fragment_prose_is_not_code_but_fragment_expression_is() -> None:
    source = """
export default () => <>
  import.meta.env.VITE_Fragment_Prose
  <span>{import.meta.env.VITE_REAL}</span>
</>;
"""
    assert _discovered_build_env_names({"src/view.tsx": source}) == {"VITE_REAL"}


@pytest.mark.parametrize(
    "source",
    [
        "debugger\n/import.meta.env.VITE_REGEX_ONLY/.test(value)",
        "while (ok) { break\n/import.meta.env.VITE_REGEX_ONLY/.test(value) }",
        "while (ok) { continue\n/import.meta.env.VITE_REGEX_ONLY/.test(value) }",
    ],
)
def test_regex_after_asi_statement_keywords_is_not_a_build_input(source: str) -> None:
    assert _names(source) == set()


def test_division_after_object_literal_keeps_real_build_input_visible() -> None:
    assert _names("const ratio = {value: 1} / import.meta.env.VITE_DIVISOR / 2") == {"VITE_DIVISOR"}


@pytest.mark.parametrize(
    "source",
    [
        "const x = myimport.meta.env.VITE_NOT_REAL",
        "const x = import.meta.env.VITE_NOT_REALé",
        "const x = λimport.meta.env.VITE_NOT_REAL",
    ],
)
def test_unicode_identifier_continuations_cannot_create_partial_chain(source: str) -> None:
    assert _names(source) == set()


@pytest.mark.parametrize(
    "source",
    [
        "const x = ((import.meta)).env.VITE_REAL",
        "const x = ((import.meta).env).VITE_REAL",
        "const x = (import.meta)?.env.VITE_REAL",
        "const x = import.meta?.env?.VITE_REAL",
    ],
)
def test_grouped_and_optional_vite_property_chains_are_conserved(source: str) -> None:
    assert _names(source) == {"VITE_REAL"}


def test_nested_svelte_expression_is_executable() -> None:
    source = "<p>{ok ? ({value: import.meta.env.VITE_REAL}).value : 'none'}</p>"
    assert _discovered_build_env_names({"src/view.svelte": source}) == {"VITE_REAL"}


def test_script_text_inside_markup_comment_is_not_executable() -> None:
    source = "<!-- <script>const x = import.meta.env.VITE_NOT_REAL</script> -->"
    assert _discovered_build_env_names({"index.html": source}) == set()


def test_malformed_large_jsx_is_scanned_in_linear_time_without_false_input() -> None:
    # Regression for the former `<...>` regex's quadratic backtracking path.
    source = "export default () => " + "<A" * 50_000
    assert _discovered_build_env_names({"src/view.tsx": source}) == set()


@pytest.mark.parametrize(
    "source",
    [
        "const x = obj.return / import.meta.env.VITE_REAL / 2",
        "const x = obj.debugger / import.meta.env.VITE_REAL / 2",
        "const x = function(){} / import.meta.env.VITE_REAL / 2",
        "const x = class {} / import.meta.env.VITE_REAL / 2",
    ],
)
def test_property_keywords_and_expression_bodies_do_not_hide_division_reads(
    source: str,
) -> None:
    assert _names(source) == {"VITE_REAL"}


def test_top_level_block_followed_by_regex_does_not_invent_input() -> None:
    assert _names("{a: 1}\n/import.meta.env.VITE_NOT_REAL/.test(value)") == set()


@pytest.mark.parametrize(
    "expression",
    [
        "ok ? (/* } */ import.meta.env.VITE_REAL) : fallback",
        "ok ? (/}/.test(value) && import.meta.env.VITE_REAL) : fallback",
    ],
)
def test_svelte_brace_matching_ignores_comment_and_regex_braces(expression: str) -> None:
    assert _discovered_build_env_names({"src/view.svelte": "<p>{" + expression + "}</p>"}) == {
        "VITE_REAL"
    }


def test_vite_identifier_unicode_escape_is_conserved() -> None:
    assert _names(r"const x = import.meta.env.VITE_\u0058") == {"VITE_X"}


@pytest.mark.parametrize(
    "source",
    [
        "<!--" * 25_000,
        "<script " * 25_000,
    ],
)
def test_large_malformed_markup_is_bounded_and_inert(source: str) -> None:
    assert _discovered_build_env_names({"index.html": source}) == set()


@pytest.mark.parametrize(
    "script_tag",
    [
        "<script>",
        '<script type="text/javascript">',
        '<script type="text/plain">',
        '<script type="application/json">',
        '<script type="application/ld+json">',
        '<script type="importmap">',
        '<script type="speculationrules">',
    ],
)
def test_html_nonmodule_scripts_do_not_invent_vite_build_inputs(script_tag: str) -> None:
    source = f"{script_tag}const x=import.meta.env.VITE_NOT_REAL</script>"
    assert _discovered_build_env_names({"index.html": source}) == set()


@pytest.mark.parametrize("path", ["src/view.vue", "src/view.svelte", "src/view.astro"])
def test_component_default_scripts_remain_executable(path: str) -> None:
    source = "<script>const x=import.meta.env.VITE_REAL</script>"
    assert _discovered_build_env_names({path: source}) == {"VITE_REAL"}


@pytest.mark.parametrize("decoy", ["</scripture>", "</script-x>", "</scripty>"])
def test_html_script_closing_tag_requires_a_name_boundary(decoy: str) -> None:
    source = (
        f'<script type="module">const decoy={decoy!r}; const x=import.meta.env.VITE_REAL</script>'
    )
    assert _discovered_build_env_names({"index.html": source}) == {"VITE_REAL"}


@pytest.mark.parametrize(
    "source",
    [
        '<p :[import.meta.env.VITE_REAL]="value" />',
        '<p v-bind:[import.meta.env.VITE_REAL]="value" />',
        '<p @[import.meta.env.VITE_REAL]="handler" />',
        '<template #[import.meta.env.VITE_REAL]="slotProps" />',
        "<p :title=import.meta.env.VITE_REAL />",
    ],
)
def test_vue_dynamic_and_unquoted_directive_expressions_are_executable(source: str) -> None:
    assert _discovered_build_env_names({"src/view.vue": source}) == {"VITE_REAL"}


@pytest.mark.parametrize(
    "opening",
    [
        "<script is:inline>",
        '<script type="module">',
        "<script data-purpose=inline>",
    ],
)
def test_astro_attributed_inline_scripts_do_not_invent_bundled_inputs(opening: str) -> None:
    source = f"{opening}const x=import.meta.env.VITE_NOT_REAL</script>"
    assert _discovered_build_env_names({"src/view.astro": source}) == set()


@pytest.mark.parametrize(
    "source",
    [
        "<code>:[import.meta.env.VITE_NOT_REAL]</code>",
        "<code>:title=import.meta.env.VITE_NOT_REAL</code>",
        '<p data-doc=":[import.meta.env.VITE_NOT_REAL]">ok</p>',
        '<p title=":title=import.meta.env.VITE_NOT_REAL">ok</p>',
    ],
)
def test_vue_directive_like_prose_and_attribute_data_are_inert(source: str) -> None:
    assert _discovered_build_env_names({"src/view.vue": source}) == set()


@pytest.mark.parametrize(
    "source",
    [
        "<div v-pre>{{ import.meta.env.VITE_NOT_REAL }}</div>",
        '<div v-pre :title="import.meta.env.VITE_NOT_REAL"></div>',
        '<div v-pre :[import.meta.env.VITE_NOT_REAL]="value"></div>',
        "<section v-pre><div><span>{{ import.meta.env.VITE_NOT_REAL }}</span></div></section>",
    ],
)
def test_vue_v_pre_subtrees_are_not_compiled_build_inputs(source: str) -> None:
    assert _discovered_build_env_names({"src/view.vue": source}) == set()


def test_vue_sibling_after_v_pre_remains_executable() -> None:
    source = (
        "<div v-pre>{{ import.meta.env.VITE_NOT_REAL }}</div>"
        '<p :title="import.meta.env.VITE_REAL"></p>'
    )
    assert _discovered_build_env_names({"src/view.vue": source}) == {"VITE_REAL"}


@pytest.mark.parametrize("tag", ["input", "br", "img"])
def test_vue_v_pre_html_void_elements_are_self_closing(tag: str) -> None:
    source = f'<{tag} v-pre :title="import.meta.env.VITE_NOT_REAL">'
    assert _discovered_build_env_names({"src/view.vue": source}) == set()


def test_many_vue_v_pre_regions_remain_bounded_and_inert() -> None:
    source = '<input v-pre :title="import.meta.env.VITE_NOT_REAL">' * 2_500
    assert _discovered_build_env_names({"src/view.vue": source}) == set()


@pytest.mark.parametrize(
    "source",
    [
        "export default()=> <><A>docs</A><B>{import.meta.env.VITE_REAL}</B></>",
        "export default()=> <main><A>docs</A> <B>{import.meta.env.VITE_REAL}</B></main>",
        "export default()=> <><A>docs</A><B x={import.meta.env.VITE_REAL}/></>",
    ],
)
def test_compact_jsx_sibling_expressions_remain_visible(source: str) -> None:
    assert _discovered_build_env_names({"src/view.tsx": source}) == {"VITE_REAL"}


def test_jsx_quoted_attribute_braces_remain_inert() -> None:
    source = "export default()=> <P title='{import.meta.env.VITE_NOT_REAL}' />"
    assert _discovered_build_env_names({"src/view.tsx": source}) == set()


@pytest.mark.parametrize(
    "source",
    [
        '<P>{"}" + import.meta.env.VITE_REAL}</P>',
        "<P>{/}/.test(x) && import.meta.env.VITE_REAL}</P>",
        '<P x={{s:"}}",v:import.meta.env.VITE_REAL}}/>',
        '<P x={`${"}"}${import.meta.env.VITE_REAL}`}/>',
        "<P x={{/* }} */v:import.meta.env.VITE_REAL}}/>",
        '<P>{{s:"}}",v:import.meta.env.VITE_REAL}.v}</P>',
    ],
)
def test_jsx_expression_braces_use_javascript_lexing(source: str) -> None:
    assert _discovered_build_env_names({"src/view.tsx": source}) == {"VITE_REAL"}


def test_jsx_quoted_attribute_with_comment_and_regex_braces_remains_inert() -> None:
    source = '<P title="{/* } */ /}/ import.meta.env.VITE_NOT_REAL}" />'
    assert _discovered_build_env_names({"src/view.tsx": source}) == set()


def test_jsx_render_prop_with_nested_element_keeps_real_input_visible() -> None:
    source = "export default()=> <Root render={()=> <P>{import.meta.env.VITE_REAL}</P>} />"
    assert _discovered_build_env_names({"src/view.tsx": source}) == {"VITE_REAL"}


def test_malformed_jsx_expression_makes_bounded_forward_progress() -> None:
    source = "export default()=> <Root render={" + ("<Broken " * 20_000)
    assert _discovered_build_env_names({"src/view.tsx": source}) == set()
