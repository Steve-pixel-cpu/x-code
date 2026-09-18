"""模型供应商配置: 未配置空态 / 保存读回 / 回退语义（读写归口 config.py）"""
import pytest

from config import load_providers, save_providers, SETTINGS_FILE  # noqa: E402
from server import _apply_provider_config  # noqa: E402


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
