# --- Skills: Claude Code 兼容的专项技能包 ---
#
# 一个技能 = 一个目录 + 一个 SKILL.md（YAML frontmatter + 正文指令），
# 可选捆绑任意辅助文件（脚本/模板/数据），目录就是技能的工作区。
#
# 发现规则（与指令文件同思路的两级作用域）:
#   用户级  ~/.x-code/skills/<name>/SKILL.md        —— 跨项目可用
#   项目级  <项目>/.claude/skills/<name>/SKILL.md   —— 随仓库走, 同名覆盖用户级
#
# 注入策略 = 渐进式披露（省 token 的关键）: 系统提示词只进 name + description
# 清单（几百 token 量级）, 正文让模型按需用 skill_read 工具读。没有这一层,
# 十个技能的全文能把上下文吃掉一大截, 而大多数轮次根本用不上。

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml

SKILL_FILE = "SKILL.md"
SKILL_TOOL_NAME = "skill_read"

# name 允许的字符集: 目录名即技能名, 顺手保证它做目录/拼 URL 都安全
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

_MAX_DESC_CHARS = 500        # 清单里单个 description 的截断
_MAX_SECTION_CHARS = 6_000   # 整个 Skills 清单段的字符预算


def sanitize_name(raw: str) -> str:
    """技能名清洗: 白名单字符以外的连成 '-'。空/全非法名返回空串（调用方跳过）。"""
    cleaned = _SAFE_NAME_RE.sub("-", (raw or "").strip()).strip("-")
    return cleaned


@dataclass
class Skill:
    """一个已发现的技能。body 是 SKILL.md 去掉 frontmatter 后的正文。"""
    name: str
    description: str
    dir: Path
    source: str                 # "user" | "project"
    body: str = ""

    def manifest_line(self) -> str:
        """清单里的一行: 名字 + 描述 + 读取指引。"""
        desc = self.description.strip().replace("\n", " ")
        if len(desc) > _MAX_DESC_CHARS:
            desc = desc[:_MAX_DESC_CHARS] + " …[truncated]"
        skill_file = self.dir / SKILL_FILE
        return f" - {self.name}: {desc} (read: skill_read path={skill_file})"


class SkillError(Exception):
    """解析/安装失败。带 user-facing 原因, 由 CLI 打红字 / API 返 400。"""


def _split_frontmatter(text: str) -> tuple[Optional[dict], str]:
    """拆出 YAML frontmatter。返回 (元数据 dict 或 None, 正文)。
    没有显式 frontmatter 时正文原样返回（Claude Code 允许纯 markdown 技能）。"""
    if not text.startswith("---"):
        return None, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return None, text
    raw_meta = parts[0][3:].strip()
    body = parts[1]
    # 去掉正文开头的多余换行, 保留其余原样
    if body.startswith("\n"):
        body = body[1:]
    try:
        meta = yaml.safe_load(raw_meta)
    except yaml.YAMLError as e:
        raise SkillError(
            f"SKILL.md frontmatter 解析失败: {e}\n"
            "  提示: 值里含冒号/特殊字符时请加引号, "
            "如 description: \"处理 PDF: 拆分与合并\"")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SkillError("SKILL.md frontmatter 必须是键值对")
    return meta, body


def parse_skill_md(skill_dir: Path, source: str) -> Skill:
    """SKILL.md → Skill。frontmatter 非法抛 SkillError（发现层跳过并记录,
    安装层让错误响亮——配错的技能静默丢弃才是排查黑洞）。"""
    skill_file = skill_dir / SKILL_FILE
    text = skill_file.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)

    name = meta.get("name") if meta else None
    name = sanitize_name(str(name)) if name else sanitize_name(skill_dir.name)
    if not name:
        raise SkillError(f"{skill_file}: 技能名为空")

    description = meta.get("description", "") if meta else ""
    if not isinstance(description, str):
        description = str(description)
    return Skill(name=name, description=description, dir=skill_dir,
                 source=source, body=body)


def discover_skills(cwd: Path, user_dir: Path,
                    on_error: Optional[Callable[[str], None]] = None) -> list[Skill]:
    """两级目录发现技能, 项目级同名覆盖用户级（后写入同名即胜出）。
    单个技能解析失败降级为警告回调, 不拖垮整个发现。"""
    roots = [
        (Path(user_dir) / "skills", "user"),
        (Path(cwd) / ".claude" / "skills", "project"),
    ]
    skills: dict[str, Skill] = {}
    for root, source in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            skill_file = child / SKILL_FILE
            if not child.is_dir() or not skill_file.is_file():
                continue
            try:
                skill = parse_skill_md(child, source)
            except SkillError as e:
                if on_error:
                    on_error(str(e))
                continue
            except (OSError, UnicodeDecodeError) as e:
                if on_error:
                    on_error(f"{skill_file}: {e}")
                continue
            skills[skill.name] = skill   # 项目级后扫, 同名自然覆盖
    return list(skills.values())


