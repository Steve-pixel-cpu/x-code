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
def _no_user_mcp_in_tools():
    """测试进程内全局 TOOLS 不携带用户真实 MCP 工具。

    server 在 import 时按用户真实 ~/.x-code/settings.json 装配; 若用户
    配了 MCP 服务器, mcp__* spec 会混进共享的 main.TOOLS —— 依赖
    "TOOLS 末尾四位是 agent 四件套"之类结构断言的用例就会假失败
    (本地复现不了、只在配了 MCP 的机器上翻车)。会话内清一次即可:
    server 的模块级装配已完成, 这里只动列表内容, 不碰连接。"""
    from main import TOOLS
    saved = list(TOOLS)
    TOOLS[:] = [t for t in TOOLS if not t["name"].startswith("mcp__")]
    yield
    TOOLS[:] = saved


@pytest.fixture(autouse=True)
def _no_real_api():
    """连接信息置空: 真实调用会在首次请求时立即认证失败（不可重试）,
    泄漏线程秒退, 不再拖住测试进程或烧真实额度。"""
    saved_key = server.api_client._api_key
    saved_url = server.api_client._base_url
    saved_model = server.api_client.model
    saved_provider_fn = multi_agent._api_config_provider

    # 占位 key 不能用 "": openai>=3.x 的客户端构造期就校验凭据,
    # 空 key 直接抛 Missing credentials（夹具 setup 即失败, 整套测试全灭）。
    # 用非空占位串保持语义不变——真实调用在服务端 401, 照样秒退不打真网。
    dead_key = "test-no-real-api-placeholder"

    def _dead_provider():
        return (dead_key, None, saved_model, "anthropic")

    def _apply(key: str, url):
        server.api_client.reset_to(api_key=key, model=saved_model, base_url=url)

    _apply(dead_key, None)
    multi_agent.set_api_config_provider(_dead_provider)
    yield
    _apply(saved_key, saved_url)
    multi_agent.set_api_config_provider(saved_provider_fn)
