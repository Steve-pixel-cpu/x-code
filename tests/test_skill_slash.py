"""斜杠技能命令: 匹配与展开的纯函数契约。

match_skill_command  — /<名> → 唯一命中技能; 精确名优先, 唯一前缀次之;
                        多义前缀/无命中/非斜杠一律 None（原样透传）。
expand_skill_command — 展开文本必须包含 skill_read 路径与用户请求。
"""
from pathlib import Path

import pytest

from skills import Skill, expand_skill_command, match_skill_command


def _skill(name: str, desc: str = "") -> Skill:
    return Skill(name=name, description=desc, dir=Path(".") / name,
                 source="user")


BRAIN = _skill("brainstorming", "创意工作的头脑风暴")
DEBUG = _skill("systematic-debugging", "系统化调试")
TDD = _skill("test-driven-development")
SKILLS = [BRAIN, DEBUG, TDD]


# --- match_skill_command: 命中 ---

@pytest.mark.parametrize("text,expected", [
    ("/brainstorming", BRAIN),                      # 精确名
    ("/brainstorming 写个 TODO 应用", BRAIN),        # 精确名 + 请求
    ("/brain", BRAIN),                              # 唯一前缀
    ("/BRAIN", BRAIN),                              # 前缀大小写不敏感
    ("/systematic", DEBUG),                         # 长名唯一前缀
    ("/test", TDD),                                 # 另一技能的子串不算, 前缀才算
    ("/brain 写代码", BRAIN),                        # 命令名只取第一个 token
])
def test_match_hits(text, expected):
    assert match_skill_command(text, SKILLS) is expected


def test_match_project_level_skill():
    proj = _skill("deploy")
    proj.source = "project"
    assert match_skill_command("/deploy", [proj]) is proj


# --- match_skill_command: 不命中（原样透传, 交给模型） ---

@pytest.mark.parametrize("text", [
    "",                  # 空输入
    "普通消息",           # 非斜杠
    "/",                 # 裸斜杠
    "/nope",             # 无命中
    "/brainstormingx",   # 名字接了别的字符 ≠ 前缀之外的命中, 无此技能
])
def test_match_misses(text):
    assert match_skill_command(text, SKILLS) is None


def test_ambiguous_prefix_returns_none():
    two_s = [DEBUG, _skill("skills-guide")]
    assert match_skill_command("/s", two_s) is None       # 两个 s 开头 → 歧义
    assert match_skill_command("/sk", two_s) is two_s[1]  # 收窄后唯一 → 命中


# --- expand_skill_command ---

def test_expand_with_request():
    out = expand_skill_command(BRAIN, "/brain 写个 TODO 应用")
    assert "skill_read" in out
    assert str(BRAIN.dir / "SKILL.md") in out
    assert "写个 TODO 应用" in out
    assert "/brainstorming" in out


def test_expand_without_request():
    out = expand_skill_command(BRAIN, "/brainstorming")
    assert "skill_read" in out
    assert str(BRAIN.dir / "SKILL.md") in out
    assert "没有附加具体请求" in out


def test_expand_uses_canonical_name():
    # 用前缀 /sys 发起, 展开里出现的应是完整技能名
    out = expand_skill_command(DEBUG, "/sys 修 bug")
    assert "/systematic-debugging" in out
