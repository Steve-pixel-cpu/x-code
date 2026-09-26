# 记忆注入到系统提示词的接线测试: 静态指引 / 动态 section / 无记忆省略
from prompt import (SystemPromptBuilder, SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
                    ProjectContext)
from pathlib import Path


def _builder(memory_text=None) -> SystemPromptBuilder:
    return (SystemPromptBuilder()
            .with_os("Windows", "11")
            .with_project_context(ProjectContext(
                cwd=Path.cwd(), current_date="2026-09-26"))
            .with_memory_section(memory_text))


def test_static_memory_guidance_present():
    """静态段含记忆工具使用指引（在边界之前, 进缓存前缀）。"""
    sections = _builder().build()
    boundary_idx = sections.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
    static = "\n".join(sections[:boundary_idx])
    assert "# User memory" in static
    assert "memory_write" in static and "memory_update" in static
    assert "memory_delete" in static


def test_memory_section_after_boundary():
    """记忆段在动态边界之后（动态区尾部、追加段之前）, 不进缓存前缀。"""
    sections = _builder("<user-memory>\n- [fact] x\n</user-memory>").build()
    boundary_idx = sections.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
    mem_idxs = [i for i, s in enumerate(sections) if "<user-memory>" in s]
    assert len(mem_idxs) == 1
    assert mem_idxs[0] > boundary_idx          # 边界之后（动态区）
    assert mem_idxs[0] == len(sections) - 1    # 动态区最后一员（追加段之前）


def test_no_memory_omits_section():
    """无记忆（None）时不追加任何记忆段——不输出空壳标签。"""
    sections = _builder(None).build()
    assert not any("<user-memory>" in s for s in sections)


def test_builder_default_has_no_memory_section():
    """不调 with_memory_section 的旧路径行为不变。"""
    sections = SystemPromptBuilder().build()
    assert not any("user-memory" in s for s in sections)
