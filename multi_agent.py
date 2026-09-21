import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from textwrap import dedent
from typing import Optional, Callable

from dotenv import load_dotenv
from pydantic import BaseModel

from api_client import ClaudeApiClient, make_api_client
from fsatomic import atomic_write_text, read_text_with_retry
from models import Session, TextContentBlock
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime
from tools import ToolRegistry, bash_tool, read_tool, write_tool, powershell_tool

TOOL_WHITELIST: dict[str, set[str]] = {
    "explore": {"read_file"},
    "plan": {"read_file"},
    "verification": {"bash", "read_file"},
    "general": {"bash", "read_file", "write_file", "web_search", "web_fetch"},
    "explore_web": {"web_search", "web_fetch"},
}

class AgentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class AgentManifest(BaseModel):
    agent_id: str
    name: Optional[str]
    description: str
    subagent_type: str
    status: str  # "pending" / "running" / "completed" / "failed"
    output_file: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    error: Optional[str]
    result: Optional[str] = None   # 终态结果文本（completed 时 = worker 汇报原文）
    # True = 结果已注入 Leader 对话（收割过）。与 status 解耦: completed 只说明
    # worker 跑完, delivered 才说明结果进了对话——两者之间可能隔着 Leader 忘轮询。
    delivered: bool = False

class AgentJob(BaseModel):
    manifest: AgentManifest
    prompt: str
    allowed_tools: set[str]
    # worker 的工具工作目录（bash cwd / 相对路径解析基点）: 从 Leader 会话继承,
    # 让子任务与主对话操作同一个项目。缺省 = 跟随进程 cwd（CLI 旧行为）。
    workdir: Optional[str] = None


def allowed_tools_for_subagent(subagent_type: str) -> set[str]:
    default_set = TOOL_WHITELIST["general"]
    return TOOL_WHITELIST.get(subagent_type, default_set)


# --- 第 12 课: worker 的 runtime 组装 ---

# 工具名 → 处理函数。worker 只注册 job.allowed_tools 里出现的名字，
# 没注册的工具在 ToolRegistry 层直接抛 ToolError（白名单的执行层兜底）。
SUBAGENT_TOOL_HANDLERS: dict[str, Callable] = {
    "bash": bash_tool,
    "powershell": powershell_tool,
    "read_file": read_tool,
    "write_file": write_tool,
}


class SilentToolExecutor:
    """worker 用的安静执行器 — 实现 runtime 的 ToolExecutor Protocol，
    但不像 Leader 的 CliToolExecutor 那样往终端打印（终端是 Leader 的）。

    workdir 来自构造时绑定（job.workdir），执行时注入 registry ——
    bash 的 cwd、读写文件的相对路径解析基点都由它决定。"""

    def __init__(self, registry: ToolRegistry, workdir: Optional[str] = None):
        self.registry = registry
        self.workdir = workdir

    def execute(self, tool_name: str, input: str, tool_use_id: str | None = None) -> str:
        return self.registry.execute(tool_name, input, workdir=self.workdir)


# worker 的 API 客户端工厂: 返回 (api_key, base_url, model)。
# 可被宿主（Web 端）注入以跟随其运行期供应商配置; 缺省回落 .env +
# main.DEFAULT_MODEL（CLI 的连接方式）。可插拔是为了不让 multi_agent
# 反向依赖任何特定宿主的配置来源。
ApiConfigProvider = Callable[[], tuple[str, Optional[str], str]]


def _default_api_config() -> tuple[str, Optional[str], str]:
    load_dotenv()
    api_key = os.getenv("API_KEY")
    if api_key is None:
        raise RuntimeError("API_KEY not set!")
    # 默认模型定义在 main.py（组装点）。延迟导入: main 接线 multi_agent
    # 后模块级 import 会成环，函数内 import 不会。
    from main import DEFAULT_MODEL
    return api_key, None, os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL


_api_config_provider: ApiConfigProvider = _default_api_config


def set_api_config_provider(provider: ApiConfigProvider) -> None:
    """宿主注入 worker 连接配置（如 Web 端跟随 UI 选的供应商/模型）。"""
    global _api_config_provider
    _api_config_provider = provider


def _tool_specs_for(allowed_tools: set[str]) -> list[dict]:
    """白名单 → API 请求的 tools 数组。只把白名单内的规格发给模型:
    执行层拦截（registry 未注册抛 ToolError）是兜底，把不可用的工具
    从请求里剔除才是让模型不误调的根本手段。"""
    from main import TOOLS
    by_name = {spec["name"]: spec for spec in TOOLS}
    return [by_name[n] for n in sorted(allowed_tools) if n in by_name]


