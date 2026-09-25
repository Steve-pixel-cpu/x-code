"""skills.py 单元测试: frontmatter 解析 / 两级发现去重 / 清单渲染 /
skill_read 工具（含路径安全）/ TOOLS 原地同步。安装流程的 git clone
端到端放在 test_skills_api（用本地真仓库）。"""

import json

import pytest

import skills
from skills import (Skill, SkillError, delete_user_skill, discover_skills,
                    parse_skill_md, register_skill_tools, render_skills_section,
                    sanitize_name, skill_info, sync_skill_tools,
                    _make_skill_read_handler)
from tools import ToolRegistry, ToolError


def make_skill(tmp_path, name="demo", source="user", body="Do the thing.",
               description="A demo skill", extra_files=None):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    meta = {"name": name, "description": description}
    fm = "---\n" + "\n".join(f"{k}: {v}" for k, v in meta.items()) + "\n---\n"
    (d / "SKILL.md").write_text(fm + body, encoding="utf-8")
    for fname, content in (extra_files or {}).items():
        f = d / fname
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return d


# --- sanitize_name ---

def test_sanitize_name():
    assert sanitize_name("pdf-tools") == "pdf-tools"
    assert sanitize_name("My Skill v2!") == "My-Skill-v2"   # 尾部 '-' 被去掉
    assert sanitize_name("  ") == ""
    assert sanitize_name("中文技能") == ""   # 全非法字符 → 空名, 发现/安装层跳过


# --- parse_skill_md ---

def test_parse_full_frontmatter(tmp_path):
    d = make_skill(tmp_path, name="parser", description="Parses things",
                   body="# Steps\n1. parse\n")
    s = parse_skill_md(d, "user")
    assert s.name == "parser"
    assert s.description == "Parses things"
    assert s.source == "user"
    assert s.body.startswith("# Steps")
    assert s.body.strip().endswith("parse")


def test_parse_name_defaults_to_dirname(tmp_path):
    d = tmp_path / "fallback-name"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\ndescription: no name field\n---\nbody", encoding="utf-8")
    s = parse_skill_md(d, "project")
    assert s.name == "fallback-name"


def test_parse_without_frontmatter(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "SKILL.md").write_text("Just markdown, no frontmatter.", encoding="utf-8")
    s = parse_skill_md(d, "user")
    assert s.name == "plain"
    assert s.description == ""
    assert "Just markdown" in s.body


