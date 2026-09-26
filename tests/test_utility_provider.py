"""utilityProvider（side-call 小模型）的验收测试。

配置: settings.json 的 utilityProvider = {"provider": <id>, "model": <名>}。
自动命名、压缩摘要、会话记忆摘要这类"整理型"调用走它, 主循环不动;
未配置/无效配置回落主模型（原行为）。

运行: uv run pytest tests/test_utility_provider.py -v
"""

import json

import pytest

import config
import main as main_mod
from config import load_utility_provider
from permissions import ALLOW_MODE, PermissionPolicy
from runtime import ConversationRuntime

from test_llm_summary import NoopExecutor, SummaryFakeClient, make_session


# ------------------------------------------------------------
# 配置解析: 校验失败一律回落 None（调用方回落主模型）
# ------------------------------------------------------------

def _write_settings(tmp_path, data):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _providers():
    return [{
        "id": "budget", "enabled": True, "api_key": "uk-123",
        "base_url": "https://budget.example.com", "protocol": "anthropic",
    }]


def test_no_settings_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "absent.json")
    assert load_utility_provider() is None


def test_valid_utility_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SETTINGS_FILE", _write_settings(tmp_path, {
        "providers": _providers(),
        "utilityProvider": {"provider": "budget", "model": "glm-4.5-air"},
    }))
    cfg = load_utility_provider()
    assert cfg == {"api_key": "uk-123",
                   "base_url": "https://budget.example.com",
                   "protocol": "anthropic",
                   "model": "glm-4.5-air"}


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("utilityProvider"),                              # 未配置
    lambda d: d["utilityProvider"].update({"provider": "ghost"}),    # 指向不存在
    lambda d: d["providers"][0].update({"enabled": False}),          # 被禁用
    lambda d: d["providers"][0].pop("api_key"),                      # 缺 key
    lambda d: d["providers"][0].update({"base_url": ""}),            # 缺地址
])
def test_invalid_config_falls_back_to_none(monkeypatch, tmp_path, mutate):
    data = {"providers": _providers(),
            "utilityProvider": {"provider": "budget", "model": "glm-4.5-air"}}
    mutate(data)
    monkeypatch.setattr(config, "SETTINGS_FILE",
                        _write_settings(tmp_path, data))
    assert load_utility_provider() is None


def test_missing_model_returns_none_model(monkeypatch, tmp_path):
    """model 留空合法: 调用方自行决定回落（CLI 回落主配置模型）。"""
    monkeypatch.setattr(config, "SETTINGS_FILE", _write_settings(tmp_path, {
        "providers": _providers(),
        "utilityProvider": {"provider": "budget"},
    }))
    assert load_utility_provider()["model"] is None


# ------------------------------------------------------------
# runtime: 摘要 side-call 优先走 utility client
# ------------------------------------------------------------

def test_llm_summarize_prefers_utility_client():
    main_client = SummaryFakeClient("主模型摘要")
    utility_client = SummaryFakeClient("小模型摘要")
    rt = ConversationRuntime(
        session=make_session(12), api_client=main_client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["助手"],
    ).set_utility_client(utility_client)

    text = rt._llm_summarize(make_session(4).messages, None)

    assert text == "小模型摘要"
    assert main_client.summary_calls == []          # 主模型零摘要调用
    assert len(utility_client.summary_calls) == 1


def test_llm_summarize_falls_back_without_utility_client():
    main_client = SummaryFakeClient("主模型摘要")
    rt = ConversationRuntime(
        session=make_session(12), api_client=main_client,
        tool_executor=NoopExecutor(),
        permission_policy=PermissionPolicy(active_mode=ALLOW_MODE),
        system_prompt=["助手"],
    )
    assert rt._llm_summarize(make_session(4).messages, None) == "主模型摘要"


# ------------------------------------------------------------
# CLI 装配: utilityProvider 生效 / 无效配置降级不挡启动
# ------------------------------------------------------------

def test_assemble_wires_utility_client(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from storage import SessionStore
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setattr(main_mod, "get_orchestrator", lambda: SimpleNamespace(
        reconcile_orphans=lambda: 0))
    monkeypatch.setattr(main_mod, "load_utility_provider", lambda: {
        "api_key": "uk", "base_url": "https://u.example.com",
        "protocol": "anthropic", "model": "small-model"})

    _, runtime, _ = main_mod._assemble(
        SessionStore(storage_dir=tmp_path), "20260926-000100")

    assert runtime._utility_client is not None
    assert runtime._utility_client is not runtime._api_client
    assert runtime._utility_client.model == "small-model"


def test_assemble_invalid_utility_provider_degrades(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from storage import SessionStore
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setattr(main_mod, "get_orchestrator", lambda: SimpleNamespace(
        reconcile_orphans=lambda: 0))
    monkeypatch.setattr(main_mod, "load_utility_provider", lambda: {
        "api_key": "uk", "base_url": "https://u.example.com",
        "protocol": "bogus-protocol", "model": "small-model"})

    _, runtime, _ = main_mod._assemble(
        SessionStore(storage_dir=tmp_path), "20260926-000101")

    assert runtime._utility_client is None           # 降级: 跟主模型
    assert "utilityProvider" in capsys.readouterr().out