def build_subagent_runtime(job: AgentJob) -> ConversationRuntime:
    """给 worker 组装一个完全独立的 runtime。

    - 空会话: subagent 看不到 Leader 的对话历史（上下文隔离的根基）
    - 安静的 api_client: emit_output=False，流式输出不刷 Leader 的终端
    - 白名单工具: 只注册 allowed_tools，白名单之外的工具调不到
    - ALLOW 权限: 子 agent 的边界是白名单而不是权限模式——没有人类
      可以被询问，权限模式在这里没有意义
    - 递归防护: 工具规格经 _tool_specs_for 过滤，agent 工具永远不在
      worker 的请求里（TOOL_WHITELIST 亦不含, 双保险）
    """
    api_key, base_url, model, protocol = _api_config_provider()

    registry = ToolRegistry()
    for tool_name in sorted(job.allowed_tools):
        handler = SUBAGENT_TOOL_HANDLERS.get(tool_name)
        if handler is not None:
            registry.register(name=tool_name, handler=handler)

    return ConversationRuntime(
        session=Session(),
        api_client=make_api_client(
            protocol,
            api_key=api_key,
            model=model,
            base_url=base_url,
            tools=_tool_specs_for(job.allowed_tools),
            emit_output=False,
        ),
        tool_executor=SilentToolExecutor(registry=registry, workdir=job.workdir),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=[
            "You are a focused subagent worker. Complete exactly the task "
            "you were given, then report the result as text. Do nothing "
            "beyond the task.",
        ],
    )


def final_text_of(summary) -> str:
    """从 TurnSummary 提取最后一条带文本的 assistant 消息，作为 agent 的结果。"""
    for msg in reversed(summary.assistant_messages):
        texts = [b.text for b in msg.content if isinstance(b, TextContentBlock)]
        if texts:
            return "\n".join(texts)
    return "(无文本输出)"


