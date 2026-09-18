"""测试全局夹具: 默认关闭连接门禁（API_TOKEN 置空）,
避免每个用例都要携带 x-xcode-token; 门禁自身的行为在 test_server 里单独验证。"""
import pytest

import server


@pytest.fixture(autouse=True)
def _disable_token_gate(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