def test_parse_invalid_frontmatter_raises(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\n: : {unbalanced [brackets\n---\nbody", encoding="utf-8")
    with pytest.raises(SkillError):
        parse_skill_md(d, "user")


# --- discover_skills ---

def test_discover_user_and_project(tmp_path):
    user_dir = tmp_path / "home"
    cwd = tmp_path / "proj"
    make_skill(user_dir / "skills", name="user-skill")
    make_skill(cwd / ".claude" / "skills", name="proj-skill", source="project")

    found = {s.name: s for s in discover_skills(cwd, user_dir)}
    assert set(found) == {"user-skill", "proj-skill"}
    assert found["user-skill"].source == "user"
    assert found["proj-skill"].source == "project"


def test_project_overrides_user_same_name(tmp_path):
    user_dir = tmp_path / "home"
    cwd = tmp_path / "proj"
    make_skill(user_dir / "skills", name="same",
               description="user version")
    make_skill(cwd / ".claude" / "skills", name="same",
               description="project version", source="project")

    found = discover_skills(cwd, user_dir)
    assert len(found) == 1
    assert found[0].description == "project version"
    assert found[0].source == "project"


def test_discover_skips_and_reports_broken(tmp_path):
    user_dir = tmp_path / "home"
    make_skill(user_dir / "skills", name="good")
    bad = user_dir / "skills" / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\n: : {\n---\nx", encoding="utf-8")
    empty = user_dir / "skills" / "empty-dir"
    empty.mkdir()   # 没有 SKILL.md: 静默跳过, 不算错误

    errors = []
    found = discover_skills(tmp_path / "cwd", user_dir,
                            on_error=errors.append)
    assert [s.name for s in found] == ["good"]
    assert len(errors) == 1


# --- render_skills_section ---

def test_render_empty_is_empty_string():
    assert render_skills_section([]) == ""


def test_render_lists_name_description_and_path(tmp_path):
    s = make_skill(tmp_path, name="pdf", description="Handle PDFs")
    section = render_skills_section([Skill(
        name="pdf", description="Handle PDFs", dir=s, source="user")])
    assert section.startswith("# Skills")
    assert "skill_read" in section
    assert "Handle PDFs" in section
    assert str(s / "SKILL.md") in section


def test_render_truncates_long_description():
    d = make_skill_tmp_long()
    skill = Skill(name="long", description="x" * 800, dir=d, source="user")
    section = render_skills_section([skill])
    assert "…[truncated]" in section
    assert "x" * 800 not in section


def make_skill_tmp_long():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "long"


def test_render_respects_total_budget():
    d = make_skill_tmp_long()
    many = [Skill(name=f"s{i}", description="y" * 400, dir=d, source="user")
            for i in range(30)]
    section = render_skills_section(many)
    assert "omitted" in section
    assert len(section) < skills._MAX_SECTION_CHARS + 800


# --- skill_read handler ---

def test_skill_read_reads_skill_file_and_bundled(tmp_path):
    d = make_skill(tmp_path, name="reader", extra_files={
        "helper.txt": "bundled content",
        "scripts/run.py": "print('hi')",
    })
    handler = _make_skill_read_handler([parse_skill_md(d, "user")])
    out = handler({"path": str(d / "SKILL.md")})
    assert "Do the thing." in out
    assert handler({"path": str(d / "helper.txt")}) == "bundled content"
    assert handler({"path": str(d / "scripts" / "run.py")}) == "print('hi')"


def test_skill_read_relative_path_uses_workdir(tmp_path):
    d = make_skill(tmp_path, name="rel")
    handler = _make_skill_read_handler([parse_skill_md(d, "user")])
    out = handler({"path": "SKILL.md"}, workdir=str(d))
    assert "Do the thing." in out


def test_skill_read_rejects_path_escape(tmp_path):
    d = make_skill(tmp_path, name="guard")
    handler = _make_skill_read_handler([parse_skill_md(d, "user")])
    with pytest.raises(SkillError, match="outside"):
        handler({"path": str(tmp_path / "outside.txt")})
    # 技能目录内捆绑文件里的相对穿越: resolve 后越界同样被拒
    with pytest.raises(SkillError, match="outside"):
        handler({"path": str(d / ".." / ".." / "escape.txt")})


def test_skill_read_missing_file_and_empty_path(tmp_path):
    d = make_skill(tmp_path, name="mm")
    handler = _make_skill_read_handler([parse_skill_md(d, "user")])
    with pytest.raises(SkillError, match="not a file"):
        handler({"path": str(d / "nope.md")})
    with pytest.raises(SkillError, match="required"):
        handler({})


def test_register_skill_tools_idempotent(tmp_path):
    reg = ToolRegistry()
    make_skill(tmp_path / "skills", name="reg")
    found = discover_skills(tmp_path, tmp_path)
    assert found, "发现层应能找到 reg"
    register_skill_tools(reg, found)
    assert "skill_read" in reg._handlers
    # 空列表: 换成占位 handler（明确拒绝）, 注册表条目不消失——
    # 与 TOOL_REQUIREMENTS 的登记保持一致
    register_skill_tools(reg, [])
    assert "skill_read" in reg._handlers
    with pytest.raises(ToolError):
        reg.execute("skill_read", json.dumps({"path": "x"}))


def test_registry_execute_routes_through_truncate(tmp_path):
    d = make_skill(tmp_path, name="route")
    reg = ToolRegistry()
    register_skill_tools(reg, [parse_skill_md(d, "user")])
    out = reg.execute("skill_read",
                      json.dumps({"path": str(d / "SKILL.md")}))
    assert "Do the thing." in out
    with pytest.raises(ToolError):
        reg.execute("skill_read", json.dumps({"path": "C:/Windows/win.ini"}))


# --- sync_skill_tools（TOOLS 原地同步, multi_agent 按引用共享） ---

def test_sync_adds_and_removes_spec():
    tools = [{"name": "bash"}, {"name": "grep"}]
    sync_skill_tools(tools, [Skill(name="x", description="", dir=None,
                                   source="user")])
    assert [t["name"] for t in tools] == ["bash", "grep", "skill_read"]
    sync_skill_tools(tools, [])
    assert [t["name"] for t in tools] == ["bash", "grep"]
    # 幂等: 重复同步不重复加
    sync_skill_tools(tools, [Skill(name="x", description="", dir=None,
                                   source="user")])
    sync_skill_tools(tools, [Skill(name="x", description="", dir=None,
                                   source="user")])
    assert [t["name"] for t in tools].count("skill_read") == 1


# --- skill_info / delete_user_skill ---

def test_skill_info_fields(tmp_path):
    d = make_skill(tmp_path, name="info", description="info desc")
    info = skill_info([parse_skill_md(d, "project")])[0]
    assert info["name"] == "info"
    assert info["source"] == "project"
    assert info["description"] == "info desc"
    assert info["dir"] == str(d)
    assert info["skill_file"] == str(d / "SKILL.md")


def test_delete_user_skill(tmp_path):
    user_dir = tmp_path / "home"
    make_skill(user_dir / "skills", name="gone")
    delete_user_skill("gone", user_dir)
    assert not (user_dir / "skills" / "gone").exists()
    with pytest.raises(SkillError, match="不存在"):
        delete_user_skill("gone", user_dir)


def test_delete_rejects_names_outside_skills_root(tmp_path):
    user_dir = tmp_path / "home"
    with pytest.raises(SkillError):
        delete_user_skill("..", user_dir)
    with pytest.raises(SkillError, match="不存在"):
        delete_user_skill("never-existed", user_dir)
