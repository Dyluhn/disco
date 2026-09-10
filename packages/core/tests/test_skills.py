"""SkillStore — .md-file-backed reusable instruction modules (Claude-Code style)."""

from __future__ import annotations

from disco.core import Skill, SkillStore, render_skills_for_prompt, slugify


def test_slugify_is_filesystem_safe():
    assert slugify("Stock API Helper") == "stock-api-helper"
    assert slugify("  weird!!  chars??  ") == "weird-chars"
    assert slugify("") == "skill"


def test_create_persists_a_markdown_file(tmp_path):
    store = SkillStore(tmp_path)
    skill = store.create(
        name="Yahoo Finance",
        description="How to fetch stock data",
        body="Use the v8 chart endpoint with a Mozilla User-Agent.",
    )
    assert skill.id == "yahoo-finance"
    f = tmp_path / "yahoo-finance.md"
    assert f.is_file()
    text = f.read_text()
    assert "name: Yahoo Finance" in text
    assert "enabled: true" in text
    assert "v8 chart endpoint" in text


def test_round_trip_preserves_content(tmp_path):
    store = SkillStore(tmp_path)
    store.create(name="A", description="desc", body="line one\n\nline two")
    loaded = store.get("a")
    assert loaded is not None
    assert loaded.name == "A"
    assert loaded.description == "desc"
    assert loaded.body == "line one\n\nline two"
    assert loaded.enabled is True


def test_list_sorts_by_name_and_skips_missing_dir(tmp_path):
    # Missing dir → empty, not an error.
    assert SkillStore(tmp_path / "nope").list() == []
    store = SkillStore(tmp_path)
    store.create(name="Zebra")
    store.create(name="Apple")
    names = [s.name for s in store.list()]
    assert names == ["Apple", "Zebra"]


def test_create_dedupes_ids(tmp_path):
    store = SkillStore(tmp_path)
    a = store.create(name="Same Name")
    b = store.create(name="Same Name")
    assert a.id == "same-name"
    assert b.id == "same-name-2"
    assert len(store.list()) == 2


def test_save_overwrites_and_toggles_enabled(tmp_path):
    store = SkillStore(tmp_path)
    s = store.create(name="Toggle Me", body="x")
    store.save(s.model_copy(update={"enabled": False}))
    reloaded = store.get(s.id)
    assert reloaded is not None and reloaded.enabled is False
    assert reloaded.body == "x"  # body preserved across the toggle


def test_delete_removes_the_file(tmp_path):
    store = SkillStore(tmp_path)
    s = store.create(name="Ephemeral")
    assert store.delete(s.id) is True
    assert store.get(s.id) is None
    assert store.delete(s.id) is False  # second delete → False


def test_enabled_filters_disabled_skills(tmp_path):
    store = SkillStore(tmp_path)
    store.create(name="On", body="a", enabled=True)
    store.create(name="Off", body="b", enabled=False)
    enabled = store.enabled()
    assert [s.name for s in enabled] == ["On"]


def test_render_for_prompt_includes_only_enabled_with_body(tmp_path):
    skills = [
        Skill(id="a", name="A", description="da", body="do A", enabled=True),
        Skill(id="b", name="B", body="do B", enabled=False),  # disabled → excluded
        Skill(id="c", name="C", body="", enabled=True),  # empty body → excluded
    ]
    rendered = render_skills_for_prompt(skills)
    assert "## Skill: A" in rendered
    assert "do A" in rendered
    assert "Skill: B" not in rendered
    assert "Skill: C" not in rendered


def test_render_for_prompt_empty_when_no_enabled_skills():
    assert render_skills_for_prompt([]) == ""
    assert render_skills_for_prompt([Skill(id="x", name="X", enabled=False)]) == ""


def test_frontmatterless_file_is_tolerated(tmp_path):
    # A hand-dropped .md with no frontmatter still loads (body = whole file).
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / "raw.md").write_text("just some instructions, no frontmatter")
    store = SkillStore(tmp_path)
    s = store.get("raw")
    assert s is not None
    assert s.name == "raw"  # falls back to the stem
    assert "no frontmatter" in s.body
    assert s.enabled is True


# ---- security: path traversal must be refused -------------------------------


def test_get_refuses_path_traversal(tmp_path):
    store = SkillStore(tmp_path)
    store.create(name="Real")
    # None of these may escape the skills dir; all read as "not found".
    for bad in ["../../etc/passwd", "../secret", "..", "foo/bar", "/etc/hosts", "a/../../b"]:
        assert store.get(bad) is None


def test_delete_refuses_path_traversal(tmp_path):
    # Plant a file OUTSIDE the skills dir; a traversal delete must not touch it.
    victim = tmp_path / "victim.md"
    victim.write_text("important")
    skills_dir = tmp_path / "skills"
    store = SkillStore(skills_dir)
    store.create(name="Real")
    assert store.delete("../victim") is False
    assert victim.exists()  # untouched
    assert store.delete("../../victim") is False
    assert victim.exists()


