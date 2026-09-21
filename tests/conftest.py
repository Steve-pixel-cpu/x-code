"""测试全局夹具:
- 默认关闭连接门禁（API_TOKEN 置空）, 避免每个用例都要携带 x-xcode-token;
  门禁自身的行为在 test_server 里单独验证。
- 掐断真实供应商连接: server 导入时会应用用户真实 ~/.x-code/settings.json
  （api_client 带真 key/base_url）, multi_agent 的 subagent
  工厂镜像同一份连接信息——测试里泄漏的工作线程会拿真实额度打真网
  （表现为用户账户莫名 1302 限流、测试进程被 300s 的 SSL 读拖住）。
  这里在每条用例前统一清空连接信息, 用后恢复。"""
import pytest

import server
import multi_agent


@pytest.fixture(autouse=True)
def _disable_token_gate(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")


@pytest.fixture(autouse=True)
def _no_real_api():
    """连接信息置空: 真实调用会在首次请求时立即认证失败（不可重试）,
    泄漏线程秒退, 不再拖住测试进程或烧真实额度。"""
    saved_key = server.api_client._api_key
    saved_url = server.api_client._base_url
    saved_model = server.api_client.model
    saved_provider_fn = multi_agent._api_config_provider

    def _dead_provider():
        return ("", None, saved_model, "anthropic")

    def _apply(key: str, url):
        server.api_client.reset_to(api_key=key, model=saved_model, base_url=url)

    _apply("", None)
    multi_agent.set_api_config_provider(_dead_provider)
    yield
    _apply(saved_key, saved_url)
    multi_agent.set_api_config_provider(saved_provider_fn)