def render_skills_section(skills: list[Skill]) -> str:
    """渲染系统提示词的 # Skills 段。空列表返回空串（不进提示词,
    静态前缀逐字节不变, prompt caching 不受影响）。
    总预算先到先得——与指令文件同款策略, 超出的技能不进清单。"""
    if not skills:
        return ""
    lines = [
        "# Skills",
        "The following skill packages are installed. When the user's task "
        "clearly matches one of them, FIRST read its SKILL.md with the "
        "skill_read tool, then follow the instructions inside it. A skill's "
        "directory may bundle additional files (scripts/templates/data) — "
        "read them with skill_read as the SKILL.md directs, and treat the "
        "skill instructions as authoritative for how to do that kind of task.",
    ]
    remaining = _MAX_SECTION_CHARS
    omitted = 0
    for skill in skills:
        line = skill.manifest_line()
        if len(line) > remaining:
            omitted += 1
            continue
        lines.append(line)
        remaining -= len(line)
    if omitted:
        lines.append(f"_(+{omitted} more skills omitted after reaching the "
                     "manifest budget; use /skills or the settings page to "
                     "see the full list.)_")
    return "\n".join(lines)


# --- skill_read 工具: 按需读取技能目录内的文件（含 SKILL.md 与捆绑文件） ---

skill_read_spec = {
    "name": SKILL_TOOL_NAME,
    "description": (
        "Read a file from an installed skill package: the SKILL.md itself or "
        "any bundled file inside that skill's directory. Paths outside skill "
        "directories are rejected. Call it when the task matches a skill in "
        "the Skills list, before following that skill's instructions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "File path inside the skill directory, as shown in the "
                    "Skills list (e.g. the SKILL.md path) or announced by "
                    "the skill itself."
                ),
            },
        },
        "required": ["path"],
    },
}