class AgentOrchestrator:
    def __init__(self, store_dir: Path, spawn_fn: Optional[Callable] = None,
                 workdir: Optional[str] = None):
        self._store_dir = store_dir
        self._workdir = workdir   # 派生的 worker 工具共用的工作目录（Leader 会话的项目）
        self._spawn_fn = spawn_fn if spawn_fn else self._default_spawn_fn

    def _default_spawn_fn(self, job: AgentJob):
        def _worker():  # ← 内层：线程的身体
            # 第 12 课: worker 本体 = 建 runtime → 跑一轮 → complete_agent
            try:
                runtime = build_subagent_runtime(job)
                summary = runtime.run_turn(job.prompt)
                self.complete_agent(job.manifest.agent_id, final_text_of(summary))
            except Exception as exc:
                try:
                    self._persist_terminal_state(job.manifest, status=AgentStatus.FAILED.value, result=None, error=str(exc))
                except Exception:
                    pass

        thread = threading.Thread(target=_worker, name=f"agent-{job.manifest.agent_id}", daemon=True)  # ← 外层：target=_worker（注意没有括号！）
        thread.start()  # ← 点火，立刻返回

    def spawn_agent(self, description: str, prompt: str, name: Optional[str] = None, subagent_type: str = "general",
                    workdir: Optional[str] = None) -> AgentManifest:
        if description.strip() == "" or prompt.strip() == "":
            raise ValueError("description or prompt are null")
        self._store_dir.mkdir(parents=True, exist_ok=True)
        agent_id = uuid.uuid4().hex[0:12]
        md_path = self._store_dir / f"{agent_id}.md"
        json_path = self._store_dir / f"{agent_id}.json"

        subagent_type = subagent_type.strip().lower()
        white_tools = allowed_tools_for_subagent(subagent_type)

        now = datetime.now(timezone.utc).isoformat()
        name = name if name else ""
        manifest = AgentManifest(
            agent_id=agent_id,
            name=name,
            description=description,
            subagent_type=subagent_type,
            status=AgentStatus.RUNNING.value,
            output_file=str(md_path),
            started_at=now,
            created_at=now,
            completed_at=None,
            error=None
        )
        md_content = dedent(f"""\
            # Agent Task

            - id: {agent_id}
            - name: {name}
            - subagent_type: {subagent_type}
            - created_at: {now}

            ## Prompt

            {prompt}
        """)
        with open(md_path, "w", encoding= "utf-8") as f:
            f.write(md_content)

        manifest_content = manifest.model_dump_json()
        # 原子写: 其他会话的 reap/list 可能此刻正 glob *.json, 直写会让
        # 读者看到半截 JSON（解析失败被吞, agent 凭空消失）
        atomic_write_text(json_path, manifest_content)

        # workdir: 调用方显式传入优先（Web 按会话传）, 否则用编排器默认
        job = AgentJob(
            manifest=manifest.model_copy(),
            prompt=prompt,
            allowed_tools=white_tools.copy(),
            workdir=workdir or self._workdir,
        )
        try:
            self._spawn_fn(job)
        except Exception as e:
            self._persist_terminal_state(manifest=manifest, status=AgentStatus.FAILED.value, result="", error=str(e))
            raise RuntimeError(f"Spawn failed: {e}") from e
        return manifest


    def set_workdir(self, workdir: Optional[str]) -> None:
        """更新后续派生的 worker 的工作目录（Web 端按会话解析后注入）。"""
        self._workdir = workdir or None

    def get_status(self,agent_id: str) -> AgentManifest:
        json_path = self._store_dir / f"{agent_id}.json"
        if not json_path.exists():
            raise FileNotFoundError(f"Agent {agent_id} not found")
        # 读走重试: replace 过渡窗口里新开读句柄在 Windows 上会瞬时被拒
        manifest = AgentManifest.model_validate(
            json.loads(read_text_with_retry(json_path)))

        return manifest

    def list_agents(self) -> list[AgentManifest]:
        result: list[AgentManifest] = []
        for manifest in self._store_dir.glob("*.json"):
            try:
                result.append(AgentManifest.model_validate(
                    json.loads(read_text_with_retry(manifest))))
            except Exception as e:
                continue

        return result

    def _persist_terminal_state(self, manifest: AgentManifest, status: str, result: Optional[str], error:Optional[str]):
        """写终态。

        .md 追加结果；manifest JSON 用原子覆盖写（唯一临时文件 + 原子替换）。
        worker 线程写终态的同时，Leader 可能正在轮询 get_status() 读
        同一个文件——直接 open("w") 会先截断再写，读者会撞见半截 JSON。
        原子替换保证读者要么看到完整旧文件、要么看到完整新文件；与
        mark_delivered 并发时唯一临时名互不踩踏（固定名会互相截断对方的
        临时文件，把半截内容发布出去），Windows 上目标被读者占住时的
        瞬时 PermissionError 由重试兜住。
        """
        md_path = self._store_dir / f"{manifest.agent_id}.md"
        now = datetime.now(timezone.utc).isoformat()
        md_content = dedent(f"""\
                   ## Result
                   
                   status: {status}
                   result: {result}
                   error: {error}
               """)
        with open(md_path, "a", encoding= "utf-8") as f:
            f.write(md_content)
        new_manifest = manifest.model_copy(update={
            "status": status,
            "completed_at": now,
            "error": error,
            "result": result,
        })
        json_path = self._store_dir / f"{manifest.agent_id}.json"
        atomic_write_text(json_path, new_manifest.model_dump_json())


    def complete_agent(self, agent_id: str, result:str):
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest=manifest, status=AgentStatus.COMPLETED.value,result=result, error=None)


    # --- 收割: 把"跑完了但结果没进对话"的 worker 交给 Leader ---

    def reap_ready(self) -> list[AgentManifest]:
        """completed 且未 delivered 的 worker 列表（只读快照, 不改状态）。"""
        ready = []
        for m in self.list_agents():
            if m.status == AgentStatus.COMPLETED.value and not m.delivered:
                ready.append(m)
        return ready

    def mark_delivered(self, agent_ids: list[str]) -> int:
        """把结果标记为已交付（原子覆盖写回）。返回成功数。

        与 worker 线程的终态写并发时两侧都走唯一临时名 + 原子替换,
        读者不会撞见半截文件, 写者互不踩踏; 极端交错下最坏是多标一次
        delivered, 幂等无害。"""
        ok = 0
        for agent_id in agent_ids:
            try:
                manifest = self.get_status(agent_id)
            except FileNotFoundError:
                continue
            new_manifest = manifest.model_copy(update={"delivered": True})
            json_path = self._store_dir / f"{agent_id}.json"
            atomic_write_text(json_path, new_manifest.model_dump_json())
            ok += 1
        return ok

    def reconcile_orphans(self) -> int:
        """启动对账: 进程死亡时 daemon worker 随之消失, manifest 永远停在
        running——把 running 一律标记 failed。

        简化假设: 对账只在进程启动时调用一次, 此刻本进程不可能有 running
        worker（_default_spawn_fn 还没跑过）, 所有 running 都是上次进程的
        孤儿。运行期绝不能调用。"""
        orphans = [m for m in self.list_agents()
                   if m.status == AgentStatus.RUNNING.value]
        for m in orphans:
            self.fail_agent(m.agent_id,
                            "进程重启时该 worker 仍在运行, 已标记为失败（结果未产出）")
        return len(orphans)


    def fail_agent(self, agent_id: str, error: str) :
        manifest = self.get_status(agent_id)
        self._persist_terminal_state(manifest = manifest, status = AgentStatus.FAILED.value,result=None, error= error)
