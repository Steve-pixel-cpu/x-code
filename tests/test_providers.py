"""模型供应商配置: 播种 / 保存读回 / 回退语义"""
import os

import pytest

# server.py 在 import 时要求 API_KEY 已设置（load_dotenv + sys.exit 兜底）
os.environ.setdefault("API_KEY", "test-key")

from server import load_provider_config, save_provider_config, _apply_provider_config  # noqa: E402


@pytest.fixture()
def providers_file(tmp_path, monkeypatch):
    target = tmp_path / "providers.json"
    monkeypatch.setattr("server.PROVIDERS_FILE", target)
    return target


def test_seed_when_missing(providers_file):
    # 读取不写盘（避免污染真实配置文件）, 只返回基于 .env 的默认配置
    assert not providers_file.exists()
    cfg = load_provider_config()
    assert not providers_file.exists()
    assert cfg["providers"], "应至少播种一个默认供应商"
    first = cfg["providers"][0]
    from server import API_KEY
    assert first["api_key"] == API_KEY                   # key 播种自服务端配置
    assert first["enabled"] is True
    assert cfg["active"]["provider"] == first["id"]
    # 保存时才落盘
    save_provider_config(cfg)
    assert providers_file.exists()


def test_save_and_roundtrip(providers_file):
    cfg = load_provider_config()
    cfg["providers"][0]["name"] = "重命名供应商"
    save_provider_config(cfg)
    again = load_provider_config()
    assert again["providers"][0]["name"] == "重命名供应商"


def test_apply_falls_back_when_active_missing(providers_file):
    cfg = load_provider_config()
    cfg["active"] = {"provider": "不存在的id", "model": "some-model"}
    _apply_provider_config(cfg)                          # 不应抛错
    # 回退 .env 默认: 模型重置为 DEFAULT_MODEL
    from server import api_client, DEFAULT_MODEL
    assert api_client.model == DEFAULT_MODEL


def test_apply_switches_model(providers_file):
    cfg = load_provider_config()
    prov = cfg["providers"][0]
    prov["models"].append({"id": "test-model-x", "name": "测试模型", "tags": []})
    cfg["active"] = {"provider": prov["id"], "model": "test-model-x"}
    _apply_provider_config(cfg)
    from server import api_client
    assert api_client.model == "test-model-x"