def _make_skill_read_handler(skills: list[Skill]) -> Callable:
    """闭包绑定本次会话发现的技能目录。执行期按名字重新匹配, 目录是
    发现时的快照——热装卸后旧引用指向的仍是合法的旧目录, 无悬挂风险。"""
    by_name = {s.name: s for s in skills}

    def handler(params: dict, workdir: Optional[str] = None) -> str:
        raw = str(params.get("path") or "").strip()
        if not raw:
            raise SkillError("skill_read: path is required")
        p = Path(raw)
        if not p.is_absolute():
            base = Path(workdir) if workdir else Path.cwd()
            p = base / p
        target = p.resolve()
        # 路径安全: resolve 后必须落在某个技能目录内——技能名来自社区
        # 仓库, 捆绑文件里写 "../../.ssh/id_rsa" 这类路径必须挡住
        owner = None
        for skill in by_name.values():
            root = skill.dir.resolve()
            if target == root or root in target.parents:
                owner = skill
                break
        if owner is None:
            raise SkillError(
                f"skill_read: path is outside every installed skill "
                f"directory: {raw}")
        if not target.is_file():
            raise SkillError(f"skill_read: not a file: {raw}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SkillError(f"skill_read: binary file not supported: {raw}")
        return text

    return handler


def register_skill_tools(registry, skills: list[Skill]):
    """skill_read 常驻注册表: 有技能时绑定发现结果; 没有时装明确拒绝的
    占位 handler——保持注册表与 TOOL_REQUIREMENTS 登记一致（权限判定、
    测试覆盖不留缺口）, spec 则只在有技能时进 TOOLS（模型才知道它存在）。
    先注销再挂: registry.register 撞名会抛错, 热装卸会重复走到这里。"""
    registry.unregister(SKILL_TOOL_NAME)
    if skills:
        registry.register(name=SKILL_TOOL_NAME,
                          handler=_make_skill_read_handler(skills))
    else:
        def _no_skills(params: dict, workdir: Optional[str] = None) -> str:
            raise SkillError("skill_read: no skills installed")
        registry.register(name=SKILL_TOOL_NAME, handler=_no_skills)
    return registry


def sync_skill_tools(tools_list: list[dict], skills: list[Skill]) -> None:
    """TOOLS（api_client / multi_agent 共享的同一列表对象）原地增删
    skill_read 的 spec。与 _attach_mcp_tools 同一手法的理由: 列表被
    多处按引用持有, 原地改即全链路生效, 重建对象会丢同步。"""
    has = any(t.get("name") == SKILL_TOOL_NAME for t in tools_list)
    if skills and not has:
        tools_list.append(dict(skill_read_spec))
    elif not skills and has:
        tools_list[:] = [t for t in tools_list if t.get("name") != SKILL_TOOL_NAME]


# --- 社区 skills 安装: GitHub 仓库 git clone → 拷入用户技能目录 ---

def install_from_repo(repo: str, user_dir: Path,
                      subpath: str = "",
                      overwrite: bool = False) -> list[Skill]:
    """从 git 仓库安装技能到 <user_dir>/skills/。仓库布局三种都认:
    根目录就是技能 / subpath 指向单个技能 / skills/<name>/ 一仓多技能。
    同名已存在且未显式 overwrite → SkillError（防误覆盖用户自改内容）。
    返回本次安装的技能列表。"""
    from main import git_bash_unavailable_reason   # 延迟导入避免环
    reason = git_bash_unavailable_reason()
    if reason:
        raise SkillError(reason)

    repo = (repo or "").strip().strip('"')
    if not repo:
        raise SkillError("仓库地址不能为空")
    # owner/repo 短格式补全为 GitHub URL。判定要避开本地路径:
    # 含 scheme(://)或 scp 风格(@)的是完整地址; Windows 盘符(C:/ C:\)
    # 和以 / 或 . 开头的是路径——只有"恰好两段且都是常规字符"才算短格式
    if "://" not in repo and not repo.startswith("git@"):
        is_win_path = re.match(r"^[A-Za-z]:[\\/]", repo) or repo.startswith(("/", "."))
        is_short = (re.match(r"^[\w.-]+/[\w.-]+$", repo) is not None
                    and not is_win_path)
        if is_short:
            repo = f"https://github.com/{repo}"

    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix="xcode-skills-") as tmp:
        dest = Path(tmp) / "repo"
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", repo, str(dest)],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise SkillError(
                f"git clone 失败: {(proc.stderr or proc.stdout or '').strip()}")

        root = dest / subpath.strip("/\\") if subpath.strip() else dest
        if not root.is_dir():
            raise SkillError(f"子目录不存在: {subpath}")

        # 收集候选: subpath 本身是技能 / 其下 skills/*/ 是技能
        candidates: list[Path] = []
        if (root / SKILL_FILE).is_file():
            candidates.append(root)
        for child in sorted(root.rglob(SKILL_FILE)):
            parent = child.parent
            if parent != root and parent not in candidates:
                candidates.append(parent)
        if not candidates:
            raise SkillError(
                f"仓库里没找到 {SKILL_FILE}（根目录、指定子目录及 skills/*/ 均未发现）")

        skills_root = Path(user_dir) / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        installed: list[Skill] = []
        errors: list[str] = []
        for cand in candidates:
            try:
                skill = parse_skill_md(cand, "user")
            except SkillError as e:
                errors.append(str(e))
                continue
            target = skills_root / skill.name
            if target.exists() and not overwrite:
                raise SkillError(
                    f"技能 {skill.name} 已存在（传 overwrite=true 或在界面勾选覆盖）")
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(cand, target,
                            ignore=shutil.ignore_patterns(".git", "__pycache__"))
            installed.append(skill)
        if not installed and errors:
            raise SkillError("; ".join(errors))
        return installed


def delete_user_skill(name: str, user_dir: Path) -> None:
    """卸载用户级技能。目录不存在抛 SkillError; 项目级技能不在
    user_dir 下, resolve 校验天然拒绝。"""
    skills_root = (Path(user_dir) / "skills").resolve()
    target = (skills_root / sanitize_name(name)).resolve()
    if skills_root not in target.parents:
        raise SkillError(f"非法技能名: {name}")
    if not target.is_dir():
        raise SkillError(f"技能不存在: {name}")
    shutil.rmtree(target)


def skill_info(skills: list[Skill]) -> list[dict]:
    """API 下发视图。"""
    return [
        {"name": s.name, "description": s.description,
         "source": s.source, "dir": str(s.dir),
         "skill_file": str(s.dir / SKILL_FILE)}
        for s in skills
    ]