def test_save_refuses_unsafe_id(tmp_path):
    import pytest

    store = SkillStore(tmp_path)
    with pytest.raises(ValueError):
        store.save(Skill(id="../escape", name="x", body="y"))


def test_body_size_is_capped(tmp_path):
    store = SkillStore(tmp_path)
    huge = "x" * (SkillStore.MAX_BODY_BYTES + 5000)
    s = store.create(name="Big", body=huge)
    assert len(s.body) == SkillStore.MAX_BODY_BYTES
    # Persisted file also reflects the cap.
    reloaded = store.get(s.id)
    assert reloaded is not None and len(reloaded.body) == SkillStore.MAX_BODY_BYTES


# ---- Cluster 4: lazy / path-scoped skill injection --------------------------


def test_unscoped_skills_always_render_full():
    from disco.core import Skill, render_skills_for_prompt

    skills = [Skill(id="a", name="House Style", body="Use TypeScript strict mode.", enabled=True)]
    out = render_skills_for_prompt(skills)  # no active_paths
    assert "House Style" in out
    assert "TypeScript strict mode" in out


def test_scoped_skill_shows_manifest_only_without_matching_path():
    from disco.core import Skill, render_skills_for_prompt

    skills = [
        Skill(
            id="css",
            name="CSS Rules",
            description="our spacing scale",
            body="Use the 8px grid everywhere.",
            scope="**/*.css",
            enabled=True,
        )
    ]
    out = render_skills_for_prompt(skills, active_paths=["src/App.tsx"])
    assert "CSS Rules" in out  # manifest entry present
    assert "applies to **/*.css" in out
    assert "8px grid" not in out  # full body NOT injected (no matching path)


def test_scoped_skill_shows_full_body_on_matching_path():
    from disco.core import Skill, render_skills_for_prompt

    skills = [
        Skill(
            id="css",
            name="CSS Rules",
            body="Use the 8px grid everywhere.",
            scope="**/*.css",
            enabled=True,
        )
    ]
    out = render_skills_for_prompt(skills, active_paths=["src/styles/main.css"])
    assert "8px grid" in out  # full body injected (path matches the scope glob)


def test_scope_round_trips_through_markdown(tmp_path):
    from disco.core import SkillStore

    store = SkillStore(tmp_path)
    store.create(name="Scoped", body="x")
    s = store.get("scoped")
    # default scope is empty
    assert s is not None and s.scope == ""
    # save with a scope, reload
    store.save(s.model_copy(update={"scope": "src/**/*.ts"}))
    reloaded = store.get("scoped")
    assert reloaded is not None and reloaded.scope == "src/**/*.ts"


# ---- per-surface scoping (a skill targets build / agent / both) -------------


def test_surfaces_round_trip_through_the_md_file(tmp_path):
    store = SkillStore(tmp_path)
    store.create(name="House Style", body="2-space indent", surfaces=["agent"])
    reloaded = store.list()[0]
    assert reloaded.surfaces == ["agent"]
    assert "surfaces: agent" in (tmp_path / f"{reloaded.id}.md").read_text()


def test_empty_surfaces_applies_everywhere_back_compat():
    s = Skill(id="x", name="Always", body="b", surfaces=[])
    assert s.applies_to_surface("build")
    assert s.applies_to_surface("agent")
    assert s.applies_to_surface(None)


def test_scoped_skill_only_applies_to_its_surfaces():
    agent_only = Skill(id="a", name="Style", body="b", surfaces=["agent"])
    assert agent_only.applies_to_surface("agent")
    assert not agent_only.applies_to_surface("build")
    # surface=None (caller with no surface in hand) → applies (back-compat)
    assert agent_only.applies_to_surface(None)


def test_render_filters_by_surface():
    skills = [
        Skill(id="a", name="House Style", body="indent 2", surfaces=["agent"]),
        Skill(id="b", name="Hourly Shot", body="screenshot", surfaces=["build"]),
        Skill(id="c", name="Always", body="global rule", surfaces=[]),
    ]
    agent_block = render_skills_for_prompt(skills, surface="agent")
    assert "House Style" in agent_block
    assert "Always" in agent_block
    assert "Hourly Shot" not in agent_block

    build_block = render_skills_for_prompt(skills, surface="build")
    assert "Hourly Shot" in build_block
    assert "Always" in build_block
    assert "House Style" not in build_block

    # No surface → no filter (every enabled skill), the back-compat default.
    all_block = render_skills_for_prompt(skills, surface=None)
    assert "House Style" in all_block and "Hourly Shot" in all_block and "Always" in all_block
