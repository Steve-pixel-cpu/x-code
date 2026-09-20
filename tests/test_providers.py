"""模型供应商配置: 未配置空态 / 保存读回 / 回退语义（读写归口 config.py）"""
import pytest

from config import load_providers, save_providers, SETTINGS_FILE  # noqa: E402
from server import _apply_provider_config, _normalize_base_url  # noqa: E402


@pytest.fixture()
def providers_file(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    monkeypatch.setattr("config.SETTINGS_FILE", target)
    return target


def _cfg(active_provider="p1", active_model="m1"):
    return {
        "active": {"provider": active_provider, "model": active_model},
        "providers": [{
            "id": "p1", "name": "测试供应商",
            "base_url": "https://example.com/api", "api_key": "k",
            "enabled": True,
            "models": [{"id": "m1", "name": "m1", "tags": []},
                       {"id": "test-model-x", "name": "测试模型", "tags": []}],
        }],
    }


def test_unconfigured_when_missing(providers_file):
    # 无文件 = 未配置空态, 不写盘也没有 .env 播种兜底; 初始化页接管
    assert not providers_file.exists()
    cfg = load_providers()
    assert not providers_file.exists()
    assert cfg["active"] == {}
    assert cfg["providers"] == []


def test_save_and_roundtrip(providers_file):
    save_providers(_cfg())
    again = load_providers()
    assert again["providers"][0]["name"] == "测试供应商"
    assert again["active"] == {"provider": "p1", "model": "m1"}


def test_save_preserves_other_keys(providers_file):
    # settings.json 里其他用户级设置原样保留（读-改-写语义）
    providers_file.parent.mkdir(parents=True, exist_ok=True)
    providers_file.write_text('{"theme": "dark", "providers": []}', encoding="utf-8")
    save_providers(_cfg())
    import json
    data = json.loads(providers_file.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["providers"][0]["id"] == "p1"


def test_apply_falls_back_when_active_missing(providers_file):
    cfg = {"active": {"provider": "不存在的id", "model": "some-model"}, "providers": []}
    _apply_provider_config(cfg)                          # 不应抛错
    # 未配置回退: 模型/key 清空（无默认值）, 等初始化页接管
    from server import api_client
    assert api_client.model == ""
    assert api_client.raw_client.api_key == ""


def test_apply_switches_model(providers_file):
    cfg = _cfg(active_model="test-model-x")
    _apply_provider_config(cfg)
    from server import api_client
    assert api_client.model == "test-model-x"


# ============================================================================
# base_url 规范化: anthropic SDK 会在 base_url 后自动拼 /v1/messages,
# 用户照 OpenAI 习惯粘贴带 /v1 的地址会变成 /v1/v1/messages → 404。
# 保存/测试/应用三处统一剥掉结尾的 /v1 与多余斜杠。
# ============================================================================
def test_normalize_base_url_strips_v1():
    assert _normalize_base_url("https://codecraftapi.com/v1") == "https://codecraftapi.com"
    assert _normalize_base_url("https://codecraftapi.com/v1/") == "https://codecraftapi.com"


def test_normalize_base_url_case_insensitive():
    assert _normalize_base_url("https://codecraftapi.com/V1") == "https://codecraftapi.com"
    assert _normalize_base_url("https://codecraftapi.com/V1/") == "https://codecraftapi.com"


def test_normalize_base_url_strips_trailing_slash():
    assert _normalize_base_url("https://example.com/") == "https://example.com"
    assert _normalize_base_url("https://example.com///") == "https://example.com"


def test_normalize_base_url_keeps_other_paths():
    # 非 /v1 结尾的真实路径原样保留（只剥尾斜杠）
    assert _normalize_base_url("https://open.bigmodel.cn/api/anthropic") \
        == "https://open.bigmodel.cn/api/anthropic"
    assert _normalize_base_url("https://api.deepseek.com/anthropic/") \
        == "https://api.deepseek.com/anthropic"


def test_normalize_base_url_only_literal_v1_segment():
    # /v1 是字面段才剥: v10 / v2 / 路径中间的 v1 不动
    assert _normalize_base_url("https://example.com/v2") == "https://example.com/v2"
    assert _normalize_base_url("https://example.com/v10") == "https://example.com/v10"
    assert _normalize_base_url("https://example.com/v1/messages") \
        == "https://example.com/v1/messages"


def test_normalize_base_url_empty():
    assert _normalize_base_url("") == ""
    assert _normalize_base_url("   ") == ""
    assert _normalize_base_url(None) == ""


def test_normalize_base_url_trims_whitespace():
    assert _normalize_base_url("  https://codecraftapi.com/v1  ") \
        == "https://codecraftapi.com"
